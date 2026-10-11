"""论文导入的文件管理与三个处理阶段；不依赖 HTTP 或任务调度器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from contextlib import aclosing
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from paper_import_mcp import paths
from paper_import_mcp.infra.elasticsearch import ElasticsearchPaperStore
from paper_import_mcp.infra.mineru_client import MinerUClient
from paper_import_mcp.models.paper_graph import PaperGraphDraft
from paper_import_mcp.services.paper_graph.code_link_finder import CodeLinkFinder
from paper_import_mcp.services.paper_graph.extractor import PaperGraphExtractor
from paper_import_mcp.services.paper_graph.method_guidelines import MethodGuidelines

logger = logging.getLogger(__name__)
_COPY_SIZE = 1024 * 1024


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _code_link_path(draft_path: Path) -> Path:
    return draft_path.with_name(f"{draft_path.stem}.code_link.json")


class PaperImportService:
    def __init__(self) -> None:
        # Neo4j 阶段全局串行，复用按需加载的本地向量模型。
        self._embedder = None
        self._guidelines = MethodGuidelines()

    def save_pdf(self, stream: BinaryIO, filename: str, batch_id: str) -> str:
        """流式保存上传文件；相同 PDF 共用一个 content-addressed source.pdf。"""

        if not filename.lower().endswith(".pdf"):
            raise ValueError("Only PDF files are accepted")
        temporary_dir = paths.paper_import_temp_dir(batch_id)
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / f"{uuid4().hex}.pdf"
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as target:
                first = True
                while data := stream.read(_COPY_SIZE):
                    if first and not data.startswith(b"%PDF-"):
                        raise ValueError("Uploaded file is not a PDF")
                    first = False
                    digest.update(data)
                    target.write(data)
                if first:
                    raise ValueError("Uploaded PDF is empty")
            paper_id = f"paper_{digest.hexdigest()}"
            destination = paths.paper_source_path(paper_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                existing = hashlib.sha256()
                with destination.open("rb") as source:
                    while data := source.read(_COPY_SIZE):
                        existing.update(data)
                if existing.digest() != digest.digest():
                    raise FileExistsError(f"Stored PDF is corrupt: {paper_id}")
            else:
                os.replace(temporary, destination)
            return paper_id
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _mineru_version() -> str:
        return version("mineru")

    async def run_mineru(self, paper_id: str) -> None:
        source = paths.paper_source_path(paper_id)
        output = paths.paper_mineru_dir(paper_id, self._mineru_version())
        client = MinerUClient()
        if output.exists():
            await client.aparse_pdf(source, output, tier="standard", include_json=False)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        try:
            await client.aparse_pdf(
                source, temporary, tier="standard", include_json=False
            )
            temporary.rename(output)
        finally:
            if temporary.exists():
                await asyncio.to_thread(shutil.rmtree, temporary)

    async def _get_method_guidelines(self, task_root_id: str) -> str:
        from paper_import_mcp.infra.llm import LLMClient
        from paper_import_mcp.infra.neo4j import GraphStore

        store = GraphStore()
        try:
            llm = LLMClient()
            try:
                return await self._guidelines.get_or_create(task_root_id, store, llm)
            finally:
                await llm.close()
        finally:
            await store.close()

    async def run_index_extract(self, paper_id: str, task_root_id: str) -> str:
        source = paths.paper_source_path(paper_id)
        output = paths.paper_mineru_dir(paper_id, self._mineru_version())
        parsed = await MinerUClient().aparse_pdf(source, output, include_json=True)
        assert parsed.middle_json is not None and parsed.structured_content is not None

        store = ElasticsearchPaperStore()
        try:
            indexed = await store.index_parsed_paper(
                paper_id=paper_id,
                middle_json=parsed.middle_json,
                structured_content=parsed.structured_content,
            )
            if indexed.chunk_count == 0:
                raise ValueError("MinerU output produced no searchable chunks")
            guidelines = await self._get_method_guidelines(task_root_id)
            extractor = PaperGraphExtractor(
                paper_store=store, method_guidelines=guidelines
            )
            try:
                draft = await extractor.extract(
                    paper_id=paper_id, parse_version=indexed.parse_version
                )
            finally:
                await extractor.close()
            draft_path = paths.paper_graph_draft_path(paper_id, indexed.parse_version)
            await asyncio.to_thread(
                _write_json, draft_path, draft.model_dump(mode="json")
            )

            # 源码链接是附加信息；GitHub 或核验模型不可用时不影响论文入库。
            link_path = _code_link_path(draft_path)
            link_path.unlink(missing_ok=True)
            try:
                content = await self._paper_excerpt(
                    store, paper_id, indexed.parse_version
                )
                async with CodeLinkFinder() as finder:
                    code_link = await finder.find(draft.paper_title, content)
                await asyncio.to_thread(_write_json, link_path, code_link)
            except Exception:
                logger.warning("代码链接搜索失败：paper_id=%s", paper_id, exc_info=True)
            return indexed.parse_version
        finally:
            await store.close()

    @staticmethod
    async def _paper_excerpt(
        store: ElasticsearchPaperStore, paper_id: str, parse_version: str
    ) -> str:
        parts: list[str] = []
        size = 0
        async with aclosing(
            store.iter_paper_chunks(paper_id=paper_id, parse_version=parse_version)
        ) as chunks:
            async for chunk in chunks:
                text = chunk.get("content", "")
                parts.append(text)
                size += len(text)
                if size >= 10_000:
                    break
        return "\n".join(parts)[:10_000]

    async def run_neo4j(
        self, paper_id: str, parse_version: str | None, task_root_id: str
    ) -> None:
        if not parse_version:
            raise ValueError("Cannot ingest a paper without parse_version")
        draft_path = paths.paper_graph_draft_path(paper_id, parse_version)
        draft = await asyncio.to_thread(
            lambda: PaperGraphDraft.model_validate_json(
                draft_path.read_text(encoding="utf-8")
            )
        )
        link_path = _code_link_path(draft_path)
        code_link = (
            json.loads(link_path.read_text(encoding="utf-8"))
            if link_path.is_file()
            else None
        )

        from paper_import_mcp.infra.embedding import BGEEmbedder
        from paper_import_mcp.infra.llm import LLMClient
        from paper_import_mcp.infra.neo4j import GraphStore
        from paper_import_mcp.services.paper_graph.aligner import GraphAligner
        from paper_import_mcp.services.paper_graph.ingester import PaperGraphIngester

        store = GraphStore()
        try:
            llm = LLMClient()
            try:
                guidelines = await self._guidelines.get_or_create(task_root_id, store, llm)
                if self._embedder is None:
                    self._embedder = BGEEmbedder()
                ingester = PaperGraphIngester(
                    store,
                    GraphAligner(
                        store, self._embedder, llm, method_guidelines=guidelines
                    ),
                )
                await ingester.ingest(
                    draft,
                    task_root_id,
                    code_link=code_link["url"] if code_link else None,
                )
            finally:
                await llm.close()
        finally:
            await store.close()
