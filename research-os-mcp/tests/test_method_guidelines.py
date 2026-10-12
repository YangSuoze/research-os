"""Task guidelines are persisted once and shared by concurrent paper imports."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from paper_import_mcp.infra.neo4j import GraphStore
from paper_import_mcp.models.paper_graph import PaperGraphDraft
from paper_import_mcp.services.paper_import import PaperImportService
from paper_import_mcp.services.paper_graph.method_guidelines import (
    GLOBAL_METHOD_RULES,
    MethodGuidelines,
)


class _MemoryStore:
    def __init__(self) -> None:
        self.tasks = {}
        self.saved = []

    async def ensure_task_root(self, task_id):
        return self.tasks.setdefault(task_id, {
            "id": task_id, "name": task_id.replace("_", " "),
            "definition": "", "method_guidelines": None,
        }).copy()

    async def save_method_guidelines(self, task_id, guidelines):
        self.saved.append(task_id)
        task = self.tasks[task_id]
        if task["method_guidelines"] is None:
            task["method_guidelines"] = guidelines
        return task["method_guidelines"]


class MethodGuidelinesTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_imports_initialize_once_and_restart_reuses_it(self):
        store = _MemoryStore()
        guide = "Family: Conditional Normalization. Leaf: Memory-Conditioned Normalization."

        async def generate(*_, **__):
            await asyncio.sleep(0)
            return json.dumps({"method_guidelines": guide})

        llm = SimpleNamespace(aquery=AsyncMock(side_effect=generate))
        guidelines = MethodGuidelines()
        results = await asyncio.gather(*[
            guidelines.get_or_create(" medical_report_generation ", store, llm)
            for _ in range(5)
        ])
        self.assertEqual(results, [guide] * 5)
        llm.aquery.assert_awaited_once()
        self.assertEqual(store.saved, ["medical_report_generation"])

        restarted = MethodGuidelines()
        self.assertEqual(
            await restarted.get_or_create("medical_report_generation", store, llm), guide
        )
        llm.aquery.assert_awaited_once()
        prompt = llm.aquery.call_args.args[0][0]["content"]
        self.assertIn(GLOBAL_METHOD_RULES, prompt)
        self.assertIn("in English", prompt)
        self.assertEqual(
            set(llm.aquery.call_args.kwargs["json_mode"].model_fields),
            {"method_guidelines"},
        )

    async def test_each_task_gets_its_own_guide(self):
        store = _MemoryStore()
        llm = SimpleNamespace(aquery=AsyncMock(side_effect=[
            json.dumps({"method_guidelines": "Medical task guide."}),
            json.dumps({"method_guidelines": "Code task guide."}),
        ]))
        guidelines = MethodGuidelines()
        self.assertEqual(
            await guidelines.get_or_create("medical_reports", store, llm),
            "Medical task guide.",
        )
        self.assertEqual(
            await guidelines.get_or_create("code_generation", store, llm),
            "Code task guide.",
        )
        self.assertEqual(llm.aquery.await_count, 2)

    async def test_existing_guide_and_human_task_definition_are_preserved(self):
        store = _MemoryStore()
        store.tasks["task_1"] = {
            "id": "task_1", "name": "Human task name", "definition": "Human scope",
            "method_guidelines": "Existing classification guide.",
        }
        llm = SimpleNamespace(aquery=AsyncMock())
        result = await MethodGuidelines().get_or_create("task_1", store, llm)
        self.assertEqual(result, "Existing classification guide.")
        self.assertEqual(store.tasks["task_1"]["definition"], "Human scope")
        self.assertEqual(store.saved, [])
        llm.aquery.assert_not_awaited()

    async def test_invalid_generation_is_not_saved_and_can_be_retried(self):
        store = _MemoryStore()
        llm = SimpleNamespace(aquery=AsyncMock(side_effect=[
            json.dumps({"method_guidelines": "  "}),
            json.dumps({"method_guidelines": "Valid task guide."}),
        ]))
        guidelines = MethodGuidelines()
        with self.assertRaises(ValidationError):
            await guidelines.get_or_create("task_1", store, llm)
        self.assertIsNone(store.tasks["task_1"]["method_guidelines"])
        self.assertEqual(store.saved, [])
        self.assertEqual(
            await guidelines.get_or_create("task_1", store, llm), "Valid task guide."
        )
        self.assertEqual(
            await guidelines.get_or_create("task_1", store, llm), "Valid task guide."
        )
        self.assertEqual(llm.aquery.await_count, 2)

    async def test_store_initializes_guide_without_overwriting_existing_content(self):
        task = {
            "id": "task_1", "name": "Human name", "definition": "Human scope",
            "method_guidelines": "Saved guide.",
        }
        session = AsyncMock()
        session.run.side_effect = [
            SimpleNamespace(single=AsyncMock(return_value=task)),
            SimpleNamespace(single=AsyncMock(return_value={"method_guidelines": "Saved guide."})),
        ]
        context = AsyncMock()
        context.__aenter__.return_value = session
        store = GraphStore(driver=SimpleNamespace(session=lambda: context))

        self.assertEqual(await store.ensure_task_root("task_1"), task)
        self.assertEqual(await store.save_method_guidelines("task_1", "New guide."), "Saved guide.")
        query = session.run.await_args_list[1].args[0]
        self.assertIn("coalesce(n.method_guidelines, $guidelines)", query)
        self.assertNotIn("n.definition =", query)


class GuidelineImportIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_import_passes_task_guide_to_extractor(self):
        service = PaperImportService()
        service._get_method_guidelines = AsyncMock(return_value="Saved task guide.")
        service._paper_excerpt = AsyncMock(return_value="Abstract")
        mineru = SimpleNamespace(aparse_pdf=AsyncMock(return_value=SimpleNamespace(
            middle_json={}, structured_content={},
        )))
        es = SimpleNamespace(
            index_parsed_paper=AsyncMock(return_value=SimpleNamespace(
                chunk_count=1, parse_version="mineru:chunker",
            )),
            close=AsyncMock(),
        )
        draft = SimpleNamespace(paper_title="Paper", model_dump=lambda **_: {})
        extractor = SimpleNamespace(extract=AsyncMock(return_value=draft), close=AsyncMock())
        finder = AsyncMock()
        finder.__aenter__.return_value = finder
        finder.find.return_value = None
        module = "paper_import_mcp.services.paper_import"
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(f"{module}.MinerUClient", return_value=mineru),
            patch(f"{module}.ElasticsearchPaperStore", return_value=es),
            patch(f"{module}.PaperGraphExtractor", return_value=extractor) as factory,
            patch(f"{module}.CodeLinkFinder", return_value=finder),
            patch(f"{module}._write_json"),
            patch(f"{module}.paths.paper_graph_draft_path", return_value=Path(directory) / "draft.json"),
            patch.object(service, "_mineru_version", return_value="4.0.5"),
        ):
            self.assertEqual(
                await service.run_index_extract("paper_1", "task_1"), "mineru:chunker"
            )
        service._get_method_guidelines.assert_awaited_once_with("task_1")
        factory.assert_called_once_with(paper_store=es, method_guidelines="Saved task guide.")
        extractor.close.assert_awaited_once()
        es.close.assert_awaited_once()

    async def test_neo4j_stage_passes_saved_guide_to_aligner(self):
        service = PaperImportService()
        service._guidelines.get_or_create = AsyncMock(return_value="Saved task guide.")
        store = SimpleNamespace(close=AsyncMock())
        llm = SimpleNamespace(close=AsyncMock())
        embedder = SimpleNamespace(model_name="bge-small")
        ingester = SimpleNamespace(ingest=AsyncMock())
        draft = PaperGraphDraft(
            paper_id="paper_1", parse_version="mineru:chunker", paper_title="Paper",
            mechanisms=[], addresses=[], results=[], limitations=[], datasets=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            draft_path = Path(directory) / "draft.json"
            draft_path.write_text(draft.model_dump_json(), encoding="utf-8")
            with (
                patch("paper_import_mcp.services.paper_import.paths.paper_graph_draft_path", return_value=draft_path),
                patch("paper_import_mcp.infra.neo4j.GraphStore", return_value=store),
                patch("paper_import_mcp.infra.llm.LLMClient", return_value=llm),
                patch("paper_import_mcp.infra.embedding.BGEEmbedder", return_value=embedder),
                patch("paper_import_mcp.services.paper_graph.aligner.GraphAligner") as factory,
                patch("paper_import_mcp.services.paper_graph.ingester.PaperGraphIngester", return_value=ingester),
            ):
                await service.run_neo4j("paper_1", "mineru:chunker", "task_1")
        service._guidelines.get_or_create.assert_awaited_once_with("task_1", store, llm)
        factory.assert_called_once_with(store, embedder, llm, method_guidelines="Saved task guide.")
        ingester.ingest.assert_awaited_once_with(draft, "task_1", code_link=None)
        store.close.assert_awaited_once()
        llm.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
