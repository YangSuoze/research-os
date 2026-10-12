"""论文知识图谱的 Neo4j 存取。向量索引只用于四类可跨论文对齐的节点。"""

from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from paper_import_mcp.config import settings

ALIGNED_LABELS = {
    "Problem": "description",
    "MethodFamily": "definition",
    "MethodLeaf": "mechanism",
    "Dataset": "description",
}
NODE_LABELS = frozenset((*ALIGNED_LABELS, "TaskRoot", "Paper", "Claim", "Evidence"))
RELATION_TYPES = frozenset(
    {
        "HAS_FAMILY",
        "HAS_METHOD",
        "HAS_CLAIM",
        "HAS_EVIDENCE",
        "SUPPORTS",
        "ABOUT_METHOD",
        "ABOUT_PROBLEM",
        "ON_DATASET",
        "ADDRESSES",
        "USES_DATASET",
        "FOR_TASK",
    }
)
VECTOR_INDEXES = {label: f"{label.lower()}_embedding" for label in ALIGNED_LABELS}

GRAPH_SCHEMA = """Nodes (all have a unique string id):
TaskRoot: id, name, definition, method_guidelines
MethodFamily: id, name, definition, aliases
MethodLeaf: id, name, mechanism, aliases
Problem: id, name, description, aliases
Dataset: id, name, description, aliases
Paper: id (the paper_id), title, code_link
Claim: id, kind (mechanism/addresses/result/limitation), text, paper_id,
       parse_version, name (optional limitation name)
Evidence: id, paper_id, parse_version, chunk_no (integer), block_ids (string array)

Directed relationships:
TaskRoot -HAS_FAMILY-> MethodFamily -HAS_METHOD-> MethodLeaf
Paper -FOR_TASK-> TaskRoot
Paper -HAS_CLAIM-> Claim -ABOUT_METHOD-> MethodLeaf
Claim -ABOUT_PROBLEM-> Problem
Claim -ON_DATASET-> Dataset
MethodLeaf -ADDRESSES-> Problem
Paper -USES_DATASET-> Dataset -FOR_TASK-> TaskRoot
Paper -HAS_EVIDENCE-> Evidence -SUPPORTS-> Claim or Dataset

Methods, families, problems and datasets are shared across tasks. A task's actual
methods come from its Papers' mechanism Claims; its problems come from its Papers'
addresses Claims; its datasets come from its Papers' USES_DATASET relationships.
Do not infer task membership solely from HAS_FAMILY/HAS_METHOD, ADDRESSES or
Dataset.FOR_TASK: these shared relationships can include other papers' knowledge.
Result and limitation are Claim kinds, NOT separate node labels. Claims with no
ABOUT_METHOD are paper-level findings; do not attribute them to a particular method.
Evidence locates original ES chunks by paper_id, parse_version, chunk_no and block_ids.
Do not return embeddings, raw nodes, relationships or paths; project explicit properties.
"""

MAX_QUERY_ROWS = 100
QUERY_TIMEOUT_SECONDS = 10
GRAPH_QUERY_RULES = """Submit one read-only Cypher statement. Start with MATCH,
OPTIONAL MATCH, WITH, UNWIND or RETURN. Do not use CALL (including subqueries),
LOAD CSV, USE, SHOW, write/schema/administration commands, comments, semicolons
or EXPLAIN/PROFILE/CYPHER directives. Project named properties, not raw nodes,
relationships, paths or embeddings. The server checks the EXPLAIN query type
and enforces an outer result row limit and a database timeout.
Direct Cypher is not rewritten and does not call an LLM. Supply its parameters
explicitly; the server reserves __result_limit. Direct task filtering must be
written into Cypher using paper provenance, not inferred from shared taxonomy.
"""
_CYPHER_LITERALS = re.compile(
    r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:``|[^`])*`""", re.DOTALL
)

_TASK_BROWSE_QUERY = """
MATCH (task:TaskRoot {id: $task_root_id})
CALL (task) {
    MATCH (paper:Paper)-[:FOR_TASK]->(task)
    MATCH (paper)-[:HAS_CLAIM]->(:Claim {kind: 'mechanism'})-[:ABOUT_METHOD]->(method:MethodLeaf)
    WITH DISTINCT method
    MATCH (family:MethodFamily)-[:HAS_METHOD]->(method)
    WITH family, collect(DISTINCT method{.id, .name, .mechanism}) AS methods
    ORDER BY family.name
    RETURN collect(family{.id, .name, .definition, methods: methods}) AS families
}
CALL (task) {
    MATCH (paper:Paper)-[:FOR_TASK]->(task)
    MATCH (paper)-[:HAS_CLAIM]->(:Claim {kind: 'addresses'})-[:ABOUT_PROBLEM]->(problem:Problem)
    WITH DISTINCT problem ORDER BY problem.name
    RETURN collect(problem{.id, .name, .description}) AS problems
}
CALL (task) {
    MATCH (paper:Paper)-[:FOR_TASK]->(task)
    MATCH (paper)-[:USES_DATASET]->(dataset:Dataset)
    WITH DISTINCT dataset ORDER BY dataset.name
    RETURN collect(dataset{.id, .name, .description}) AS datasets
}
RETURN task{.id, .name, .definition} AS task, families, problems, datasets
"""


class GraphStore:
    def __init__(
        self,
        uri: str = settings.neo4j_uri,
        user: str = settings.neo4j_user,
        password: str = settings.neo4j_password,
        *,
        driver=None,
    ) -> None:
        if driver is None:
            from neo4j import AsyncGraphDatabase

            driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._driver = driver

    async def close(self) -> None:
        await self._driver.close()

    async def verify(self) -> None:
        await self._driver.verify_connectivity()

    async def execute_read_query(
        self, cypher: str, parameters: dict | None = None, *, limit: int = 50
    ) -> list[dict]:
        """执行模型或调用方提供的单条只读 Cypher；由 Neo4j 编译器核对类型。"""
        cypher = cypher.strip()
        visible = _CYPHER_LITERALS.sub(" ", cypher)
        if not re.match(
            r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|RETURN)\b", visible, re.IGNORECASE
        ):
            raise ValueError("Cypher must start with a read query clause")
        if re.search(
            r"\b(CALL|LOAD|USE|SHOW|PROFILE|EXPLAIN|CYPHER)\b|;|//|/\*",
            visible,
            re.IGNORECASE,
        ):
            raise ValueError(
                "Procedure calls, external loading, comments and multiple statements are not allowed"
            )
        if not 1 <= limit <= MAX_QUERY_ROWS:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_ROWS}")
        # 外层 LIMIT 不依赖模型是否正确添加 LIMIT，也覆盖 UNION 查询的总行数。
        bounded = f"CALL () {{\n{cypher}\n}}\nRETURN * LIMIT $__result_limit"
        return await self._read_query(
            bounded, {**(parameters or {}), "__result_limit": limit}
        )

    async def browse_task(self, task_root_id: str) -> dict:
        """仅依据该任务实际论文的 Claim/USES_DATASET 组织知识，不调用模型。"""
        task_root_id = task_root_id.strip()
        if not task_root_id:
            raise ValueError("task_root_id must not be blank")
        records = await self._read_query(
            _TASK_BROWSE_QUERY, {"task_root_id": task_root_id}
        )
        if not records:
            raise ValueError(f"TaskRoot {task_root_id!r} does not exist")
        return records[0]

    async def _read_query(self, cypher: str, parameters: dict) -> list[dict]:
        from neo4j import READ_ACCESS, Query

        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS + 5):
            async with self._driver.session(default_access_mode=READ_ACCESS) as session:
                plan = await session.run(
                    Query("EXPLAIN " + cypher, timeout=QUERY_TIMEOUT_SECONDS),
                    parameters,
                )
                summary = await plan.consume()
                # READ_ACCESS 只负责路由，不能替代执行前的只读检查。
                if summary.query_type != "r":
                    raise ValueError("Only read-only Cypher is allowed")
                result = await session.run(
                    Query(cypher, timeout=QUERY_TIMEOUT_SECONDS), parameters
                )
                records = [record.data() async for record in result]
        # 时间等 Neo4j 特有值转为字符串，保证 MCP 返回标准 JSON。
        return json.loads(json.dumps(records, ensure_ascii=False, default=str))

    async def initialize_schema(self) -> None:
        async with self._driver.session() as session:
            for label in NODE_LABELS:
                await (
                    await session.run(
                        f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS "
                        f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                    )
                ).consume()
            for label, index in VECTOR_INDEXES.items():
                await (
                    await session.run(
                        f"CREATE VECTOR INDEX {index} IF NOT EXISTS FOR (n:{label}) "
                        "ON (n.embedding) OPTIONS {indexConfig: "
                        "{`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}"
                    )
                ).consume()
            await (await session.run("CALL db.awaitIndexes(60)")).consume()

    async def find_candidates(
        self,
        label: str,
        embedding: list[float],
        limit: int,
        embedding_model: str,
    ) -> list[dict]:
        detail = ALIGNED_LABELS[label]
        parent_context = (
            "OPTIONAL MATCH (family:MethodFamily)-[:HAS_METHOD]->(node) "
            "WITH node, score, collect(DISTINCT family{.id, .name, .definition}) AS parents "
            if label == "MethodLeaf"
            else "WITH node, score, [] AS parents "
        )
        query = (
            "CALL db.index.vector.queryNodes($index, $limit, $embedding) "
            "YIELD node, score "
            + parent_context
            + "WHERE node.embedding_model = $model "
            + f"RETURN node.id AS id, node.name AS name, node.{detail} AS detail, "
            + "parents, score ORDER BY score DESC"
        )
        async with self._driver.session() as session:
            result = await session.run(
                query,
                index=VECTOR_INDEXES[label],
                limit=limit,
                embedding=embedding,
                model=embedding_model,
            )
            return [dict(row) async for row in result]

    async def save_entity(
        self,
        label: str,
        name: str,
        detail: str,
        embedding: list[float],
        embedding_model: str,
        merge_ids: tuple[str, ...] = (),
    ) -> str:
        if label not in ALIGNED_LABELS:
            raise ValueError(f"Unsupported entity label: {label}")
        entity_id = merge_ids[0] if merge_ids else f"{label.lower()}_{uuid4().hex}"
        async with self._driver.session() as session:
            return await session.execute_write(
                self._save_entity_tx,
                label,
                entity_id,
                name,
                detail,
                embedding,
                embedding_model,
                merge_ids,
            )

    @staticmethod
    async def _save_entity_tx(
        tx,
        label: str,
        entity_id: str,
        name: str,
        detail: str,
        embedding: list[float],
        embedding_model: str,
        merge_ids: tuple[str, ...],
    ) -> str:
        old_names: set[str] = set()
        relationships: list[dict] = []
        if merge_ids:
            result = await tx.run(
                f"MATCH (n:{label}) WHERE n.id IN $ids "
                "RETURN n.id AS id, n.name AS name, n.aliases AS aliases",
                ids=list(merge_ids),
            )
            old_nodes = [dict(row) async for row in result]
            if len(old_nodes) != len(merge_ids):
                raise ValueError("Alignment candidate no longer exists")
            for node in old_nodes:
                old_names.update([node["name"], *(node["aliases"] or [])])
            if len(merge_ids) > 1:
                result = await tx.run(
                    "MATCH (a)-[r]->(b) WHERE a.id IN $ids OR b.id IN $ids "
                    "RETURN a.id AS source, b.id AS target, type(r) AS type, "
                    "properties(r) AS properties",
                    ids=list(merge_ids[1:]),
                )
                relationships = [dict(row) async for row in result]
                await (
                    await tx.run(
                        f"MATCH (n:{label}) WHERE n.id IN $ids DETACH DELETE n",
                        ids=list(merge_ids[1:]),
                    )
                ).consume()

        aliases = sorted((old_names | {name}) - {name})
        detail_property = ALIGNED_LABELS[label]
        await (
            await tx.run(
                f"MERGE (n:{label} {{id: $id}}) "
                f"SET n.name = $name, n.{detail_property} = $detail, "
                "n.aliases = $aliases, n.embedding = $embedding, "
                "n.embedding_model = $embedding_model",
                id=entity_id,
                name=name,
                detail=detail,
                aliases=aliases,
                embedding=embedding,
                embedding_model=embedding_model,
            )
        ).consume()
        removed_ids = set(merge_ids[1:])
        for relationship in relationships:
            relation_type = relationship["type"]
            if relation_type not in RELATION_TYPES:
                raise ValueError(f"Unsupported relationship type: {relation_type}")
            source = (
                entity_id
                if relationship["source"] in removed_ids
                else relationship["source"]
            )
            target = (
                entity_id
                if relationship["target"] in removed_ids
                else relationship["target"]
            )
            if source != target:
                await (
                    await tx.run(
                        f"MATCH (a {{id: $source}}), (b {{id: $target}}) "
                        f"MERGE (a)-[r:{relation_type}]->(b) SET r += $properties",
                        source=source,
                        target=target,
                        properties=relationship["properties"],
                    )
                ).consume()
        return entity_id

    async def upsert_task_root(self, task_id: str, name: str, definition: str) -> None:
        async with self._driver.session() as session:
            await (
                await session.run(
                    "MERGE (n:TaskRoot {id: $id}) "
                    "SET n.name = $name, n.definition = $definition",
                    id=task_id,
                    name=name,
                    definition=definition,
                )
            ).consume()

    async def ensure_task_root(self, task_id: str) -> dict:
        """创建缺失的 TaskRoot，并读取任务定义与已保存的分类指南。"""

        async with self._driver.session() as session:
            result = await session.run(
                "MERGE (n:TaskRoot {id: $id}) "
                "ON CREATE SET n.name = $name, n.definition = '' "
                "RETURN n.id AS id, n.name AS name, n.definition AS definition, "
                "n.method_guidelines AS method_guidelines",
                id=task_id,
                name=task_id.replace("_", " "),
            )
            return dict(await result.single())

    async def save_method_guidelines(self, task_id: str, guidelines: str) -> str:
        """只初始化缺失的指南，已有内容始终复用。"""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (n:TaskRoot {id: $id}) "
                "SET n.method_guidelines = coalesce(n.method_guidelines, $guidelines) "
                "RETURN n.method_guidelines AS method_guidelines",
                id=task_id,
                guidelines=guidelines,
            )
            return (await result.single())["method_guidelines"]

    async def clear_paper(self, paper_id: str) -> None:
        """替换论文前删除旧 Paper/Claim/Evidence，保留仍被其他论文引用的实体。"""
        async with self._driver.session() as session:
            await session.execute_write(self._clear_paper_tx, paper_id)

    @staticmethod
    async def _clear_paper_tx(tx, paper_id: str) -> None:
        async def ids(query: str) -> list[str]:
            result = await tx.run(query, paper_id=paper_id)
            return [row["id"] async for row in result]

        method_ids = await ids(
            "MATCH (:Claim {paper_id: $paper_id})-[:ABOUT_METHOD]->(n:MethodLeaf) "
            "RETURN DISTINCT n.id AS id"
        )
        problem_ids = await ids(
            "MATCH (:Claim {paper_id: $paper_id})-[:ABOUT_PROBLEM]->(n:Problem) "
            "RETURN DISTINCT n.id AS id"
        )
        dataset_ids = await ids(
            "MATCH (:Paper {id: $paper_id})-[:USES_DATASET]->(n:Dataset) "
            "RETURN DISTINCT n.id AS id"
        )
        result = await tx.run(
            "MATCH (f:MethodFamily)-[:HAS_METHOD]->(m:MethodLeaf) "
            "WHERE m.id IN $method_ids RETURN DISTINCT f.id AS id",
            method_ids=method_ids,
        )
        family_ids = [row["id"] async for row in result]

        await (
            await tx.run(
                "MATCH (n) WHERE (n:Paper AND n.id = $paper_id) "
                "OR ((n:Claim OR n:Evidence) AND n.paper_id = $paper_id) "
                "DETACH DELETE n",
                paper_id=paper_id,
            )
        ).consume()
        await (
            await tx.run(
                "MATCH (m:MethodLeaf)-[r:ADDRESSES]->(p:Problem) "
                "WHERE m.id IN $methods AND p.id IN $problems "
                "AND NOT EXISTS { MATCH (c:Claim)-[:ABOUT_METHOD]->(m) "
                "MATCH (c)-[:ABOUT_PROBLEM]->(p) } DELETE r",
                methods=method_ids,
                problems=problem_ids,
            )
        ).consume()
        for label, candidate_ids, reference in (
            ("MethodLeaf", method_ids, "(:Claim)-[:ABOUT_METHOD]->(n)"),
            ("Problem", problem_ids, "(:Claim)-[:ABOUT_PROBLEM]->(n)"),
            ("Dataset", dataset_ids, "(:Paper)-[:USES_DATASET]->(n)"),
            ("MethodFamily", family_ids, "(n)-[:HAS_METHOD]->(:MethodLeaf)"),
        ):
            await (
                await tx.run(
                    f"MATCH (n:{label}) WHERE n.id IN $ids "
                    f"AND NOT EXISTS {{ MATCH {reference} }} DETACH DELETE n",
                    ids=candidate_ids,
                )
            ).consume()

    async def upsert_graph(self, nodes: list[dict], relationships: list[dict]) -> None:
        """写入论文专属节点及边；所有节点具有稳定 id。"""
        async with self._driver.session() as session:
            await session.execute_write(self._upsert_graph_tx, nodes, relationships)

    @staticmethod
    async def _upsert_graph_tx(
        tx, nodes: list[dict], relationships: list[dict]
    ) -> None:
        for node in nodes:
            label = node["label"]
            if label not in NODE_LABELS:
                raise ValueError(f"Unsupported node label: {label}")
            await (
                await tx.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
                    id=node["id"],
                    properties=node["properties"],
                )
            ).consume()
        for relationship in relationships:
            relation_type = relationship["type"]
            if relation_type not in RELATION_TYPES:
                raise ValueError(f"Unsupported relationship type: {relation_type}")
            await (
                await tx.run(
                    "MATCH (a {id: $source}), (b {id: $target}) "
                    f"MERGE (a)-[:{relation_type}]->(b)",
                    source=relationship["source"],
                    target=relationship["target"],
                )
            ).consume()
