"""The copied worker retains the backend's 1/N/1 queue semantics."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.workers.paper_import import PaperImportWorker


class _FakeService:
    def __init__(self) -> None:
        self.active = {name: 0 for name in ("mineru", "index_extract", "neo4j")}
        self.maximum = self.active.copy()

    async def _stage(self, name: str) -> None:
        self.active[name] += 1
        self.maximum[name] = max(self.maximum[name], self.active[name])
        try:
            await asyncio.sleep(0.02 if name == "index_extract" else 0.005)
        finally:
            self.active[name] -= 1

    async def run_mineru(self, _paper_id: str) -> None:
        await self._stage("mineru")

    async def run_index_extract(self, _paper_id: str, task_root_id: str) -> str:
        assert task_root_id == "report_generation"
        await self._stage("index_extract")
        return "mineru-4.0.5:chunker-v1"

    async def run_neo4j(
        self, _paper_id: str, parse_version: str, task_root_id: str
    ) -> None:
        assert parse_version == "mineru-4.0.5:chunker-v1"
        assert task_root_id == "report_generation"
        await self._stage("neo4j")


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PaperImportRepository(Path(directory) / "jobs.sqlite")
            repository.create_batch(
                "batch_1",
                "report_generation",
                [(f"paper_{number}", f"{number}.pdf") for number in range(4)],
            )
            service = _FakeService()
            await PaperImportWorker(
                repository, service, extract_concurrency=2
            ).run_until_idle()
            self.assertEqual(repository.get_batch("batch_1").status, "succeeded")
            self.assertLessEqual(service.maximum["mineru"], 1)
            self.assertEqual(service.maximum["index_extract"], 2)
            self.assertLessEqual(service.maximum["neo4j"], 1)


if __name__ == "__main__":
    unittest.main()
