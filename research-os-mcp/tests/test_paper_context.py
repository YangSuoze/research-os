"""Exact evidence recovery, PDF HTTP routes and MCP protocol tests; no LLM."""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import Client
from paper_import_mcp import paths
from paper_import_mcp.infra.elasticsearch import (
    Bm25Hit,
    ElasticsearchIndexError,
    ElasticsearchPaperStore,
)
from paper_import_mcp.server import get_paper_context, mcp
from paper_import_mcp.services.paper_context import PaperContextService
from starlette.requests import Request

_VERSION = "mineru-4.0.5:chunker-v1"
_TEXT = "Channel evidence: 中文🙂 remains unchanged."


def _document():
    prefix = "## Channel attention\n\n"
    return {
        "paper_id": "paper_test",
        "parse_version": _VERSION,
        "paper_title": "A test paper",
        "chunk_no": 14,
        "section_path": "A test paper > Method",
        "content_type": "mixed",
        "content": prefix + _TEXT,
        "llm_content": "[block: p2-b1]\n## Channel attention\n\n[block: p3-b4]\n"
        + _TEXT,
        "page_start": 2,
        "page_end": 3,
        "source_blocks": [
            {
                "page_idx": 2,
                "block_index": 1,
                "type": "paragraph_title",
                "bbox": None,
                "content_start": 0,
                "content_end": len(prefix) - 2,
            },
            {
                "page_idx": 3,
                "block_index": 4,
                "type": "text",
                "bbox": [0.057, 0.174, 0.488, 0.227],
                "content_start": len(prefix),
                "content_end": len(prefix) + len(_TEXT),
            },
        ],
        "assets": [{"kind": "image", "relative_path": "images/figure.jpg"}],
    }


class _PaperStore:
    def __init__(self, documents=None):
        self.documents = documents if documents is not None else [_document()]
        self.calls = []
        self.close = AsyncMock()
        self.iterator_closed = False

    async def iter_paper_chunks(self, **options):
        self.calls.append(options)
        try:
            for doc in sorted(self.documents, key=lambda value: value["chunk_no"]):
                if (
                    doc["paper_id"] != options["paper_id"]
                    or doc["parse_version"] != options["parse_version"]
                ):
                    continue
                if (
                    options["chunk_nos"] is not None
                    and doc["chunk_no"] not in options["chunk_nos"]
                ):
                    continue
                if options["page_indices"] is not None and not any(
                    doc["page_start"] <= page <= doc["page_end"]
                    for page in options["page_indices"]
                ):
                    continue
                yield copy.deepcopy(doc)
        finally:
            self.iterator_closed = True


class PaperContextTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(paths, "PAPERS_DIR", self.root / "papers")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.pdf = paths.paper_source_path("paper_test")
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"%PDF-1.4\nexample")
        self.store = _PaperStore()
        self.service = PaperContextService(self.store)

    async def test_block_lookup_preserves_context_unicode_offsets_and_source(self):
        result = await self.service.get(
            "paper_test",
            _VERSION,
            block_ids=["p3-b4"],
            viewer_base_url="http://127.0.0.1:8002/",
        )
        chunk = result["chunks"][0]
        self.assertEqual(chunk["content"], _document()["content"])
        self.assertEqual(chunk["llm_content"], _document()["llm_content"])
        self.assertEqual(chunk["assets"], _document()["assets"])
        self.assertEqual(len(chunk["blocks"]), 1)
        block = chunk["blocks"][0]
        self.assertEqual(block["text"], _TEXT)
        self.assertEqual(block["page_number"], 4)
        self.assertEqual(result["pdf_path"], str(self.pdf.resolve()))
        self.assertEqual(
            result["pdf_url"], "http://127.0.0.1:8002/papers/paper_test/source.pdf"
        )
        url = urlsplit(block["viewer_url"])
        self.assertEqual(url.netloc, "127.0.0.1:8002")
        self.assertEqual(url.path, "/papers/paper_test/viewer")
        self.assertEqual(
            parse_qs(url.query),
            {
                "page_number": ["4"],
                "block_id": ["p3-b4"],
                "parse_version": [_VERSION],
                "bbox": ["0.057,0.174,0.488,0.227"],
            },
        )
        self.assertEqual(self.store.calls[0]["page_indices"], [3])
        self.assertTrue(self.store.iterator_closed)

    async def test_chunk_lookup_returns_all_blocks_in_order_without_http_urls(self):
        result = await self.service.get("paper_test", _VERSION, chunk_nos=[14, 14])
        self.assertEqual(
            [block["block_id"] for block in result["chunks"][0]["blocks"]],
            ["p2-b1", "p3-b4"],
        )
        self.assertIsNone(result["pdf_url"])
        self.assertIsNone(result["chunks"][0]["blocks"][0]["viewer_url"])
        self.assertEqual(self.store.calls[0]["chunk_nos"], [14])
        self.assertIsNone(self.store.calls[0]["page_indices"])

    async def test_both_references_must_belong_to_requested_chunks(self):
        result = await self.service.get(
            "paper_test", _VERSION, chunk_nos=[14], block_ids=["p3-b4"]
        )
        self.assertEqual(len(result["chunks"][0]["blocks"]), 1)
        with self.assertRaisesRegex(ValueError, "p3-b8"):
            await self.service.get(
                "paper_test", _VERSION, chunk_nos=[14], block_ids=["p3-b8"]
            )

    async def test_missing_version_chunk_or_block_is_not_silently_ignored(self):
        for version, options in [
            ("other-version", {"chunk_nos": [14]}),
            (_VERSION, {"chunk_nos": [14, 99]}),
            (_VERSION, {"block_ids": ["p3-b4", "p3-b99"]}),
        ]:
            with (
                self.subTest(version=version, options=options),
                self.assertRaisesRegex(ValueError, "References not found"),
            ):
                await self.service.get("paper_test", version, **options)

    async def test_missing_pdf_keeps_es_context_but_no_broken_links(self):
        self.pdf.unlink()
        result = await self.service.get(
            "paper_test",
            _VERSION,
            block_ids=["p3-b4"],
            viewer_base_url="http://localhost:8002",
        )
        self.assertIsNone(result["pdf_path"])
        self.assertIsNone(result["pdf_url"])
        self.assertIsNone(result["chunks"][0]["blocks"][0]["viewer_url"])
        self.assertEqual(result["chunks"][0]["blocks"][0]["text"], _TEXT)

    async def test_block_without_bbox_returns_a_page_only_link(self):
        result = await self.service.get(
            "paper_test",
            _VERSION,
            block_ids=["p2-b1"],
            viewer_base_url="http://localhost:8002",
        )
        block = result["chunks"][0]["blocks"][0]
        self.assertIsNone(block["bbox"])
        self.assertNotIn("bbox", parse_qs(urlsplit(block["viewer_url"]).query))
        self.assertEqual(block["page_number"], 3)

    async def test_invalid_inputs_fail_before_es_requests(self):
        for options in [
            {},
            {"chunk_nos": [0]},
            {"chunk_nos": [True]},
            {"chunk_nos": [-1]},
            {"chunk_nos": list(range(1, 52))},
            {"block_ids": ["p-1-b4"]},
            {"block_ids": ["unknown"]},
        ]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                await self.service.get("paper_test", _VERSION, **options)
        self.assertEqual(self.store.calls, [])

    async def test_invalid_offsets_close_the_iterator(self):
        self.store.documents[0]["source_blocks"][1]["content_end"] = 9999
        with self.assertRaisesRegex(ValueError, "offsets"):
            await self.service.get("paper_test", _VERSION, block_ids=["p3-b4"])
        self.assertTrue(self.store.iterator_closed)

    async def test_service_closes_the_es_client(self):
        await self.service.close()
        self.store.close.assert_awaited_once()

    async def test_http_tool_uses_actual_request_port(self):
        request = Request(
            {
                "type": "http",
                "scheme": "http",
                "server": ("127.0.0.1", 8002),
                "path": "/mcp",
                "root_path": "",
                "headers": [],
            }
        )
        context = SimpleNamespace(
            request_context=SimpleNamespace(
                request=request, lifespan_context=SimpleNamespace(papers=self.service)
            )
        )
        result = await get_paper_context(
            "paper_test", _VERSION, context, block_ids=["p3-b4"]
        )
        self.assertIn("127.0.0.1:8002", result["chunks"][0]["blocks"][0]["viewer_url"])

    async def test_pdf_http_routes_and_range_requests(self):
        app = mcp.streamable_http_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            viewer = await client.get("/papers/paper_test/viewer?page_number=4")
            self.assertEqual(viewer.status_code, 200)
            self.assertIn("pdfjs-dist@4.10.38", viewer.text)
            pdf = await client.get("/papers/paper_test/source.pdf")
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf.headers["content-type"], "application/pdf")
            self.assertEqual(pdf.content, self.pdf.read_bytes())
            ranged = await client.get(
                "/papers/paper_test/source.pdf", headers={"Range": "bytes=0-4"}
            )
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, b"%PDF-")
            self.assertEqual(
                (await client.get("/papers/missing/viewer")).status_code, 404
            )
            self.assertEqual(
                (await client.get("/papers/%2E%2E/source.pdf")).status_code, 400
            )
            self.assertEqual(
                (
                    await client.get(
                        "/papers/paper_test/source.pdf",
                        headers={"Host": "untrusted.example"},
                    )
                ).status_code,
                400,
            )

    async def test_pdf_routes_reject_symlinks_outside_storage(self):
        external = self.root / "secret.pdf"
        external.write_bytes(b"private")
        source = paths.paper_source_path("paper_external")
        source.parent.mkdir(parents=True)
        source.symlink_to(external)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp.streamable_http_app()),
            base_url="http://localhost",
        ) as client:
            response = await client.get("/papers/paper_external/source.pdf")
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("private", response.text)


class ContextESFilterTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_range_filter_preserves_pagination_and_pit_cleanup(self):
        document = _document()
        client = SimpleNamespace(
            open_point_in_time=AsyncMock(return_value={"id": "pit-1"}),
            search=AsyncMock(
                side_effect=[
                    {
                        "pit_id": "pit-2",
                        "hits": {"hits": [{"_source": document, "sort": [14, 1]}]},
                    },
                    {"hits": {"hits": []}},
                ]
            ),
            close_point_in_time=AsyncMock(),
        )
        store = ElasticsearchPaperStore(client=client)
        result = [
            chunk
            async for chunk in store.iter_paper_chunks(
                paper_id="paper_test",
                parse_version=_VERSION,
                page_indices=[3],
                chunk_nos=[14],
                page_size=1,
            )
        ]
        self.assertEqual(result, [document])
        first, second = client.search.await_args_list
        filters = first.kwargs["query"]["bool"]["filter"]
        self.assertIn({"term": {"paper_id": "paper_test"}}, filters)
        self.assertIn({"term": {"parse_version": _VERSION}}, filters)
        self.assertIn({"terms": {"chunk_no": [14]}}, filters)
        ranges = filters[-1]["bool"]["should"][0]["bool"]["filter"]
        self.assertEqual(
            ranges,
            [
                {"range": {"page_start": {"lte": 3}}},
                {"range": {"page_end": {"gte": 3}}},
            ],
        )
        self.assertEqual(second.kwargs["search_after"], [14, 1])
        self.assertEqual(second.kwargs["pit"]["id"], "pit-2")
        client.close_point_in_time.assert_awaited_once_with(id="pit-2")


class PaperChunkSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_bm25_filters_paper_ids_without_a_parse_version_filter(self):
        client = SimpleNamespace(
            search=AsyncMock(
                return_value={
                    "hits": {
                        "hits": [
                            {
                                "_id": "chunk-14",
                                "_score": 4.2,
                                "_source": _document(),
                                "highlight": {"content": [_TEXT]},
                            }
                        ]
                    }
                }
            )
        )
        store = ElasticsearchPaperStore(client=client)
        hits = await store.search_bm25(
            "attention", paper_ids=[" paper_test ", "paper_other", "paper_test"], size=3
        )
        self.assertEqual(hits[0].score, 4.2)
        self.assertEqual(hits[0].source["parse_version"], _VERSION)
        request = client.search.await_args.kwargs
        self.assertEqual(
            request["query"]["bool"]["filter"],
            [{"terms": {"paper_id": ["paper_test", "paper_other"]}}],
        )
        self.assertEqual(request["size"], 3)
        self.assertEqual(
            request["query"]["bool"]["must"][0]["multi_match"]["fields"],
            ["content^4", "paper_title^3", "section_path^2"],
        )
        self.assertEqual(request["highlight"]["pre_tags"], [""])
        self.assertFalse(request["allow_partial_search_results"])

    async def test_unfiltered_search_is_global_and_no_match_is_empty(self):
        client = SimpleNamespace(search=AsyncMock(return_value={"hits": {"hits": []}}))
        result = await PaperContextService(
            ElasticsearchPaperStore(client=client)
        ).search("unknown")
        self.assertEqual(result, {"hits": []})
        self.assertEqual(client.search.await_args.kwargs["query"]["bool"]["filter"], [])

    async def test_empty_paper_filter_matches_none_instead_of_all_papers(self):
        client = SimpleNamespace(search=AsyncMock())
        result = await PaperContextService(
            ElasticsearchPaperStore(client=client)
        ).search("attention", paper_ids=[])
        self.assertEqual(result, {"hits": []})
        client.search.assert_not_awaited()

    async def test_invalid_query_limit_and_blank_ids_fail_before_es_requests(self):
        client = SimpleNamespace(search=AsyncMock())
        service = PaperContextService(ElasticsearchPaperStore(client=client))
        for query, options in [
            (" ", {}),
            ("attention", {"limit": 0}),
            ("attention", {"limit": 101}),
            ("attention", {"paper_ids": [" "]}),
            (" ", {"paper_ids": []}),
        ]:
            with (
                self.subTest(query=query, options=options),
                self.assertRaises(ValueError),
            ):
                await service.search(query, **options)
        client.search.assert_not_awaited()

    async def test_incomplete_es_search_is_an_error_not_a_false_empty_result(self):
        for payload in [{"timed_out": True}, {"_shards": {"failed": 1}}]:
            with self.subTest(payload=payload):
                client = SimpleNamespace(search=AsyncMock(return_value=payload))
                with self.assertRaisesRegex(
                    ElasticsearchIndexError, "did not complete"
                ):
                    await ElasticsearchPaperStore(client=client).search_bm25(
                        "attention"
                    )

    async def test_previews_keep_versions_and_use_matching_fragments_or_bounded_fallback(
        self,
    ):
        first, second = _document(), _document()
        second["parse_version"] = "mineru-next:chunker-v2"
        second["content"] = "long text " * 100
        store = SimpleNamespace(
            search_bm25=AsyncMock(
                return_value=[
                    Bm25Hit(
                        "first",
                        4.2,
                        first,
                        {"content": [_TEXT, "Another matching fragment"]},
                    ),
                    Bm25Hit("second", 2.1, second, {}),
                ]
            )
        )
        result = await PaperContextService(store).search(
            " attention ", paper_ids=["paper_test"], limit=2
        )
        hits = result["hits"]
        self.assertEqual(
            [hit["parse_version"] for hit in hits], [_VERSION, "mineru-next:chunker-v2"]
        )
        self.assertEqual(
            hits[0]["content_preview"], _TEXT + "\n...\nAnother matching fragment"
        )
        self.assertEqual(len(hits[1]["content_preview"]), 500)
        self.assertNotIn("content", hits[0])
        self.assertNotIn("viewer_url", hits[0])
        store.search_bm25.assert_awaited_once_with(
            "attention", paper_ids=["paper_test"], size=2
        )


class PaperContextMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_returns_structured_context_and_reports_missing_references(self):
        async def idle():
            await asyncio.Future()

        store = _PaperStore()
        store.search_bm25 = AsyncMock(
            return_value=[Bm25Hit("chunk-14", 4.2, _document(), {"content": [_TEXT]})]
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(paths, "PAPERS_DIR", Path(directory) / "papers"),
            patch.object(paths, "PAPER_IMPORTS_DIR", Path(directory) / "imports"),
            patch(
                "paper_import_mcp.server.PaperImportWorker",
                return_value=SimpleNamespace(run_exclusive=idle),
            ),
            patch(
                "paper_import_mcp.server.ElasticsearchPaperStore", return_value=store
            ),
            patch(
                "paper_import_mcp.server.GraphStore",
                return_value=SimpleNamespace(close=AsyncMock()),
            ),
        ):
            async with Client(mcp) as client:
                tools = (await client.list_tools()).tools
                tool = next(tool for tool in tools if tool.name == "get_paper_context")
                self.assertEqual(
                    set(tool.input_schema["required"]), {"paper_id", "parse_version"}
                )
                search_tool = next(
                    tool for tool in tools if tool.name == "search_paper_chunks"
                )
                self.assertNotIn(
                    "parse_version", search_tool.input_schema["properties"]
                )
                searched = await client.call_tool(
                    "search_paper_chunks",
                    {"query": "attention", "paper_ids": ["paper_test"], "limit": 1},
                )
                self.assertFalse(searched.is_error, searched.content)
                hit = searched.structured_content["hits"][0]
                self.assertEqual(hit["parse_version"], _VERSION)
                store.search_bm25.assert_awaited_once_with(
                    "attention", paper_ids=["paper_test"], size=1
                )
                fetched = await client.call_tool(
                    "get_paper_context",
                    {
                        "paper_id": hit["paper_id"],
                        "parse_version": hit["parse_version"],
                        "chunk_nos": [hit["chunk_no"]],
                    },
                )
                self.assertFalse(fetched.is_error, fetched.content)
                self.assertEqual(
                    fetched.structured_content["chunks"][0]["content"],
                    _document()["content"],
                )
                store.search_bm25.side_effect = RuntimeError("ES unavailable")
                failed = await client.call_tool(
                    "search_paper_chunks", {"query": "attention"}
                )
                self.assertTrue(failed.is_error)
                result = await client.call_tool(
                    "get_paper_context",
                    {
                        "paper_id": "paper_test",
                        "parse_version": _VERSION,
                        "block_ids": ["p3-b4"],
                    },
                )
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(
                    result.structured_content["chunks"][0]["blocks"][0]["text"], _TEXT
                )
                result = await client.call_tool(
                    "get_paper_context",
                    {
                        "paper_id": "paper_test",
                        "parse_version": _VERSION,
                        "chunk_nos": [99],
                    },
                )
                self.assertTrue(result.is_error)
        store.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
