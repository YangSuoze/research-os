"""批量导入接口的轻量响应模型。"""

from typing import Literal

from pydantic import BaseModel

Stage = Literal["mineru", "index_extract", "neo4j"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
BatchStatus = Literal["queued", "running", "succeeded", "failed"]


class PaperJobOut(BaseModel):
    paper_id: str
    original_filename: str
    stage: Stage
    status: JobStatus
    parse_version: str | None
    retry_count: int
    last_error: str | None
    updated_at: str


class BatchAccepted(BaseModel):
    batch_id: str
    paper_ids: list[str]


class BatchOut(BaseModel):
    batch_id: str
    task_root_id: str
    created_at: str
    status: BatchStatus
    papers: list[PaperJobOut]
