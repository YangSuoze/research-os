"""把论文抽取草稿对齐为共享实体，并写入论文专属的声明与证据。"""

from __future__ import annotations

import hashlib
import json

from paper_import_mcp.infra.neo4j import GraphStore
from paper_import_mcp.models.paper_graph import EvidenceReference, PaperGraphDraft
from paper_import_mcp.services.paper_graph.aligner import AlignmentDecision, GraphAligner


def _stable_id(prefix: str, *parts: object) -> str:
    key = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return f"{prefix}_{hashlib.sha256(key.encode()).hexdigest()[:24]}"


class PaperGraphIngester:
    def __init__(self, store: GraphStore, aligner: GraphAligner) -> None:
        self.store = store
        self.aligner = aligner

    async def ingest(
        self,
        draft: PaperGraphDraft,
        task_root_id: str,
        *,
        code_link: str | None = None,
    ) -> dict[str, int]:
        task_root_id = task_root_id.strip()
        if not task_root_id:
            raise ValueError("task_root_id must not be blank")
        await self.store.initialize_schema()
        await self.store.ensure_task_root(task_root_id)
        await self.store.clear_paper(draft.paper_id)
        nodes: list[dict] = [
            {
                "label": "Paper",
                "id": draft.paper_id,
                "properties": {
                    "title": draft.paper_title,
                    **({"code_link": code_link} if code_link else {}),
                },
            }
        ]
        edges: set[tuple[str, str, str]] = set()
        edges.add((draft.paper_id, "FOR_TASK", task_root_id))
        methods: dict[str, str] = {}
        datasets: dict[str, str] = {}
        aligned_ids: set[str] = set()
        redirects: dict[str, str] = {}

        def resolved(entity_id: str) -> str:
            while entity_id in redirects:
                entity_id = redirects[entity_id]
            return entity_id

        async def save_decision(label: str, decision: AlignmentDecision) -> str:
            entity_id = await self.store.save_entity(
                label,
                decision.name,
                decision.description,
                decision.embedding,
                self.aligner.embedder.model_name,
                decision.related_ids,
            )
            removed = set(decision.related_ids) - {entity_id}
            if removed:
                redirects.update({old_id: entity_id for old_id in removed})
                for mapping in (methods, datasets):
                    mapping.update(
                        {key: resolved(value) for key, value in mapping.items()}
                    )
                edges_copy = set()
                for source, relation, target in edges:
                    source, target = resolved(source), resolved(target)
                    if source != target:
                        edges_copy.add((source, relation, target))
                edges.clear()
                edges.update(edges_copy)
                aligned_ids.difference_update(removed)
            aligned_ids.add(entity_id)
            return entity_id

        async def entity(label: str, name: str, detail: str) -> str:
            return await save_decision(
                label, await self.aligner.align(label, name, detail)
            )

        def evidence_refs(references: list[EvidenceReference], target_id: str) -> None:
            for reference in references:
                evidence_id = _stable_id(
                    "evidence",
                    draft.paper_id,
                    draft.parse_version,
                    reference.chunk_no,
                    sorted(reference.block_ids),
                )
                nodes.append(
                    {
                        "label": "Evidence",
                        "id": evidence_id,
                        "properties": {
                            "paper_id": draft.paper_id,
                            "parse_version": draft.parse_version,
                            "chunk_no": reference.chunk_no,
                            "block_ids": reference.block_ids,
                        },
                    }
                )
                edges.add((draft.paper_id, "HAS_EVIDENCE", evidence_id))
                edges.add((evidence_id, "SUPPORTS", target_id))

        def claim(
            kind: str,
            text: str,
            method_ids: list[str],
            references: list[EvidenceReference],
            **properties: object,
        ) -> str:
            claim_id = _stable_id(
                "claim",
                draft.paper_id,
                draft.parse_version,
                kind,
                text,
                properties,
                [(ref.chunk_no, ref.block_ids) for ref in references],
            )
            nodes.append(
                {
                    "label": "Claim",
                    "id": claim_id,
                    "properties": {
                        "kind": kind,
                        "text": text,
                        "paper_id": draft.paper_id,
                        "parse_version": draft.parse_version,
                        **properties,
                    },
                }
            )
            edges.add((draft.paper_id, "HAS_CLAIM", claim_id))
            for method_id in method_ids:
                edges.add((claim_id, "ABOUT_METHOD", method_id))
            evidence_refs(references, claim_id)
            return claim_id

        for method in draft.mechanisms:
            family, leaf = await self.aligner.align_method(
                method.method_description, method.mechanism
            )
            family_id = await save_decision("MethodFamily", family)
            method_id = await save_decision("MethodLeaf", leaf)
            methods[method.id] = method_id
            edges.add((task_root_id, "HAS_FAMILY", family_id))
            edges.add((family_id, "HAS_METHOD", method_id))
            # 后续机制对齐也需要看到本篇刚生成的 Leaf 父类。
            await self.store.upsert_graph(
                [],
                [
                    {"source": task_root_id, "type": "HAS_FAMILY", "target": family_id},
                    {"source": family_id, "type": "HAS_METHOD", "target": method_id},
                ],
            )
            claim("mechanism", method.mechanism, [method_id], method.evidence)

        for dataset in draft.datasets:
            dataset_id = await entity(
                "Dataset", dataset.name, dataset.description or ""
            )
            datasets[dataset.id] = dataset_id
            edges.add((draft.paper_id, "USES_DATASET", dataset_id))
            edges.add((dataset_id, "FOR_TASK", task_root_id))
            evidence_refs(dataset.evidence, dataset_id)

        for item in draft.addresses:
            problem_id = await entity(
                "Problem", item.problem_name, item.problem_description
            )
            linked_methods = [methods[item_id] for item_id in item.method_ids]
            claim_id = claim(
                "addresses", item.explanation, linked_methods, item.evidence
            )
            edges.add((claim_id, "ABOUT_PROBLEM", problem_id))
            for method_id in linked_methods:
                edges.add((method_id, "ADDRESSES", problem_id))

        for item in draft.results:
            linked_methods = [methods[item_id] for item_id in item.method_ids]
            claim_id = claim("result", item.result, linked_methods, item.evidence)
            for dataset_id in item.dataset_ids:
                edges.add((claim_id, "ON_DATASET", datasets[dataset_id]))

        for item in draft.limitations:
            linked_methods = [methods[item_id] for item_id in item.method_ids]
            claim(
                "limitation",
                item.description,
                linked_methods,
                item.evidence,
                name=item.name,
            )

        unique_nodes = {node["id"]: node for node in nodes}
        relationships = [
            {"source": source, "type": relation_type, "target": target}
            for source, relation_type, target in sorted(edges)
        ]
        await self.store.upsert_graph(list(unique_nodes.values()), relationships)
        return {
            "aligned_entities": len(aligned_ids),
            "paper_nodes": len(unique_nodes),
            "relationships": len(relationships),
        }
