"""Elasticsearch 中论文 chunk 的创建、写入与 BM25 检索。

该模块只保存可检索文本和来源定位信息。MinerU 图片继续留在 artifacts 的
``images/`` 目录，ES 仅保存相对路径、caption 与 bbox，绝不保存 Base64 或
图片二进制。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from paper_import_mcp.config import settings

PAPER_CHUNKS_INDEX = "paper_chunks_v1"
CHUNKER_VERSION = "v1"
_IGNORED_BLOCK_TYPES = {"aside_text", "footer", "header", "page_number"}
_HEADING_BLOCK_TYPES = {"doc_title", "paragraph_title"}
_VISUAL_BLOCK_TYPES = {"image", "table", "chart"}

# ES 默认相似度即 BM25；本版本不声明 dense_vector，也不写入 embedding。
PAPER_CHUNKS_MAPPING: dict[str, Any] = {
    "dynamic": "strict",
    "properties": {
        "paper_id": {"type": "keyword"},
        "parse_version": {"type": "keyword"},
        "chunk_no": {"type": "integer"},
        "paper_title": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
        },
        "section_path": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
        },
        "content_type": {"type": "keyword"},
        "content": {"type": "text"},
        # LLM 专用上下文：在每个 MinerU block 前插入定位标签。保留在 _source，
        # 但不参与 BM25，避免 p12-b7 这类标签影响检索评分。
        "llm_content": {"type": "text", "index": False},
        "page_start": {"type": "integer"},
        "page_end": {"type": "integer"},
        # 保留在 _source 供命中后跳回 PDF；不创建大量动态字段，也不参与检索。
        "source_blocks": {"type": "object", "enabled": False},
        "assets": {"type": "object", "enabled": False},
        "indexed_at": {"type": "date"},
    },
}


class ElasticsearchIndexError(RuntimeError):
    """Elasticsearch 索引创建、写入或读取失败。"""


@dataclass(frozen=True, slots=True)
class IndexedPaper:
    paper_id: str
    parse_version: str
    chunk_count: int
    document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Bm25Hit:
    document_id: str
    score: float
    source: dict[str, Any]
    highlights: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class _ChunkUnit:
    content: str
    source_block: dict[str, Any]
    asset: dict[str, Any] | None
    section_path: tuple[str, ...]


class ElasticsearchPaperStore:
    """论文 chunk 的异步 Elasticsearch 存储。

    ``index_parsed_paper`` 接收 MinerU 的 ``middle_json`` 和
    ``structured_content`` 字典。它按原始阅读顺序将 block 内联到 ``content``，
    并生成带 ``[block: p{page_idx}-b{block_index}]`` 标签的 ``llm_content``。
    来源信息同时以字符区间 + page/block/bbox 保存，供命中后精确回溯。
    网络操作使用 ``await``，分页读取使用 ``async for``；使用完毕后须
    ``await close()`` 释放客户端连接。
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        url: str | None = None,
        index_name: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.index_name = (
            index_name or settings.elasticsearch_index or PAPER_CHUNKS_INDEX
        )
        if not self.index_name.strip():
            raise ValueError("Elasticsearch index_name must not be blank")
        self._client = client or self._build_client(
            url=url or settings.elasticsearch_url,
            username=(
                username if username is not None else settings.elasticsearch_username
            ),
            password=(
                password if password is not None else settings.elasticsearch_password
            ),
        )

    @staticmethod
    def _build_client(*, url: str, username: str | None, password: str | None) -> Any:
        if not url.strip():
            raise ValueError("Elasticsearch url must not be blank")
        from elasticsearch import AsyncElasticsearch

        options: dict[str, Any] = {"request_timeout": 30}
        if username or password:
            if not username or not password:
                raise ValueError(
                    "Elasticsearch username and password must be configured together"
                )
            options["basic_auth"] = (username, password)
        return AsyncElasticsearch(url, **options)

    async def close(self) -> None:
        await self._client.close()

    async def initialize_index(self) -> bool:
        """幂等创建 chunk 索引；返回 True 表示本次确实创建。"""

        if await self._client.indices.exists(index=self.index_name):
            return False
        await self._client.indices.create(
            index=self.index_name, mappings=PAPER_CHUNKS_MAPPING
        )
        return True

    async def index_parsed_paper(
        self,
        *,
        paper_id: str,
        middle_json: Mapping[str, Any],
        structured_content: Mapping[str, Any],
        paper_title: str | None = None,
        max_characters: int = 3_500,
        refresh: bool = True,
    ) -> IndexedPaper:
        """由 MinerU 产物构建 chunk 后，以 Bulk API 写入 Elasticsearch。

        ``max_characters`` 是初版的保守上限。切分仅发生在 MinerU block 的
        边界，因此不会从正文句子、表格或图片 caption 中间切开。
        """

        if not paper_id.strip():
            raise ValueError("paper_id must not be blank")
        if max_characters < 256:
            raise ValueError("max_characters must be at least 256")

        await self.initialize_index()
        documents = _build_chunk_documents(
            paper_id=paper_id,
            middle_json=middle_json,
            structured_content=structured_content,
            paper_title=paper_title,
            max_characters=max_characters,
        )
        await _bulk_index(
            client=self._client,
            index_name=self.index_name,
            documents=documents,
            refresh=refresh,
        )
        parse_version = (
            documents[0]["parse_version"] if documents else _parse_version(middle_json)
        )
        return IndexedPaper(
            paper_id=paper_id,
            parse_version=parse_version,
            chunk_count=len(documents),
            document_ids=tuple(
                _document_id(
                    paper_id=document["paper_id"],
                    parse_version=document["parse_version"],
                    chunk_no=document["chunk_no"],
                )
                for document in documents
            ),
        )

    async def iter_paper_chunks(
        self,
        *,
        paper_id: str,
        parse_version: str,
        chunk_nos: list[int] | None = None,
        page_indices: list[int] | None = None,
        page_size: int = 100,
        keep_alive: str = "5m",
    ) -> AsyncGenerator[dict[str, Any], None]:
        """按论文和解析版本分页读取全部 chunk，按 chunk_no 升序逐条返回。

        返回完整的 ES ``_source``，包含 ``llm_content``、来源 block 和图片
        引用。PIT 固定读取快照，search_after 避免深分页的结果窗口限制。
        ``keep_alive`` 是两次分页请求之间的快照保留时间，每页请求会续期。

        读取完毕、查询异常或调用生成器的 ``aclose()`` 时释放 PIT。提前结束
        迭代时，调用方应使用 ``contextlib.aclosing`` 或显式 ``await aclose()``。

        可按 chunk 编号或经过指定 page_idx 的 chunk 筛选；省略时读取全文。
        source_blocks 未索引，按 block 定位时先用页范围召回，再由服务核对。
        """

        if not paper_id.strip():
            raise ValueError("paper_id must not be blank")
        if not parse_version.strip():
            raise ValueError("parse_version must not be blank")
        if not 1 <= page_size <= 10_000:
            raise ValueError("page_size must be between 1 and 10000")
        if not keep_alive.strip():
            raise ValueError("keep_alive must not be blank")

        filters: list[dict[str, Any]] = [
            {"term": {"paper_id": paper_id}},
            {"term": {"parse_version": parse_version}},
        ]
        if chunk_nos is not None:
            filters.append({"terms": {"chunk_no": chunk_nos}})
        if page_indices is not None:
            filters.append(
                {
                    "bool": {
                        "should": [
                            {
                                "bool": {
                                    "filter": [
                                        {"range": {"page_start": {"lte": page}}},
                                        {"range": {"page_end": {"gte": page}}},
                                    ]
                                }
                            }
                            for page in sorted(set(page_indices))
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        opened = _response_body(
            await self._client.open_point_in_time(
                index=self.index_name,
                keep_alive=keep_alive,
                allow_partial_search_results=False,
            )
        )
        pit_id = opened["id"]
        search_after: list[Any] | None = None
        try:
            while True:
                options: dict[str, Any] = {
                    "size": page_size,
                    "pit": {"id": pit_id, "keep_alive": keep_alive},
                    "query": {"bool": {"filter": filters}},
                    "sort": [{"chunk_no": "asc"}, {"_shard_doc": "asc"}],
                    "track_total_hits": False,
                    "allow_partial_search_results": False,
                }
                if search_after is not None:
                    options["search_after"] = search_after
                response = _response_body(await self._client.search(**options))
                # ES 搜索可能返回新的 PIT ID，后续请求和释放均使用最新 ID。
                pit_id = response.get("pit_id", pit_id)
                if response.get("timed_out"):
                    raise ElasticsearchIndexError(
                        "Elasticsearch chunk search timed out"
                    )
                if response.get("_shards", {}).get("failed", 0):
                    raise ElasticsearchIndexError(
                        "Elasticsearch chunk search has failed shards"
                    )
                hits = response["hits"]["hits"]
                if not hits:
                    return
                search_after = hits[-1]["sort"]
                for hit in hits:
                    yield dict(hit["_source"])
                if len(hits) < page_size:
                    return
        finally:
            await self._client.close_point_in_time(id=pit_id)

    async def search_bm25(
        self,
        query: str,
        *,
        paper_id: str | None = None,
        size: int = 10,
    ) -> list[Bm25Hit]:
        """使用 Elasticsearch 默认 BM25 对论文 chunk 做关键词检索。"""

        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= size <= 100:
            raise ValueError("size must be between 1 and 100")

        filters: list[dict[str, Any]] = []
        if paper_id is not None:
            filters.append({"term": {"paper_id": paper_id}})

        response = await self._client.search(
            index=self.index_name,
            size=size,
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": [
                                    "content^4",
                                    "paper_title^3",
                                    "section_path^2",
                                ],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": filters,
                }
            },
            highlight={
                "fields": {
                    "content": {"fragment_size": 180, "number_of_fragments": 2},
                }
            },
        )
        payload = _response_body(response)
        return [
            Bm25Hit(
                document_id=hit["_id"],
                score=float(hit.get("_score") or 0),
                source=dict(hit.get("_source") or {}),
                highlights=dict(hit.get("highlight") or {}),
            )
            for hit in payload.get("hits", {}).get("hits", [])
        ]


def _build_chunk_documents(
    *,
    paper_id: str,
    middle_json: Mapping[str, Any],
    structured_content: Mapping[str, Any],
    paper_title: str | None,
    max_characters: int,
) -> list[dict[str, Any]]:
    units, inferred_title = _build_units(middle_json, structured_content)
    title = paper_title or inferred_title or paper_id
    parse_version = _parse_version(middle_json)

    documents: list[dict[str, Any]] = []
    current: list[_ChunkUnit] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if not current:
            return
        documents.append(
            _make_chunk_document(
                paper_id=paper_id,
                parse_version=parse_version,
                paper_title=title,
                chunk_no=len(documents) + 1,
                units=current,
            )
        )
        current = []
        current_size = 0

    for unit in units:
        is_heading = unit.source_block["type"] in _HEADING_BLOCK_TYPES
        # 一级/二级标题是下一段内容的起点，chunk 不能横跨章节。
        if is_heading and current:
            flush()
        required = len(unit.content) + (2 if current else 0)
        if current and current_size + required > max_characters:
            flush()
        current.append(unit)
        current_size += len(unit.content) + (2 if len(current) > 1 else 0)

    flush()
    return documents


def _build_units(
    middle_json: Mapping[str, Any],
    structured_content: Mapping[str, Any],
) -> tuple[list[_ChunkUnit], str | None]:
    middle_pages = _pages_by_index(middle_json, name="middle_json")
    structured_pages = _pages_by_index(structured_content, name="structured_content")
    if set(middle_pages) != set(structured_pages):
        raise ValueError(
            "middle_json and structured_content do not contain the same pages"
        )

    units: list[_ChunkUnit] = []
    section_path: list[str] = []
    inferred_title: str | None = None
    for page_idx in sorted(structured_pages):
        middle_blocks = middle_pages[page_idx]
        structured_blocks = structured_pages[page_idx]
        if len(middle_blocks) != len(structured_blocks):
            raise ValueError(
                f"page {page_idx} has different block counts in MinerU outputs"
            )
        for ordinal, (middle_block, structured_block) in enumerate(
            zip(middle_blocks, structured_blocks, strict=True)
        ):
            block_type = structured_block.get("type")
            if block_type != middle_block.get("type"):
                raise ValueError(
                    f"page {page_idx} block {ordinal} type differs between MinerU outputs"
                )
            if block_type in _IGNORED_BLOCK_TYPES:
                continue

            content = _render_block_content(structured_block)
            if not content:
                continue
            if block_type in _HEADING_BLOCK_TYPES:
                heading = _plain_content(structured_block.get("content"))
                if heading:
                    if block_type == "doc_title":
                        section_path = [heading]
                        inferred_title = inferred_title or heading
                    else:
                        # section_path[0] 始终是论文标题；MinerU 的 level=1
                        # 表示第一级正文标题，因此保留标题后追加该层级。
                        level = structured_block.get("level")
                        if isinstance(level, int) and level >= 1:
                            section_path = section_path[:level]
                        else:
                            section_path = section_path[:1]
                        section_path.append(heading)

            source_block = {
                "page_idx": page_idx,
                "block_index": middle_block.get("index", ordinal),
                "type": block_type,
                "bbox": middle_block.get("bbox"),
            }
            asset = _asset_from_block(
                structured_block, page_idx, source_block["block_index"]
            )
            units.append(
                _ChunkUnit(
                    content=content,
                    source_block=source_block,
                    asset=asset,
                    section_path=tuple(section_path),
                )
            )
    return units, inferred_title


def _pages_by_index(
    payload: Mapping[str, Any], *, name: str
) -> dict[int, list[Mapping[str, Any]]]:
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise ValueError(f"{name}.pages must be a list")
    result: dict[int, list[Mapping[str, Any]]] = {}
    for page in pages:
        if not isinstance(page, Mapping):
            raise ValueError(f"{name}.pages entries must be objects")
        page_idx = page.get("page_idx")
        blocks = page.get("blocks")
        if not isinstance(page_idx, int) or not isinstance(blocks, list):
            raise ValueError(f"{name}.pages entries must contain page_idx and blocks")
        if page_idx in result:
            raise ValueError(f"{name} contains duplicate page_idx {page_idx}")
        if not all(isinstance(block, Mapping) for block in blocks):
            raise ValueError(f"{name} page {page_idx} contains a non-object block")
        result[page_idx] = blocks
    return result


def _render_block_content(block: Mapping[str, Any]) -> str:
    block_type = block.get("type")
    content = _plain_content(block.get("content"))
    captions = _annotation_text(block.get("captions"))
    footnotes = _annotation_text(block.get("footnotes"))

    parts: list[str] = []
    if block_type == "doc_title":
        parts.append(f"# {content}" if content else "")
    elif block_type == "paragraph_title":
        parts.append(f"## {content}" if content else "")
    elif content:
        parts.append(content)
    # 图片可能没有 OCR 文字和 caption，仍须在阅读顺序中留下可溯源的位置。
    if block_type in _VISUAL_BLOCK_TYPES and not content:
        image_source = block.get("image_source")
        if isinstance(image_source, str) and image_source.strip():
            parts.append(f"[{block_type.capitalize()} asset: {image_source}]")
    if captions:
        parts.append("Caption: " + " ".join(captions))
    if footnotes:
        parts.append("Footnote: " + " ".join(footnotes))
    return "\n".join(part for part in parts if part).strip()


def _plain_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [
            _plain_content(item.get("content"))
            for item in value
            if isinstance(item, Mapping)
        ]
        return " ".join(part for part in parts if part)
    return ""


def _annotation_text(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text
        for item in value
        if isinstance(item, Mapping)
        if (text := _plain_content(item.get("content")))
    ]


def _asset_from_block(
    block: Mapping[str, Any], page_idx: int, block_index: int | Any
) -> dict[str, Any] | None:
    path = block.get("image_source")
    if not isinstance(path, str) or not path.strip():
        return None
    return {
        "kind": block.get("type", "image"),
        "relative_path": path,
        "caption": " ".join(_annotation_text(block.get("captions"))),
        "page_idx": page_idx,
        "block_index": block_index,
        "bbox": block.get("bbox"),
    }


def _make_chunk_document(
    *,
    paper_id: str,
    parse_version: str,
    paper_title: str,
    chunk_no: int,
    units: list[_ChunkUnit],
) -> dict[str, Any]:
    content_parts: list[str] = []
    llm_content_parts: list[str] = []
    source_blocks: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    cursor = 0
    llm_cursor = 0
    for unit in units:
        if content_parts:
            cursor += 2  # ``\n\n`` 分隔符也计入 content 的字符位置。
            llm_cursor += 2
        start = cursor
        content_parts.append(unit.content)
        cursor += len(unit.content)
        marker = _block_marker(unit.source_block)
        llm_part = f"{marker}\n{unit.content}"
        llm_start = llm_cursor + len(marker) + 1
        llm_content_parts.append(llm_part)
        llm_cursor += len(llm_part)
        source_blocks.append(
            {
                **unit.source_block,
                "content_start": start,
                "content_end": cursor,
                "llm_content_start": llm_start,
                "llm_content_end": llm_cursor,
            }
        )
        if unit.asset is not None:
            assets.append(unit.asset)

    block_types = {unit.source_block["type"] for unit in units}
    content_type = "mixed" if len(block_types) > 1 else next(iter(block_types))
    pages = [int(unit.source_block["page_idx"]) for unit in units]
    section_path = units[-1].section_path
    return {
        "paper_id": paper_id,
        "parse_version": parse_version,
        "chunk_no": chunk_no,
        "paper_title": paper_title,
        "section_path": " > ".join(section_path),
        "content_type": content_type,
        "content": "\n\n".join(content_parts),
        "llm_content": "\n\n".join(llm_content_parts),
        "page_start": min(pages),
        "page_end": max(pages),
        "source_blocks": source_blocks,
        "assets": assets,
        "indexed_at": datetime.now(UTC).isoformat(),
    }


def _block_marker(source_block: Mapping[str, Any]) -> str:
    """生成给 LLM 的稳定来源标签，使用 MinerU 原始 page/block 索引。"""

    return f"[block: p{source_block['page_idx']}-b{source_block['block_index']}]"


def _parse_version(middle_json: Mapping[str, Any]) -> str:
    version = _mineru_version(middle_json) or "unknown"
    return f"mineru-{version}:chunker-{CHUNKER_VERSION}"


def _document_id(*, paper_id: str, parse_version: str, chunk_no: int) -> str:
    """构造 ES 文档的全局唯一 _id；不冗余保存为业务字段。"""

    return f"{paper_id}:{parse_version}:chunk:{chunk_no:04d}"


def _mineru_version(middle_json: Mapping[str, Any]) -> str | None:
    metadata = middle_json.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    producer = metadata.get("producer")
    if not isinstance(producer, Mapping):
        return None
    version = producer.get("version")
    return version if isinstance(version, str) else None


async def _bulk_index(
    *,
    client: Any,
    index_name: str,
    documents: Iterable[dict[str, Any]],
    refresh: bool,
) -> None:
    operations: list[dict[str, Any]] = []
    for document in documents:
        operations.append(
            {
                "index": {
                    "_index": index_name,
                    "_id": _document_id(
                        paper_id=document["paper_id"],
                        parse_version=document["parse_version"],
                        chunk_no=document["chunk_no"],
                    ),
                }
            }
        )
        operations.append(document)
    if not operations:
        return

    response = _response_body(
        await client.bulk(
            operations=operations, refresh="wait_for" if refresh else False
        )
    )
    if not response.get("errors"):
        return
    failures = [
        item
        for item in response.get("items", [])
        if isinstance(item, Mapping)
        if isinstance(item.get("index"), Mapping)
        if int(item["index"].get("status", 500)) >= 300
    ]
    raise ElasticsearchIndexError(f"Elasticsearch bulk index failed: {failures!r}")


def _response_body(response: Any) -> Mapping[str, Any]:
    """兼容 elasticsearch-py 的 ObjectApiResponse 与测试中的普通 dict。"""

    body = getattr(response, "body", response)
    if not isinstance(body, Mapping):
        raise ElasticsearchIndexError(f"Unexpected Elasticsearch response: {body!r}")
    return body
