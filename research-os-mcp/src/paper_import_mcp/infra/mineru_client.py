"""MinerU 4.x 的本地 PDF 解析适配器。

本模块只负责调用 MinerU 和保存其标准产物，不负责 HTTP、任务状态或数据库
持久化；这些职责应由 service 层编排。版面识别的结构化结果位于
``middle_json.json``，其中包含每一页的 ``blocks`` 与坐标信息。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MinerUUnavailableError(RuntimeError):
    """当前运行环境未安装或无法加载 MinerU。"""


class MinerUParseError(RuntimeError):
    """MinerU 未能完成指定 PDF 的解析。"""


class MinerUOutputError(MinerUParseError):
    """MinerU 输出目录缺少约定产物，或其中的 JSON 无效。"""


@dataclass(frozen=True, slots=True)
class MinerUParseResult:
    """一次成功解析产生的、可由 service 层保存到数据库的产物位置。"""

    source_pdf: Path
    output_dir: Path
    tier: str
    markdown_path: Path
    middle_json_path: Path
    structured_content_path: Path
    images_dir: Path
    middle_json: dict[str, Any] | None
    structured_content: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """返回可直接交给 FastAPI JSON 响应或 service 层持久化的字典。"""

        return {
            "source_pdf": str(self.source_pdf),
            "output_dir": str(self.output_dir),
            "tier": self.tier,
            "markdown_path": str(self.markdown_path),
            "middle_json_path": str(self.middle_json_path),
            "structured_content_path": str(self.structured_content_path),
            "images_dir": str(self.images_dir),
            "middle_json": self.middle_json,
            "structured_content": self.structured_content,
        }


class MinerUClient:
    """通过 MinerU Python SDK 进行本地、无状态的 PDF 解析。

    MinerU 是可选依赖，所以相关导入延后到真正开始解析时再执行；这使得
    不安装 ``pdf`` extra 的 API 进程仍可正常启动。

    ``output_dir`` 应由调用方为该 PDF 的解析结果单独指定。若目录已有完整的
    标准产物则直接复用，不再重复运行 MinerU；非空但产物不完整时抛出错误，
    避免将上次失败后留下的文件误判为成功结果。
    """

    def __init__(self, default_tier: str = "standard") -> None:
        if not default_tier.strip():
            raise ValueError("default_tier must not be blank")
        self._default_tier = default_tier

    def parse_pdf(
        self,
        pdf_path: str | Path,
        output_dir: str | Path,
        *,
        tier: str | None = None,
        include_json: bool = True,
    ) -> MinerUParseResult:
        """解析一个 PDF，或直接复用 output_dir 中已有的完整标准产物。

        返回的 ``middle_json_path`` 是后处理后的版面语义结果；
        ``structured_content_path`` 是供 ES 分块与 LLM 消费的简化内容。
        默认还会将这两份 JSON 反序列化为 ``middle_json`` 与
        ``structured_content`` 字段。若调用方只需要产物路径，可传
        ``include_json=False`` 避免把大型文档加载进内存。

        这是同步方法，适合后台 worker 调用。FastAPI 的异步调用方应使用
        :meth:`aparse_pdf`，以免阻塞事件循环。
        """

        source_pdf = self._validate_pdf(pdf_path)
        selected_tier = self._resolve_tier(tier)
        destination = self._prepare_output_dir(output_dir)

        if any(destination.iterdir()):
            return self._collect_result(
                source_pdf=source_pdf,
                output_dir=destination,
                tier=selected_tier,
                include_json=include_json,
            )

        try:
            parse, writer_type = self._load_runtime()
        except ImportError as exc:
            raise MinerUUnavailableError(
                "MinerU is unavailable. Install the optional dependency with "
                "`uv sync --extra pdf`."
            ) from exc

        try:
            # MinerU 4.x 的 Python SDK 对 PDF 默认解析全部页面。解析结果的
            # middle_json 保留版面 block、页码与 bbox，save() 会物化完整产物。
            parse_result = parse(str(source_pdf), tier=selected_tier)
            parse_result.save(writer_type(str(destination)))
        except Exception as exc:
            raise MinerUParseError(
                f"MinerU failed to parse PDF {source_pdf.name!r}: {exc}"
            ) from exc

        return self._collect_result(
            source_pdf=source_pdf,
            output_dir=destination,
            tier=selected_tier,
            include_json=include_json,
        )

    async def aparse_pdf(
        self,
        pdf_path: str | Path,
        output_dir: str | Path,
        *,
        tier: str | None = None,
        include_json: bool = True,
    ) -> MinerUParseResult:
        """在线程中执行 :meth:`parse_pdf`，避免阻塞异步 Web 服务。"""

        return await asyncio.to_thread(
            self.parse_pdf,
            pdf_path,
            output_dir,
            tier=tier,
            include_json=include_json,
        )

    @staticmethod
    def _load_runtime() -> tuple[Callable[..., Any], Callable[[str], Any]]:
        """延迟加载可选的 MinerU 依赖，供测试替换。"""

        from mineru.parser import parse
        from mineru.parser.writer import FileBasedDataWriter

        return parse, FileBasedDataWriter

    @staticmethod
    def _validate_pdf(pdf_path: str | Path) -> Path:
        source_pdf = Path(pdf_path).expanduser().resolve()
        if source_pdf.suffix.lower() != ".pdf":
            raise ValueError("MinerUClient accepts PDF files only")
        if not source_pdf.is_file():
            raise FileNotFoundError(f"PDF does not exist: {source_pdf}")
        return source_pdf

    @staticmethod
    def _prepare_output_dir(output_dir: str | Path) -> Path:
        destination = Path(output_dir).expanduser().resolve()
        if destination.exists() and not destination.is_dir():
            raise NotADirectoryError(
                f"MinerU output path is not a directory: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def _resolve_tier(self, tier: str | None) -> str:
        selected_tier = tier if tier is not None else self._default_tier
        if not selected_tier.strip():
            raise ValueError("tier must not be blank")
        return selected_tier

    @staticmethod
    def _collect_result(
        *,
        source_pdf: Path,
        output_dir: Path,
        tier: str,
        include_json: bool,
    ) -> MinerUParseResult:
        # MinerU 4.x ParseResult.save 的标准文件名，参见官方 result contract。
        markdown_path = output_dir / "markdown.md"
        middle_json_path = output_dir / "middle_json.json"
        structured_content_path = output_dir / "structured_content.json"
        missing = [
            path.name
            for path in (markdown_path, middle_json_path, structured_content_path)
            if not path.is_file()
        ]
        if missing:
            raise MinerUOutputError(
                "MinerU output directory is missing required output files: "
                + ", ".join(missing)
            )

        middle_json = (
            MinerUClient._read_json(middle_json_path) if include_json else None
        )
        structured_content = (
            MinerUClient._read_json(structured_content_path) if include_json else None
        )

        return MinerUParseResult(
            source_pdf=source_pdf,
            output_dir=output_dir,
            tier=tier,
            markdown_path=markdown_path,
            middle_json_path=middle_json_path,
            structured_content_path=structured_content_path,
            images_dir=output_dir / "images",
            middle_json=middle_json,
            structured_content=structured_content,
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MinerUOutputError(f"MinerU output is not valid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise MinerUOutputError(f"MinerU JSON output must be an object: {path}")
        return payload
