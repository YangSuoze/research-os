"""Standalone MCP entrypoint for queued PDF import and graph ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from paper_import_mcp.config import settings
from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.models.paper_import import BatchAccepted, BatchOut
from paper_import_mcp.services.batches import PaperImportBatches
from paper_import_mcp.services.paper_import import PaperImportService
from paper_import_mcp.workers.paper_import import PaperImportWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[PaperImportBatches]:
    repository = PaperImportRepository()
    service = PaperImportService()
    worker = PaperImportWorker(repository, service)
    task = asyncio.create_task(worker.run_exclusive(), name="paper-import-worker")

    def report_failure(finished: asyncio.Task[None]) -> None:
        if not finished.cancelled() and (error := finished.exception()) is not None:
            logger.error("Paper import worker exited unexpectedly", exc_info=error)

    task.add_done_callback(report_failure)
    try:
        yield PaperImportBatches(repository, service)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


mcp = MCPServer("research-os-paper-import", lifespan=lifespan)


@mcp.tool()
async def create_paper_import_batch(
    task_root_id: str,
    ctx: Context[PaperImportBatches],
    pdf_paths: list[str] | None = None,
    paper_ids: list[str] | None = None,
) -> BatchAccepted:
    """Queue PDFs for MinerU, ES and Neo4j. Supply either server-side PDF paths or stored paper IDs. Returns immediately."""

    try:
        return await ctx.request_context.lifespan_context.create(
            task_root_id, pdf_paths=pdf_paths, paper_ids=paper_ids
        )
    except (ValueError, FileExistsError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def get_paper_import_batch(
    batch_id: str, ctx: Context[PaperImportBatches]
) -> BatchOut:
    """Read batch and per-paper progress, stage and last error."""

    try:
        return await ctx.request_context.lifespan_context.get(batch_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def retry_failed_paper_import_batch(
    batch_id: str, ctx: Context[PaperImportBatches]
) -> BatchOut:
    """Requeue only failed papers, resuming from their failed stages."""

    try:
        return await ctx.request_context.lifespan_context.retry_failed(batch_id)
    except (ValueError, FileExistsError) as exc:
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
