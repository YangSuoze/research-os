"""Graph alignment and paper-specific method evidence without external services."""

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from paper_import_mcp.infra.neo4j import GraphStore
from paper_import_mcp.models.paper_graph import (
    EvidenceReference,
    MethodEntry,
    MechanismFinding,
    MechanismResponse,
    PaperGraphDraft,
)
from paper_import_mcp.services.paper_graph.aligner import (
    AlignmentDecision,
    GraphAligner,
)
from paper_import_mcp.services.paper_graph.ingester import PaperGraphIngester
from paper_import_mcp.services.paper_graph.extractor import PaperGraphExtractor
from paper_import_mcp.services.paper_graph.method_guidelines import GLOBAL_METHOD_RULES


class GraphAlignmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_mechanism_draft_has_no_premature_method_names(self) -> None:
        self.assertEqual(
            set(MechanismFinding.model_fields),
            {"method_description", "mechanism", "evidence"},
        )
        catalog = PaperGraphExtractor.build_method_catalog(
            MechanismResponse(
                items=[
                    MechanismFinding(
                        method_description="Guide features with explicit constraints.",
                        mechanism="Adds a paper-specific constraint penalty.",
                        evidence=[EvidenceReference(chunk_no=1, block_ids=["p0-b1"])],
                    )
                ]
            )
        )
        self.assertEqual(catalog[0].id, "m001")

    async def test_vector_query_does_not_filter_by_score(self) -> None:
        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def run(self, query, **params):
                self.query = query
                self.params = params

                async def rows():
                    yield {
                        "id": "method_1",
                        "name": "Method",
                        "detail": "Reusable mechanism",
                        "score": 0.2,
                    }

                return rows()

        session = Session()
        store = GraphStore(driver=SimpleNamespace(session=lambda: session))
        candidates = await store.find_candidates("MethodLeaf", [0.1], 50, "bge-small")

        self.assertEqual(candidates[0]["score"], 0.2)
        self.assertNotIn("score >=", session.query)
        self.assertEqual(session.params["limit"], 50)
        self.assertIn("OPTIONAL MATCH (family:MethodFamily)", session.query)

    async def test_method_names_are_chosen_with_both_candidate_lists(self) -> None:
        family_candidate = {
            "id": "family_1",
            "name": "Contrastive learning",
            "detail": "Learn by contrasting positive and negative pairs.",
            "score": 0.3,
        }
        leaf_candidate = {
            "id": "method_1",
            "name": "Cross-modal contrastive alignment",
            "detail": "Align image and text representations with a contrastive objective.",
            "parents": [{"id": "family_1", "name": "Contrastive learning"}],
            "score": 0.2,
        }
        store = SimpleNamespace(
            find_candidates=AsyncMock(
                side_effect=lambda label, *_: (
                    [family_candidate] if label == "MethodFamily" else [leaf_candidate]
                )
            )
        )
        embedder = SimpleNamespace(
            model_name="bge-small",
            embed=AsyncMock(side_effect=[[0.1], [0.2], [0.3]]),
        )
        llm = SimpleNamespace(
            aquery=AsyncMock(
                return_value=json.dumps(
                    {
                        "family": {
                            "name": "Contrastive learning",
                            "related_ids": ["family_1"],
                            "description": "Learn representations by contrasting paired examples.",
                        },
                        "leaf": {
                            "name": "Image-text contrastive alignment",
                            "related_ids": ["method_1"],
                            "description": "Align image and text representations using contrastive learning.",
                        },
                    }
                )
            )
        )
        guide = "Use Contrastive Learning as a family; keep loss weights in Claim."
        family, leaf = await GraphAligner(
            store, embedder, llm, method_guidelines=guide
        ).align_method(
            "Align image and text representations using contrastive learning.",
            "Weight hard negatives by clinical relevance.",
        )

        store.find_candidates.assert_has_awaits(
            [
                call("MethodFamily", [0.1], 50, "bge-small"),
                call("MethodLeaf", [0.1], 50, "bge-small"),
            ],
            any_order=True,
        )
        self.assertEqual(family.related_ids, ("family_1",))
        self.assertEqual(leaf.related_ids, ("method_1",))
        self.assertEqual(leaf.name, "Image-text contrastive alignment")
        self.assertEqual(
            leaf.description,
            "Align image and text representations using contrastive learning.",
        )
        self.assertEqual(leaf.embedding, [0.3])
        self.assertEqual(
            set(llm.aquery.call_args.kwargs["json_mode"].model_fields),
            {"family", "leaf"},
        )
        payload = json.loads(llm.aquery.call_args.args[0][1]["content"])
        self.assertEqual(
            payload["core_mechanism"],
            "Align image and text representations using contrastive learning.",
        )
        self.assertEqual(
            payload["leaf_candidates"][0]["parents"][0]["id"], "family_1"
        )
        prompt = llm.aquery.call_args.args[0][0]["content"]
        self.assertIn(guide, prompt)
        self.assertIn(GLOBAL_METHOD_RULES, prompt)

    async def test_method_is_named_even_without_candidates(self) -> None:
        store = SimpleNamespace(find_candidates=AsyncMock(return_value=[]))
        embedder = SimpleNamespace(
            model_name="bge-small", embed=AsyncMock(return_value=[0.1])
        )
        llm = SimpleNamespace(
            aquery=AsyncMock(
                return_value=json.dumps(
                    {
                        "family": {
                            "name": "Constraint-based learning",
                            "related_ids": [],
                            "description": "Learn with explicit constraints on model behavior.",
                        },
                        "leaf": {
                            "name": "Constraint-guided feature learning",
                            "related_ids": [],
                            "description": "Guide learned features with explicit constraints.",
                        },
                    }
                )
            )
        )

        family, leaf = await GraphAligner(store, embedder, llm).align_method(
            "Guide learned features with explicit constraints.",
            "Adds a constraint penalty to the paper's training objective.",
        )
        self.assertEqual(family.related_ids, ())
        self.assertEqual(leaf.related_ids, ())
        self.assertEqual(family.name, "Constraint-based learning")
        llm.aquery.assert_awaited_once()

    async def test_rejects_non_candidate_ids(self) -> None:
        store = SimpleNamespace(
            find_candidates=AsyncMock(
                side_effect=lambda label, *_: (
                    [{"id": "family_1"}] if label == "MethodFamily" else []
                )
            )
        )
        embedder = SimpleNamespace(
            model_name="bge-small", embed=AsyncMock(return_value=[0.1])
        )
        llm = SimpleNamespace(
            aquery=AsyncMock(
                return_value=json.dumps(
                    {
                        "family": {
                            "name": "Constraint learning",
                            "related_ids": ["unknown"],
                            "description": "Use constraints during learning.",
                        },
                        "leaf": {
                            "name": "Constraint-guided feature learning",
                            "related_ids": [],
                            "description": "Guide learned features with explicit constraints.",
                        },
                    }
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "unknown candidate IDs"):
            await GraphAligner(store, embedder, llm).align_method(
                "Guide learned features with explicit constraints.",
                "Adds a constraint penalty to the training objective.",
            )

    async def test_method_core_is_shared_but_paper_detail_stays_in_claim(self) -> None:
        core = "Align image and text representations with a contrastive objective."
        paper_detail = "Weights hard negatives according to their clinical relevance."
        reference = EvidenceReference(chunk_no=1, block_ids=["p0-b1"])
        draft = PaperGraphDraft(
            paper_id="paper_1",
            parse_version="mineru:chunker",
            paper_title="Example paper",
            mechanisms=[
                MethodEntry(
                    id="m001",
                    method_description=core,
                    mechanism=paper_detail,
                    evidence=[reference],
                )
            ],
            addresses=[],
            results=[],
            limitations=[],
            datasets=[],
        )
        store = SimpleNamespace(
            initialize_schema=AsyncMock(),
            ensure_task_root=AsyncMock(),
            clear_paper=AsyncMock(),
            save_entity=AsyncMock(side_effect=["family_1", "method_1"]),
            upsert_graph=AsyncMock(),
        )
        aligner = SimpleNamespace(
            embedder=SimpleNamespace(model_name="bge-small"),
            align_method=AsyncMock(
                return_value=(
                    AlignmentDecision(
                        "Contrastive learning", (), "Learn by contrasting pairs.", [0.1]
                    ),
                    AlignmentDecision("Image-text contrastive alignment", (), core, [0.2]),
                )
            ),
        )

        await PaperGraphIngester(store, aligner).ingest(draft, "report_generation")

        aligner.align_method.assert_awaited_once_with(core, paper_detail)
        self.assertEqual(store.save_entity.await_args_list[1].args[2], core)
        nodes = store.upsert_graph.await_args.args[0]
        self.assertEqual(
            [node["properties"]["text"] for node in nodes if node["label"] == "Claim"],
            [paper_detail],
        )
        relationships = store.upsert_graph.await_args.args[1]
        self.assertIn(
            {"source": "family_1", "type": "HAS_METHOD", "target": "method_1"},
            relationships,
        )
        self.assertEqual(
            store.upsert_graph.await_args_list[0].args,
            (
                [],
                [
                    {"source": "report_generation", "type": "HAS_FAMILY", "target": "family_1"},
                    {"source": "family_1", "type": "HAS_METHOD", "target": "method_1"},
                ],
            ),
        )

    async def test_next_method_can_see_parents_created_in_the_same_paper(self) -> None:
        draft = PaperGraphDraft(
            paper_id="paper_1",
            parse_version="mineru:chunker",
            paper_title="Example paper",
            mechanisms=[
                MethodEntry(
                    id=f"m{number:03d}",
                    method_description="Guide features with explicit constraints.",
                    mechanism=f"Constraint implementation {number}.",
                    evidence=[EvidenceReference(chunk_no=1, block_ids=["p0-b1"])],
                )
                for number in (1, 2)
            ],
            addresses=[], results=[], limitations=[], datasets=[],
        )
        persisted_edges = []

        async def upsert(_nodes, edges):
            persisted_edges.extend(deepcopy(edges))

        async def align(_core, _detail):
            if aligner.align_method.await_count == 2:
                self.assertIn(
                    {"source": "family_1", "type": "HAS_METHOD", "target": "method_1"},
                    persisted_edges,
                )
            return (
                AlignmentDecision("Constraint Learning", (), "Learn with constraints.", [0.1]),
                AlignmentDecision("Constrained Features", (), "Constrain features.", [0.2]),
            )

        store = SimpleNamespace(
            initialize_schema=AsyncMock(), ensure_task_root=AsyncMock(),
            clear_paper=AsyncMock(),
            save_entity=AsyncMock(side_effect=["family_1", "method_1", "family_1", "method_2"]),
            upsert_graph=AsyncMock(side_effect=upsert),
        )
        aligner = SimpleNamespace(
            embedder=SimpleNamespace(model_name="bge-small"),
            align_method=AsyncMock(side_effect=align),
        )
        await PaperGraphIngester(store, aligner).ingest(draft, "report_generation")
        self.assertEqual(aligner.align_method.await_count, 2)

    async def test_extraction_receives_the_same_granularity_guide(self) -> None:
        async def chunks(**_):
            yield {
                "paper_id": "paper_1", "parse_version": "mineru:chunker",
                "paper_title": "Example paper", "chunk_no": 1, "section_path": "Methods",
                "llm_content": "[block: p0-b1]\nWe guide features with constraints.",
                "source_blocks": [{"page_idx": 0, "block_index": 1}],
            }

        llm = SimpleNamespace(aquery=AsyncMock(return_value=json.dumps({"items": [{
            "method_description": "Guide features with explicit constraints.",
            "mechanism": "Use a constraint penalty.",
            "evidence": [{"chunk_no": 1, "block_ids": ["p0-b1"]}],
        }]})))
        guide = "Keep penalty coefficients in Claim, not separate methods."
        extractor = PaperGraphExtractor(
            paper_store=SimpleNamespace(iter_paper_chunks=chunks), llm_client=llm,
            section_selector=SimpleNamespace(select=AsyncMock(return_value=[1])),
            model_name="test-model", method_guidelines=guide,
        )
        result = await extractor.extract_kind(
            paper_id="paper_1", parse_version="mineru:chunker", kind="mechanism"
        )
        self.assertEqual(len(result.items), 1)
        prompt = llm.aquery.call_args.args[0][0]["content"]
        self.assertIn(GLOBAL_METHOD_RULES, prompt)
        self.assertIn(guide, prompt)


if __name__ == "__main__":
    unittest.main()
