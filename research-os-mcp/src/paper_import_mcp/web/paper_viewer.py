"""只读 HTTP 页面和 PDF 文件；不开放任意文件路径参数。"""

from importlib.resources import files
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from paper_import_mcp import paths


def _source_pdf(request: Request) -> Path | None:
    # custom_route 不经过 MCP transport 的 Host 校验，单独阻止 DNS rebinding。
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("PDF viewer is local-only")
    return paths.stored_paper_source_path(request.path_params["paper_id"])


async def paper_viewer(request: Request) -> Response:
    try:
        source = _source_pdf(request)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    if source is None:
        return PlainTextResponse("Stored PDF not found", status_code=404)
    return HTMLResponse(files(__package__).joinpath("pdf_viewer.html").read_text())


async def paper_pdf(request: Request) -> Response:
    try:
        source = _source_pdf(request)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    if source is None:
        return PlainTextResponse("Stored PDF not found", status_code=404)
    return FileResponse(
        source,
        media_type="application/pdf",
        filename="source.pdf",
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )
