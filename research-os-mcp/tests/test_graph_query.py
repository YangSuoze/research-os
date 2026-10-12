"""Text-to-Cypher, read-only execution and task-scoped browsing."""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from mcp import Client
from paper_import_mcp import paths
from paper_import_mcp.infra.neo4j import (
    GRAPH_QUERY_RULES,
    GRAPH_SCHEMA,
    MAX_QUERY_ROWS,
    QUERY_TIMEOUT_SECONDS,
    GraphStore,
)
from paper_import_mcp.server import mcp
from paper_import_mcp.services.graph_query import GraphQueryService
from pydantic import ValidationError


class _Result:
    def __init__(self, rows=(), query_type="r"):
        self.rows = rows
        self.consume = AsyncMock(return_value=SimpleNamespace(query_type=query_type))

    def __aiter__(self):
        async def records():
            for row in self.rows:
                yield SimpleNamespace(data=lambda row=row: row)

        return records()


def _store(rows=(), query_type="r"):
    session = AsyncMock()
    session.run.side_effect = [_Result(query_type=query_type), _Result(rows)]
    context = AsyncMock()
    context.__aenter__.return_value = session
    driver = SimpleNamespace(session=Mock(return_value=context))
    return GraphStore(driver=driver), session, driver


class GraphQueryServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_generates_strict_cypher_with_schema_and_parameters(self):
        cypher = (
            "MATCH (p:Paper)-[:FOR_TASK]->(:TaskRoot {id: $task_root_id}) "
            "MATCH (p)-[:HAS_CLAIM]->(:Claim {kind: 'mechanism'})-[:ABOUT_METHOD]->(m:MethodLeaf) "
            "WHERE toLower(m.mechanism) CONTAINS 'contrastive' RETURN m.id AS id, m.name AS name LIMIT $limit"
        )
        store = SimpleNamespace(
            execute_read_query=AsyncMock(return_value=[{"id": "m1"}])
        )
        llm = SimpleNamespace(
            aquery=AsyncMock(return_value=json.dumps({"cypher": cypher}))
        )
        result = await GraphQueryService(store, llm, model_name="test-model").search(
            "查找对比学习方法", " medical_reports ", 5
        )
        self.assertEqual(result, {"cypher": cypher, "records": [{"id": "m1"}]})
        store.execute_read_query.assert_awaited_once_with(
            cypher, {"limit": 5, "task_root_id": "medical_reports"}, limit=5
        )
        request = llm.aquery.call_args
        self.assertIn(GRAPH_SCHEMA, request.args[0][0]["content"])
        self.assertIn(GRAPH_QUERY_RULES, request.args[0][0]["content"])
        self.assertEqual(
            json.loads(request.args[0][1]["content"])["question"], "查找对比学习方法"
        )
        self.assertEqual(set(request.kwargs["json_mode"].model_fields), {"cypher"})
        self.assertEqual(request.kwargs["temperature"], 0.0)

    def test_schema_uses_shared_definition_and_limits_without_io(self):
        store = SimpleNamespace(execute_read_query=AsyncMock())
        with patch("paper_import_mcp.services.graph_query.LLMClient") as factory:
            schema = GraphQueryService(store).get_schema()
            self.assertEqual(schema["schema"], GRAPH_SCHEMA)
            self.assertEqual(
                schema["query_rules"],
                {
                    "read_only": True,
                    "max_rows": MAX_QUERY_ROWS,
                    "timeout_seconds": QUERY_TIMEOUT_SECONDS,
                    "constraints": GRAPH_QUERY_RULES,
                },
            )
            factory.assert_not_called()
        store.execute_read_query.assert_not_awaited()

    async def test_direct_cypher_preserves_parameters_without_llm(self):
        cypher = "MATCH (p:Paper)-[:FOR_TASK]->(:TaskRoot {id: $task}) RETURN p.id AS paper_id"
        parameters = {"task": "medical_reports", "limit": 9999}
        store = SimpleNamespace(
            execute_read_query=AsyncMock(return_value=[{"paper_id": "p1"}])
        )
        with patch("paper_import_mcp.services.graph_query.LLMClient") as factory:
            result = await GraphQueryService(store).search(
                cypher=cypher, parameters=parameters, limit=3
            )
            factory.assert_not_called()
        self.assertEqual(result, {"cypher": cypher, "records": [{"paper_id": "p1"}]})
        store.execute_read_query.assert_awaited_once_with(cypher, parameters, limit=3)
        self.assertEqual(parameters, {"task": "medical_reports", "limit": 9999})

    async def test_direct_cypher_without_parameters_uses_empty_mapping(self):
        store = SimpleNamespace(execute_read_query=AsyncMock(return_value=[]))
        llm = SimpleNamespace(aquery=AsyncMock())
        result = await GraphQueryService(store, llm).search(cypher="RETURN 1 AS value")
        self.assertEqual(result["records"], [])
        store.execute_read_query.assert_awaited_once_with(
            "RETURN 1 AS value", {}, limit=50
        )
        llm.aquery.assert_not_awaited()

    async def test_ambiguous_modes_and_mode_specific_arguments_fail_before_io(self):
        store = SimpleNamespace(execute_read_query=AsyncMock())
        llm = SimpleNamespace(aquery=AsyncMock())
        service = GraphQueryService(store, llm)
        for options in [
            {},
            {"query": "methods", "cypher": "RETURN 1 AS value"},
            {"cypher": " "},
            {"query": "methods", "parameters": {}},
            {"cypher": "RETURN 1 AS value", "task_root_id": "t1"},
            {"cypher": "RETURN 1 AS value", "limit": 0},
            {"cypher": "RETURN 1 AS value", "limit": 101},
        ]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                await service.search(**options)
        store.execute_read_query.assert_not_awaited()
        llm.aquery.assert_not_awaited()

    async def test_direct_cypher_uses_existing_read_only_guard_and_outer_limit(self):
        store, session, _ = _store([{"value": "x"}])
        result = await GraphQueryService(store).search(
            cypher="RETURN $value AS value",
            parameters={"value": "x", "__result_limit": 99999},
            limit=1,
        )
        self.assertEqual(result["records"], [{"value": "x"}])
        self.assertTrue(
            session.run.await_args_list[0].args[0].text.startswith("EXPLAIN ")
        )
        self.assertEqual(
            session.run.await_args.args[1], {"value": "x", "__result_limit": 1}
        )

    async def test_unsafe_direct_cypher_is_rejected_without_llm(self):
        for query, query_type in [
            ("CALL db.labels() YIELD label RETURN label", "r"),
            ("CREATE (:Paper {id: 'unsafe'})", "w"),
            ("MATCH (p:Paper) SET p.title = 'changed' RETURN p.id AS id", "rw"),
        ]:
            with self.subTest(query=query):
                store, session, _ = _store(query_type=query_type)
                with patch(
                    "paper_import_mcp.services.graph_query.LLMClient"
                ) as factory:
                    with self.assertRaises(ValueError):
                        await GraphQueryService(store).search(cypher=query)
                    factory.assert_not_called()
                self.assertEqual(
                    session.run.await_count, 1 if query_type == "rw" else 0
                )

    async def test_invalid_inputs_do_not_call_llm(self):
        llm = SimpleNamespace(aquery=AsyncMock())
        service = GraphQueryService(SimpleNamespace(), llm)
        for query, task, limit in [
            (" ", None, 50),
            ("methods", " ", 50),
            ("methods", None, 0),
            ("methods", None, 101),
        ]:
            with (
                self.subTest(query=query, task=task, limit=limit),
                self.assertRaises(ValueError),
            ):
                await service.search(query, task, limit)
        llm.aquery.assert_not_awaited()

    async def test_malformed_llm_response_does_not_reach_database(self):
        store = SimpleNamespace(execute_read_query=AsyncMock())
        llm = SimpleNamespace(aquery=AsyncMock(return_value='{"sql": "SELECT 1"}'))
        with self.assertRaises(ValidationError):
            await GraphQueryService(store, llm).search("find methods")
        store.execute_read_query.assert_not_awaited()

    async def test_browse_does_not_create_or_call_llm(self):
        expected = {
            "task": {"id": "t1"},
            "families": [],
            "problems": [],
            "datasets": [],
        }
        store = SimpleNamespace(
            browse_task=AsyncMock(return_value=expected), close=AsyncMock()
        )
        service = GraphQueryService(store)
        with patch("paper_import_mcp.services.graph_query.LLMClient") as factory:
            self.assertEqual(await service.browse_task("t1"), expected)
            await service.close()
            factory.assert_not_called()
        store.close.assert_awaited_once()

    async def test_clients_are_closed_even_if_llm_close_fails(self):
        store = SimpleNamespace(close=AsyncMock())
        llm = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
        with self.assertRaises(RuntimeError):
            await GraphQueryService(store, llm).close()
        store.close.assert_awaited_once()


class GraphReadQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_explain_precedes_execution_and_limit_is_server_controlled(self):
        store, session, driver = _store([{"id": "m1", "date": date(2026, 10, 12)}])
        result = await store.execute_read_query(
            "MATCH (m:MethodLeaf) RETURN m.id AS id", {"__result_limit": 99999}, limit=3
        )
        self.assertEqual(result, [{"id": "m1", "date": "2026-10-12"}])
        driver.session.assert_called_once_with(default_access_mode="READ")
        first, second = session.run.await_args_list
        self.assertTrue(first.args[0].text.startswith("EXPLAIN CALL () {"))
        self.assertEqual(first.args[0].text, "EXPLAIN " + second.args[0].text)
        self.assertTrue(second.args[0].text.endswith("LIMIT $__result_limit"))
        self.assertEqual(second.args[1]["__result_limit"], 3)
        self.assertEqual(second.args[0].timeout, 10)

    async def test_non_read_query_types_are_rejected_before_execution(self):
        for query_type in ("w", "rw", "s", None):
            with self.subTest(query_type=query_type):
                store, session, _ = _store(query_type=query_type)
                with self.assertRaisesRegex(ValueError, "read-only"):
                    await store.execute_read_query(
                        "MATCH (n:MethodLeaf) SET n.name = 'changed' RETURN n.id AS id"
                    )
                self.assertEqual(session.run.await_count, 1)

    async def test_unsafe_queries_never_reach_database(self):
        queries = [
            "CREATE (:MethodLeaf {id: 'new'})",
            "CALL apoc.cypher.run('MATCH (n) DELETE n', {}) YIELD value RETURN value",
            "MATCH (n) CALL db.labels() YIELD label RETURN label",
            "LOAD CSV FROM 'https://example.com/data.csv' AS row RETURN row",
            "USE system RETURN 1 AS value",
            "MATCH (n) RETURN n.id AS id; MATCH (m) RETURN m.id AS id",
            "MATCH (n) /* hidden */ RETURN n.id AS id",
            "MATCH (n) RETURN n.id AS id // comment",
            "EXPLAIN MATCH (n) RETURN n.id AS id",
        ]
        for cypher in queries:
            with self.subTest(cypher=cypher):
                store, _, driver = _store()
                with self.assertRaises(ValueError):
                    await store.execute_read_query(cypher)
                driver.session.assert_not_called()

    async def test_command_words_in_literals_and_identifiers_are_not_commands(self):
        store, session, _ = _store([{"text": "CALL ; // DELETE"}])
        await store.execute_read_query("RETURN 'CALL ; // DELETE' AS `USE`")
        self.assertEqual(session.run.await_count, 2)

    async def test_client_timeout_covers_unresponsive_database(self):
        store, session, _ = _store()

        async def stalled(*_, **__):
            await asyncio.Future()

        session.run.side_effect = stalled
        timeout = asyncio.timeout
        with (
            patch(
                "paper_import_mcp.infra.neo4j.asyncio.timeout",
                side_effect=lambda _: timeout(0.01),
            ),
            self.assertRaises(TimeoutError),
        ):
            await store.execute_read_query("RETURN 1 AS value")

    async def test_task_browse_uses_paper_provenance_not_shared_taxonomy(self):
        expected = {
            "task": {"id": "t1"},
            "families": [],
            "problems": [],
            "datasets": [],
        }
        store, session, _ = _store([expected])
        self.assertEqual(await store.browse_task(" t1 "), expected)
        cypher = session.run.await_args.args[0].text
        self.assertIn("(paper:Paper)-[:FOR_TASK]->(task)", cypher)
        self.assertIn("kind: 'mechanism'", cypher)
        self.assertIn("kind: 'addresses'", cypher)
        self.assertIn("(paper)-[:USES_DATASET]->(dataset:Dataset)", cypher)
        self.assertNotIn("-[:HAS_FAMILY]", cypher)
        self.assertNotIn("-[:ADDRESSES]", cypher)
        self.assertNotIn("MERGE", cypher)
        self.assertEqual(session.run.await_args.args[1], {"task_root_id": "t1"})

    async def test_missing_task_is_reported_without_creating_it(self):
        store, session, _ = _store([])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            await store.browse_task("missing")
        self.assertEqual(session.run.await_count, 2)


class GraphMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_tools_are_callable_and_errors_are_reported(self):
        graph = SimpleNamespace(
            get_schema=Mock(return_value=GraphQueryService.get_schema()),
            search=AsyncMock(
                return_value={"cypher": "RETURN 1 AS value", "records": [{"value": 1}]}
            ),
            browse_task=AsyncMock(
                return_value={
                    "task": {"id": "t1"},
                    "families": [],
                    "problems": [],
                    "datasets": [],
                }
            ),
            close=AsyncMock(),
        )

        async def idle():
            await asyncio.Future()

        worker = SimpleNamespace(run_exclusive=idle)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(paths, "PAPER_IMPORTS_DIR", Path(directory) / "imports"),
            patch("paper_import_mcp.server.PaperImportWorker", return_value=worker),
            patch("paper_import_mcp.server.GraphStore", return_value=object()),
            patch("paper_import_mcp.server.GraphQueryService", return_value=graph),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "search_graph",
                    {"query": "find methods", "task_root_id": "t1", "limit": 5},
                )
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(result.structured_content["records"], [{"value": 1}])
                graph.search.assert_awaited_once_with(
                    "find methods", "t1", 5, cypher=None, parameters=None
                )
                result = await client.call_tool("get_graph_schema", {})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(result.structured_content["schema"], GRAPH_SCHEMA)
                graph.get_schema.assert_called_once_with()
                result = await client.call_tool(
                    "search_graph",
                    {
                        "cypher": "RETURN $value AS value",
                        "parameters": {"value": 1},
                        "limit": 2,
                    },
                )
                self.assertFalse(result.is_error, result.content)
                graph.search.assert_awaited_with(
                    None,
                    None,
                    2,
                    cypher="RETURN $value AS value",
                    parameters={"value": 1},
                )
                graph.search.side_effect = ValueError(
                    "Provide exactly one of query or cypher"
                )
                result = await client.call_tool(
                    "search_graph", {"query": "methods", "cypher": "RETURN 1 AS value"}
                )
                self.assertTrue(result.is_error)
                result = await client.call_tool("browse_task", {"task_root_id": "t1"})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(result.structured_content["task"]["id"], "t1")
                graph.browse_task.side_effect = ValueError(
                    "TaskRoot 'missing' does not exist"
                )
                result = await client.call_tool(
                    "browse_task", {"task_root_id": "missing"}
                )
                self.assertTrue(result.is_error)
        graph.close.assert_awaited_once()


@unittest.skipUnless(
    os.getenv("PAPER_GRAPH_QUERY_TEST_NEO4J") == "1", "Opt-in Neo4j rollback fixture"
)
class GraphQueryNeo4jTest(unittest.IsolatedAsyncioTestCase):
    async def test_shared_entities_do_not_leak_into_other_tasks(self):
        from neo4j import AsyncGraphDatabase
        from paper_import_mcp.config import settings

        prefix = "query_test_" + uuid4().hex
        ids = {
            key: f"{prefix}_{key}"
            for key in (
                "a",
                "b",
                "empty",
                "family",
                "shared",
                "method_a",
                "method_b",
                "paper_a",
                "paper_b",
                "claim_a",
                "claim_b",
                "address_a",
                "address_b",
                "problem_a",
                "problem_b",
                "dataset_a",
                "dataset_b",
            )
        }
        labels = {
            "TaskRoot": ["a", "b", "empty"],
            "MethodFamily": ["family"],
            "MethodLeaf": ["shared", "method_a", "method_b"],
            "Paper": ["paper_a", "paper_b"],
            "Claim": ["claim_a", "claim_b", "address_a", "address_b"],
            "Problem": ["problem_a", "problem_b"],
            "Dataset": ["dataset_a", "dataset_b"],
        }
        nodes = [
            {
                "label": label,
                "id": ids[key],
                "properties": {
                    "name": key,
                    "definition": "scope test",
                    "mechanism": "test mechanism",
                    "description": "scope test",
                },
            }
            for label, keys in labels.items()
            for key in keys
        ]
        for node in nodes:
            if node["label"] == "Claim":
                node["properties"]["kind"] = (
                    "addresses" if "address" in node["id"] else "mechanism"
                )
        edges = []

        def edge(source, relation, target):
            edges.append(
                {"source": ids[source], "type": relation, "target": ids[target]}
            )

        for suffix in ("a", "b"):
            edge(suffix, "HAS_FAMILY", "family")
            edge(f"paper_{suffix}", "FOR_TASK", suffix)
            edge(f"paper_{suffix}", "HAS_CLAIM", f"claim_{suffix}")
            edge(f"claim_{suffix}", "ABOUT_METHOD", "shared")
            edge(f"claim_{suffix}", "ABOUT_METHOD", f"method_{suffix}")
            edge(f"paper_{suffix}", "HAS_CLAIM", f"address_{suffix}")
            edge(f"address_{suffix}", "ABOUT_PROBLEM", f"problem_{suffix}")
            edge("shared", "ADDRESSES", f"problem_{suffix}")
            edge(f"paper_{suffix}", "USES_DATASET", f"dataset_{suffix}")
            edge("family", "HAS_METHOD", f"method_{suffix}")
        edge("family", "HAS_METHOD", "shared")
        edge("dataset_b", "FOR_TASK", "a")  # 故意留下误导的共享关系。

        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            notifications_min_severity="OFF",
        )
        async with driver, driver.session() as session:
            tx = await session.begin_transaction(timeout=15)
            try:
                await GraphStore._upsert_graph_tx(tx, nodes, edges)

                class TxSession:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_):
                        pass

                    async def run(self, query, parameters):
                        return await tx.run(query.text, parameters)

                store = GraphStore(
                    driver=SimpleNamespace(session=lambda **_: TxSession())
                )
                result = await store.browse_task(ids["a"])
                self.assertEqual(result["task"]["id"], ids["a"])
                self.assertEqual(
                    {m["id"] for f in result["families"] for m in f["methods"]},
                    {ids["shared"], ids["method_a"]},
                )
                self.assertEqual(
                    [p["id"] for p in result["problems"]], [ids["problem_a"]]
                )
                self.assertEqual(
                    [d["id"] for d in result["datasets"]], [ids["dataset_a"]]
                )
                empty = await store.browse_task(ids["empty"])
                self.assertEqual(
                    [empty[key] for key in ("families", "problems", "datasets")],
                    [[], [], []],
                )
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    await store.browse_task(prefix + "_missing")
                rows = await store.execute_read_query(
                    "UNWIND range(1, 10) AS value RETURN value AS number", limit=2
                )
                self.assertEqual(len(rows), 2)
            finally:
                await tx.rollback()
            count = await session.run(
                "MATCH (n) WHERE n.id STARTS WITH $prefix RETURN count(n) AS total",
                prefix=prefix,
            )
            self.assertEqual((await count.single())["total"], 0)


if __name__ == "__main__":
    unittest.main()
