"""从 ES 原始字符区间还原证据，并返回同端口 PDF 查看链接；不调用 LLM。"""

from __future__ import annotations

import logging
import re
from contextlib import aclosing
from typing import Any
from urllib.parse import quote, urlencode

from paper_import_mcp import paths
from paper_import_mcp.infra.elasticsearch import ElasticsearchPaperStore

logger = logging.getLogger(__name__)
_BLOCK_ID = re.compile(r"p(\d+)-b(\d+)")
_MAX_REFERENCES = 50


class PaperContextService:
    def __init__(self, store: ElasticsearchPaperStore) -> None:
        self.store = store

    async def search(
        self,
        query: str,
        *,
        paper_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """只做 BM25 召回；结果保留解析版本，完整原文及 PDF 定位由 get 提供。"""
        hits = await self.store.search_bm25(
            query.strip(), paper_ids=paper_ids, size=limit
        )
        results = [
            {
                "paper_id": hit.source["paper_id"],
                "parse_version": hit.source["parse_version"],
                "chunk_no": hit.source["chunk_no"],
                "paper_title": hit.source["paper_title"],
                "section_path": hit.source["section_path"],
                "content_type": hit.source["content_type"],
                "score": hit.score,
                "content_preview": "\n...\n".join(
                    hit.highlights.get("content") or [hit.source["content"][:500]]
                ),
            }
            for hit in hits
        ]
        logger.info("论文正文检索：query=%r hits=%d", query, len(results))
        return {"hits": results}

    async def get(
        self,
        paper_id: str,
        parse_version: str,
        *,
        chunk_nos: list[int] | None = None,
        block_ids: list[str] | None = None,
        viewer_base_url: str | None = None,
    ) -> dict[str, Any]:
        """返回完整 chunk 上下文，blocks 只包含指定证据（未指定则为全部）。

        只给 block_ids 时先按页范围读 ES，再核对原始 block 索引。两种定位
        同时提供时，指定 block 必须在指定 chunks 内。不查找其他解析版本。
        """
        paper_id, parse_version = paper_id.strip(), parse_version.strip()
        if not paper_id or not parse_version:
            raise ValueError("paper_id and parse_version must not be blank")
        if not chunk_nos and not block_ids:
            raise ValueError("Provide chunk_nos or block_ids")
        if max(len(chunk_nos or []), len(block_ids or [])) > _MAX_REFERENCES:
            raise ValueError(
                f"At most {_MAX_REFERENCES} chunk_nos or block_ids per call"
            )
        if any(type(number) is not int or number < 1 for number in chunk_nos or []):
            raise ValueError("chunk_nos must be positive integers")
        selected_blocks = set(block_ids or [])
        pages = set()
        for block_id in selected_blocks:
            match = _BLOCK_ID.fullmatch(block_id)
            if match is None:
                raise ValueError(f"Invalid block_id: {block_id!r}; expected p3-b4")
            pages.add(int(match[1]))
        requested_chunks = sorted(set(chunk_nos or []))
        source = paths.stored_paper_source_path(paper_id)
        viewer_root = (
            f"{viewer_base_url.rstrip('/')}/papers/{quote(paper_id, safe='')}"
            if viewer_base_url and source is not None
            else None
        )

        chunks: list[dict[str, Any]] = []
        found_chunks: set[int] = set()
        found_blocks: set[str] = set()
        title = paper_id
        async with aclosing(
            self.store.iter_paper_chunks(
                paper_id=paper_id,
                parse_version=parse_version,
                chunk_nos=requested_chunks or None,
                page_indices=sorted(pages) if not requested_chunks else None,
            )
        ) as documents:
            async for document in documents:
                found_chunks.add(document["chunk_no"])
                title = document["paper_title"]
                blocks = []
                for block in document["source_blocks"]:
                    block_id = f"p{block['page_idx']}-b{block['block_index']}"
                    if selected_blocks and block_id not in selected_blocks:
                        continue
                    found_blocks.add(block_id)
                    start, end = block["content_start"], block["content_end"]
                    if not 0 <= start <= end <= len(document["content"]):
                        raise ValueError(
                            f"Invalid stored content offsets for {block_id}"
                        )
                    page_number = block["page_idx"] + 1
                    bbox = block.get("bbox")
                    parameters = {
                        "page_number": page_number,
                        "parse_version": parse_version,
                        "block_id": block_id,
                    }
                    if bbox is not None:
                        parameters["bbox"] = ",".join(str(value) for value in bbox)
                    blocks.append(
                        {
                            "block_id": block_id,
                            "type": block["type"],
                            "text": document["content"][start:end],
                            "page_number": page_number,
                            "bbox": bbox,
                            "viewer_url": (
                                f"{viewer_root}/viewer?{urlencode(parameters)}"
                                if viewer_root
                                else None
                            ),
                        }
                    )
                if blocks or requested_chunks:
                    chunks.append(
                        {
                            "chunk_no": document["chunk_no"],
                            "section_path": document["section_path"],
                            "content_type": document["content_type"],
                            "content": document["content"],
                            "llm_content": document["llm_content"],
                            "blocks": blocks,
                            "assets": document["assets"],
                        }
                    )
        missing_chunks = set(requested_chunks) - found_chunks
        missing_blocks = selected_blocks - found_blocks
        if missing_chunks or missing_blocks:
            raise ValueError(
                f"References not found in {paper_id}:{parse_version}: "
                f"chunk_nos={sorted(missing_chunks)}, block_ids={sorted(missing_blocks)}"
            )
        logger.info(
            "原文定位：paper_id=%s parse_version=%s chunks=%d blocks=%d",
            paper_id,
            parse_version,
            len(chunks),
            sum(len(chunk["blocks"]) for chunk in chunks),
        )
        return {
            "paper_id": paper_id,
            "parse_version": parse_version,
            "paper_title": title,
            "pdf_path": str(source) if source else None,
            "pdf_url": f"{viewer_root}/source.pdf" if viewer_root else None,
            "bbox_coordinate_system": "normalized_top_left",
            "chunks": chunks,
        }

    async def close(self) -> None:
        await self.store.close()
