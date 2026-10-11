"""按证据承载类型，从一篇论文的 ES chunks 中选出值得抽取的 chunk_no。

四类 claim（mechanism、addresses、result、limitation）和 Dataset 需要原文
证据，因此才是本选择器的 kind。TaskRoot 由用户指定；MethodFamily、
MethodLeaf 从 mechanism 抽取结果中归类；Problem 无需单独选择证据。
只把章节路径和有限的正文预览送给 LLM；不在这里抽取图谱节点，也不修改 ES。
MinerU 的章节层级尚未校准，因此提示词特别要求参考路径末段，而不能把
``Abstract > 1 Introduction`` 误认为摘要内容。
"""

from __future__ import annotations

import json
from contextlib import aclosing
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from paper_import_mcp.config import settings
from paper_import_mcp.infra.elasticsearch import ElasticsearchPaperStore
from paper_import_mcp.infra.llm import LLMClient

_PREVIEW_CHARACTERS = 320
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_BASE_PROMPT = """请你选择可能包含目标知识或必要分类上下文的 chunk。
用户消息包含 按 chunk_no 排序的 chunks。每个 chunk 只提供 section_path、section_heading、content_type 和正文开头的短预览。
其中每个字段含义如下：
- section_path 为改chunk对应的文章层级。其层级可能有误，例如“Abstract > 1 Introduction”其实是 Introduction，具体根据你的理解和常识来判断。
- section_heading 为优section_path对应的最后一层标题。优先参考 section_heading 与预览为准。
- content_type举例其中text表示只包含普通正文 block；paragraph_title表示只包含段落或章节标题 block；ref_text表示只包含参考文献条目 block；code表示只包含代码 block；table表示只包含表格 block；mixed表示混合了不同类型，例如标题＋正文、正文＋图表。其他类型可类似根据名称进行推测，为mineru解析结果。
只返回 JSON 格式的相关chunk标号：{"chunk_nos": [按升序排列的整数]}。
Output language: Use English for any generated natural-language text, even when the input is in another language.
Keep JSON keys, chunk_no values, section headings and source identifiers unchanged; do not add explanations."""

_KIND_PROMPTS = {
    "mechanism": "目标是选出和 mechanism claim 相关的chunks：选择本文实际提出或采用的方法，如架构、算法、损失函数及实现细节等，重点看 Method/Approach 类似 和 Related Work 章节相关的chunks。",
    "addresses": "目标是选出和 addresses claim 相关的chunks：选择明确说明本文机制解决哪个问题、怎样解决的，主要是摘要、引言、问题定义和方法动机段落对应的chunks",
    "result": "目标是选出和 result claim 相关的chunks：选择本文实验结果、定量指标、定性分析、表格与图注等，以及理解这些结果必需的实验设置。",
    "limitation": "目标是选出和 limitation claim 相关的chunks：选择本文方法的失败场景、适用边界、消融分析、讨论、局限或结论。",
    "Dataset": "目标是选出使用的 Dataset 相关的chunks：选择本文实际使用、构建或评估的数据集及其设置。",
}


class SectionSelectionError(RuntimeError):
    """论文 chunk 或 LLM 返回的 chunk 编号不可信。"""


class _StoredChunk(BaseModel):
    model_config = ConfigDict(strict=True)

    paper_id: NonBlank
    parse_version: NonBlank
    chunk_no: int = Field(gt=0)
    paper_title: NonBlank
    section_path: str
    content_type: str
    content: str = Field(min_length=1)


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chunk_nos: list[Annotated[int, Field(strict=True, gt=0)]]


class SectionSelector:
    """用 kind 专属提示词选 chunk；返回编号供后续抽取器读取完整 llm_content。"""

    def __init__(
        self,
        *,
        paper_store: ElasticsearchPaperStore | None = None,
        llm_client: LLMClient | None = None,
        model_name: str = settings.model_name,
    ) -> None:
        self._store = (
            paper_store if paper_store is not None else ElasticsearchPaperStore()
        )
        self._llm = llm_client
        self._owns_store = paper_store is None
        self._owns_llm = llm_client is None
        self.model_name = model_name

    async def select(
        self,
        *,
        paper_id: str,
        parse_version: str,
        kind: str,
        page_size: int = 100,
    ) -> list[int]:
        """返回目标 kind 对应的 chunk_no，升序且去重；无相关内容返回 []。"""

        if kind not in _KIND_PROMPTS:
            raise ValueError(f"Unsupported selection kind: {kind}")
        if not paper_id.strip() or not parse_version.strip():
            raise ValueError("paper_id and parse_version must not be blank")

        chunks = await self._read_chunks(paper_id, parse_version, page_size)
        metadata = [
            {
                "chunk_no": chunk.chunk_no,
                "section_path": chunk.section_path,
                "section_heading": chunk.section_path.rsplit(" > ", 1)[-1],
                "content_type": chunk.content_type,
                "content_preview": chunk.content[:_PREVIEW_CHARACTERS],
            }
            for chunk in chunks
        ]
        messages = [
            {
                "role": "system",
                "content": _BASE_PROMPT + "\n" + _KIND_PROMPTS[kind],
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "paper_id": paper_id,
                        "parse_version": parse_version,
                        "kind": kind,
                        "chunks": metadata,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        if self._llm is None:
            self._llm = LLMClient()
        response = await self._llm.aquery(
            messages,
            model_name=self.model_name,
            temperature=0.0,
            json_mode=_Selection,
        )
        try:
            selected = _Selection.model_validate_json(response).chunk_nos
        except ValidationError as exc:
            raise SectionSelectionError(
                "LLM returned invalid chunk selection JSON"
            ) from exc
        available = {chunk.chunk_no for chunk in chunks}
        if len(selected) != len(set(selected)) or not set(selected) <= available:
            raise SectionSelectionError(
                "LLM selected duplicate or nonexistent chunk_no"
            )
        return sorted(selected)

    async def _read_chunks(
        self, paper_id: str, parse_version: str, page_size: int
    ) -> list[_StoredChunk]:
        chunks: list[_StoredChunk] = []
        seen: set[int] = set()
        stream = self._store.iter_paper_chunks(
            paper_id=paper_id, parse_version=parse_version, page_size=page_size
        )
        async with aclosing(stream):
            async for source in stream:
                try:
                    chunk = _StoredChunk.model_validate(source)
                except ValidationError as exc:
                    raise SectionSelectionError("Invalid paper chunk in ES") from exc
                if chunk.paper_id != paper_id or chunk.parse_version != parse_version:
                    raise SectionSelectionError(
                        "Chunk paper_id or parse_version mismatch"
                    )
                if chunk.chunk_no in seen:
                    raise SectionSelectionError(f"Duplicate chunk_no: {chunk.chunk_no}")
                chunks.append(chunk)
                seen.add(chunk.chunk_no)
        if not chunks:
            raise SectionSelectionError(
                "No chunks found for this paper and parse_version"
            )
        if len({chunk.paper_title for chunk in chunks}) != 1:
            raise SectionSelectionError("Inconsistent paper_title across chunks")
        return sorted(chunks, key=lambda chunk: chunk.chunk_no)

    async def close(self) -> None:
        try:
            if self._owns_store:
                await self._store.close()
        finally:
            if self._owns_llm and self._llm is not None:
                await self._llm.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


async def select_section_chunks(
    paper_id: str,
    parse_version: str,
    kind: str,
    *,
    model_name: str = settings.model_name,
) -> list[int]:
    """便捷入口：返回可用于后续按编号读取完整正文的 chunk_no 列表。"""

    async with SectionSelector(model_name=model_name) as selector:
        return await selector.select(
            paper_id=paper_id, parse_version=parse_version, kind=kind
        )


async def _smoke_test(kind: str) -> None:
    """使用已入库的 test-paper，实际调用 ES 和 LLM 验证选择流程。"""

    paper_id = "test-paper"
    parse_version = "mineru-4.0.5:chunker-v1"
    kinds = _KIND_PROMPTS if kind == "all" else (kind,)
    async with SectionSelector() as selector:
        for selected_kind in kinds:
            chunk_nos = await selector.select(
                paper_id=paper_id,
                parse_version=parse_version,
                kind=selected_kind,
            )
            print(
                json.dumps(
                    {"kind": selected_kind, "chunk_nos": chunk_nos},
                    ensure_ascii=False,
                )
            )
