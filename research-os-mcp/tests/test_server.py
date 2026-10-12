"""Protocol-level paper-import tests without external services or model downloads."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from paper_import_mcp import paths
from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.server import mcp


class _IdleWorker:
    async def run_exclusive(self) -> None:
        await asyncio.Future()


class PaperImportMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_status_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\nexample")
            with (
                patch.object(paths, "PAPERS_DIR", root / "papers"),
                patch.object(paths, "PAPER_IMPORTS_DIR", root / "paper_imports"),
                patch(
                    "paper_import_mcp.server.PaperImportWorker",
                    return_value=_IdleWorker(),
                ),
            ):
                async with Client(mcp) as client:
                    await self._check_create_status_and_retry(client, source)

    async def _check_create_status_and_retry(
        self, client: Client, source: Path
    ) -> None:
        tools = await client.list_tools()
        self.assertEqual(
            {tool.name for tool in tools.tools},
            {
                "create_paper_import_batch",
                "get_paper_import_batch",
                "retry_failed_paper_import_batch",
                "search_graph",
                "browse_task",
                "get_paper_context",
            },
        )
        created = await client.call_tool(
            "create_paper_import_batch",
            {"task_root_id": " report_generation ", "pdf_paths": [str(source)]},
        )
        self.assertFalse(created.is_error, created.content)
        batch_id = created.structured_content["batch_id"]
        paper_id = created.structured_content["paper_ids"][0]
        self.assertTrue(paths.paper_source_path(paper_id).is_file())

        status = await client.call_tool(
            "get_paper_import_batch", {"batch_id": batch_id}
        )
        self.assertFalse(status.is_error, status.content)
        self.assertEqual(status.structured_content["status"], "queued")
        self.assertEqual(status.structured_content["task_root_id"], "report_generation")

        repository = PaperImportRepository()
        repository.claim_next("mineru")
        repository.fail_stage(batch_id, paper_id, "mineru", "parse failed")
        retried = await client.call_tool(
            "retry_failed_paper_import_batch", {"batch_id": batch_id}
        )
        self.assertFalse(retried.is_error, retried.content)
        self.assertEqual(retried.structured_content["papers"][0]["retry_count"], 1)

    async def test_rejects_missing_path_and_missing_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(paths, "PAPERS_DIR", root / "papers"),
                patch.object(paths, "PAPER_IMPORTS_DIR", root / "paper_imports"),
                patch(
                    "paper_import_mcp.server.PaperImportWorker",
                    return_value=_IdleWorker(),
                ),
            ):
                async with Client(mcp) as client:
                    created = await client.call_tool(
                        "create_paper_import_batch",
                        {
                            "task_root_id": "task",
                            "pdf_paths": [str(root / "missing.pdf")],
                        },
                    )
                    self.assertTrue(created.is_error)
                    missing = await client.call_tool(
                        "get_paper_import_batch", {"batch_id": "not-found"}
                    )
                    self.assertTrue(missing.is_error)

    async def test_can_queue_an_already_stored_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(paths, "PAPERS_DIR", root / "papers"),
                patch.object(paths, "PAPER_IMPORTS_DIR", root / "paper_imports"),
                patch(
                    "paper_import_mcp.server.PaperImportWorker",
                    return_value=_IdleWorker(),
                ),
            ):
                source = paths.paper_source_path("paper_test")
                source.parent.mkdir(parents=True)
                source.write_bytes(b"%PDF-1.4\nexample")
                async with Client(mcp) as client:
                    result = await client.call_tool(
                        "create_paper_import_batch",
                        {"task_root_id": "task", "paper_ids": ["paper_test"]},
                    )
                    self.assertFalse(result.is_error, result.content)
                    self.assertEqual(
                        result.structured_content["paper_ids"], ["paper_test"]
                    )


if __name__ == "__main__":
    unittest.main()
