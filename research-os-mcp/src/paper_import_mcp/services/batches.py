"""Queue paper imports without depending on FastAPI or the MCP protocol."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from paper_import_mcp import paths
from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.models.paper_import import BatchAccepted, BatchOut
from paper_import_mcp.services.paper_import import PaperImportService


class PaperImportBatches:
    def __init__(
        self,
        repository: PaperImportRepository,
        service: PaperImportService,
    ) -> None:
        self.repository = repository
        self.service = service

    async def create(
        self,
        task_root_id: str,
        *,
        pdf_paths: list[str] | None = None,
        paper_ids: list[str] | None = None,
    ) -> BatchAccepted:
        if bool(pdf_paths) == bool(paper_ids):
            raise ValueError("Provide either pdf_paths or paper_ids")
        batch_id = uuid4().hex
        papers: dict[str, str] = {}

        for raw_path in pdf_paths or []:
            pdf_path = Path(raw_path).expanduser().resolve()
            if not pdf_path.is_file():
                raise ValueError(f"PDF does not exist on the MCP host: {raw_path}")
            paper_id = await asyncio.to_thread(self._save_path, pdf_path, batch_id)
            papers.setdefault(paper_id, pdf_path.name)

        for paper_id in paper_ids or []:
            source = paths.paper_source_path(paper_id)
            if not source.is_file():
                raise ValueError(f"Stored PDF not found: {paper_id}")
            papers.setdefault(paper_id, source.name)

        await asyncio.to_thread(
            self.repository.create_batch,
            batch_id,
            task_root_id,
            list(papers.items()),
        )
        return BatchAccepted(batch_id=batch_id, paper_ids=list(papers))

    def _save_path(self, path: Path, batch_id: str) -> str:
        with path.open("rb") as stream:
            return self.service.save_pdf(stream, path.name, batch_id)

    async def get(self, batch_id: str) -> BatchOut:
        batch = await asyncio.to_thread(self.repository.get_batch, batch_id)
        if batch is None:
            raise ValueError(f"Batch not found: {batch_id}")
        return batch

    async def retry_failed(self, batch_id: str) -> BatchOut:
        count = await asyncio.to_thread(self.repository.retry_failed, batch_id)
        if count is None:
            raise ValueError(f"Batch not found: {batch_id}")
        if count == 0:
            raise ValueError(f"Batch has no failed papers: {batch_id}")
        return await self.get(batch_id)
