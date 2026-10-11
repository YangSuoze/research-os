"""论文图谱抽取草稿；不包含 Neo4j ID 或跨论文实体对齐结果。"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ExtractionKind: TypeAlias = Literal[
    "mechanism", "addresses", "result", "limitation", "Dataset"
]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
BlockId = Annotated[str, StringConstraints(pattern=r"^p\d+-b\d+$")]
ChunkNo = Annotated[int, Field(strict=True, gt=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceReference(StrictModel):
    """指向一篇论文的一个 chunk 内的具体 MinerU block。"""

    chunk_no: ChunkNo
    block_ids: list[BlockId] = Field(min_length=1)


class MechanismFinding(StrictModel):
    # 仅用于候选召回和后续论断关联；规范名称在 Neo4j 阶段参照候选生成。
    method_description: NonBlank
    mechanism: NonBlank
    evidence: list[EvidenceReference] = Field(min_length=1)


class MethodEntry(MechanismFinding):
    """程序为本篇论文分配的临时方法 ID，不由 LLM 生成。"""

    id: NonBlank


class AddressesFinding(StrictModel):
    # 空列表表示无法可靠定位到目录中的原子方法，不强行连边。
    method_ids: list[NonBlank]
    problem_name: NonBlank
    problem_description: NonBlank
    explanation: NonBlank
    evidence: list[EvidenceReference] = Field(min_length=1)


class ResultFinding(StrictModel):
    # 论文级结果或无法可靠定位时可为空；组合方法可引用多个 ID。
    method_ids: list[NonBlank]
    dataset_ids: list[NonBlank]
    result: NonBlank
    evidence: list[EvidenceReference] = Field(min_length=1)


class LimitationFinding(StrictModel):
    method_ids: list[NonBlank]
    name: NonBlank
    description: NonBlank
    evidence: list[EvidenceReference] = Field(min_length=1)


class DatasetFinding(StrictModel):
    name: NonBlank
    description: NonBlank | None
    evidence: list[EvidenceReference] = Field(min_length=1)


class DatasetEntry(DatasetFinding):
    """程序为本篇论文分配的临时数据集 ID，不由 LLM 生成。"""

    id: NonBlank


class MechanismResponse(StrictModel):
    items: list[MechanismFinding]


class AddressesResponse(StrictModel):
    items: list[AddressesFinding]


class ResultResponse(StrictModel):
    items: list[ResultFinding]


class LimitationResponse(StrictModel):
    items: list[LimitationFinding]


class DatasetResponse(StrictModel):
    items: list[DatasetFinding]


KindResponse: TypeAlias = (
    MechanismResponse
    | AddressesResponse
    | ResultResponse
    | LimitationResponse
    | DatasetResponse
)


class PaperGraphDraft(StrictModel):
    """五类抽取结果的合集；还不是可直接写入 Neo4j 的图。"""

    paper_id: NonBlank
    parse_version: NonBlank
    paper_title: NonBlank
    mechanisms: list[MethodEntry]
    addresses: list[AddressesFinding]
    results: list[ResultFinding]
    limitations: list[LimitationFinding]
    datasets: list[DatasetEntry]
