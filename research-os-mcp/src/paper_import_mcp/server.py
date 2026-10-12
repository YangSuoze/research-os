"""Standalone MCP entrypoint for paper import and read-only graph queries."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from paper_import_mcp.config import settings
from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.infra.elasticsearch import ElasticsearchPaperStore
from paper_import_mcp.infra.neo4j import GraphStore
from paper_import_mcp.models.paper_import import BatchAccepted, BatchOut
from paper_import_mcp.services.batches import PaperImportBatches
from paper_import_mcp.services.graph_query import GraphQueryService
from paper_import_mcp.services.paper_context import PaperContextService
from paper_import_mcp.services.paper_import import PaperImportService
from paper_import_mcp.web.paper_viewer import paper_pdf, paper_viewer
from paper_import_mcp.workers.paper_import import PaperImportWorker

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    batches: PaperImportBatches
    graph: GraphQueryService
    papers: PaperContextService


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    repository = PaperImportRepository()
    service = PaperImportService()
    worker = PaperImportWorker(repository, service)
    graph = GraphQueryService(GraphStore())
    papers = PaperContextService(ElasticsearchPaperStore())
    task = asyncio.create_task(worker.run_exclusive(), name="paper-import-worker")

    def report_failure(finished: asyncio.Task[None]) -> None:
        if not finished.cancelled() and (error := finished.exception()) is not None:
            logger.error("Paper import worker exited unexpectedly", exc_info=error)

    task.add_done_callback(report_failure)
    try:
        yield AppContext(PaperImportBatches(repository, service), graph, papers)
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            try:
                await graph.close()
            finally:
                await papers.close()


mcp = MCPServer("research-os-paper-import", lifespan=lifespan)
mcp.custom_route("/papers/{paper_id}/viewer", methods=["GET"])(paper_viewer)
mcp.custom_route("/papers/{paper_id}/source.pdf", methods=["GET", "HEAD"])(paper_pdf)


@mcp.tool()
async def create_paper_import_batch(
    task_root_id: str,
    ctx: Context[AppContext],
    pdf_paths: list[str] | None = None,
    paper_ids: list[str] | None = None,
) -> BatchAccepted:
    """Queue PDFs for MinerU, ES and Neo4j. Supply either server-side PDF paths or stored paper IDs. Returns immediately."""

    try:
        return await ctx.request_context.lifespan_context.batches.create(
            task_root_id, pdf_paths=pdf_paths, paper_ids=paper_ids
        )
    except (ValueError, FileExistsError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def get_paper_import_batch(batch_id: str, ctx: Context[AppContext]) -> BatchOut:
    """Read batch and per-paper progress, stage and last error."""

    try:
        return await ctx.request_context.lifespan_context.batches.get(batch_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def retry_failed_paper_import_batch(
    batch_id: str, ctx: Context[AppContext]
) -> BatchOut:
    """Requeue only failed papers, resuming from their failed stages."""

    try:
        return await ctx.request_context.lifespan_context.batches.retry_failed(batch_id)
    except (ValueError, FileExistsError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def search_graph(
    query: str,
    ctx: Context[AppContext],
    task_root_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Translate a research question to read-only Cypher and return cypher/records. Optional task_root_id guides paper-level task filtering. limit: 1-100."""
    try:
        return await ctx.request_context.lifespan_context.graph.search(
            query, task_root_id, limit
        )
    except Exception as exc:
        logger.warning("图谱检索失败：%s", exc)
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def browse_task(task_root_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Browse a task's method families, method summaries, problems and datasets. Read-only; no LLM call."""
    try:
        return await ctx.request_context.lifespan_context.graph.browse_task(
            task_root_id
        )
    except Exception as exc:
        logger.warning("任务浏览失败：task_root_id=%s error=%s", task_root_id, exc)
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def get_paper_context(
    paper_id: str,
    parse_version: str,
    ctx: Context[AppContext],
    chunk_nos: list[int] | None = None,
    block_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch exact ES evidence and full chunk context; no LLM. Supply chunk_nos or block_ids (max 50 each). If both are supplied, blocks must belong to those chunks. Returns chunks with unchanged content/llm_content, selected blocks with text, 1-based page_number, normalized top-left bbox, assets and viewer_url. HTTP links use this MCP server's port; stdio or missing PDFs return null URLs. Does not fall back to another parse_version."""
    try:
        request = ctx.request_context.request
        return await ctx.request_context.lifespan_context.papers.get(
            paper_id,
            parse_version,
            chunk_nos=chunk_nos,
            block_ids=block_ids,
            viewer_base_url=str(request.base_url) if request is not None else None,
        )
    except Exception as exc:
        logger.warning(
            "原文定位失败：paper_id=%s parse_version=%s error=%s",
            paper_id,
            parse_version,
            exc,
        )
        raise ToolError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Research OS paper-import MCP")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level)
    if args.transport == "streamable-http" and args.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        parser.error("HTTP is local-only until MCP authentication is implemented")
    try:
        if args.transport == "stdio":
            mcp.run()
        else:
            mcp.run(transport="streamable-http", host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
