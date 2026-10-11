"""全局 jobs.sqlite：批次状态与单 worker 的持久化任务队列。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from paper_import_mcp import paths
from paper_import_mcp.models.paper_import import BatchOut

_NEXT_STAGE = {"mineru": "index_extract", "index_extract": "neo4j"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    task_root_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_jobs (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    paper_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('mineru', 'index_extract', 'neo4j')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    parse_version TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, paper_id)
);
CREATE INDEX IF NOT EXISTS paper_jobs_queue ON paper_jobs(status, stage, updated_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaperImportRepository:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or paths.paper_import_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_batch(
        self, batch_id: str, task_root_id: str, papers: Sequence[tuple[str, str]]
    ) -> None:
        if not papers or not task_root_id.strip():
            raise ValueError("A batch needs papers and a task_root_id")
        task_root_id = task_root_id.strip()
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for paper_id, _ in papers:
                active = connection.execute(
                    "SELECT 1 FROM paper_jobs WHERE paper_id = ? "
                    "AND status IN ('queued', 'running') LIMIT 1",
                    (paper_id,),
                ).fetchone()
                if active:
                    raise FileExistsError(f"Paper {paper_id} is already being imported")
            connection.execute(
                "INSERT INTO batches VALUES (?, ?, ?)",
                (batch_id, task_root_id, timestamp),
            )
            connection.executemany(
                "INSERT INTO paper_jobs "
                "(batch_id, paper_id, original_filename, stage, status, updated_at) "
                "VALUES (?, ?, ?, 'mineru', 'queued', ?)",
                [
                    (batch_id, paper_id, filename, timestamp)
                    for paper_id, filename in papers
                ],
            )

    def get_batch(self, batch_id: str) -> BatchOut | None:
        with self._connect() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                return None
            papers = [
                dict(row)
                for row in connection.execute(
                    "SELECT paper_id, original_filename, stage, status, parse_version, "
                    "retry_count, last_error, updated_at FROM paper_jobs "
                    "WHERE batch_id = ? ORDER BY rowid",
                    (batch_id,),
                )
            ]
        statuses = {paper["status"] for paper in papers}
        status = (
            "succeeded"
            if statuses == {"succeeded"}
            else "queued"
            if statuses == {"queued"}
            else "running"
            if statuses & {"queued", "running"}
            else "failed"
        )
        return BatchOut(
            batch_id=batch["batch_id"],
            task_root_id=batch["task_root_id"],
            created_at=batch["created_at"],
            status=status,
            papers=papers,
        )

    def retry_failed(self, batch_id: str) -> int | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                is None
            ):
                return None
            failed = connection.execute(
                "SELECT paper_id FROM paper_jobs WHERE batch_id = ? AND status = 'failed'",
                (batch_id,),
            ).fetchall()
            for row in failed:
                active = connection.execute(
                    "SELECT 1 FROM paper_jobs WHERE paper_id = ? AND batch_id != ? "
                    "AND status IN ('queued', 'running') LIMIT 1",
                    (row["paper_id"], batch_id),
                ).fetchone()
                if active:
                    raise FileExistsError(
                        f"Paper {row['paper_id']} is already being imported"
                    )
            connection.execute(
                "UPDATE paper_jobs SET status = 'queued', last_error = NULL, "
                "retry_count = retry_count + 1, updated_at = ? "
                "WHERE batch_id = ? AND status = 'failed'",
                (_now(), batch_id),
            )
            return len(failed)

    def recover_running(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_jobs SET status = 'queued', updated_at = ? "
                "WHERE status = 'running'",
                (_now(),),
            )
            return cursor.rowcount

    def claim_next(self, stage: str) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT j.batch_id, j.paper_id, j.parse_version, b.task_root_id "
                "FROM paper_jobs AS j JOIN batches AS b USING (batch_id) "
                "WHERE j.stage = ? AND j.status = 'queued' "
                "AND NOT EXISTS (SELECT 1 FROM paper_jobs AS other "
                "WHERE other.paper_id = j.paper_id AND other.status = 'running') "
                "ORDER BY j.updated_at, j.rowid LIMIT 1",
                (stage,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE paper_jobs SET status = 'running', updated_at = ? "
                "WHERE batch_id = ? AND paper_id = ?",
                (_now(), row["batch_id"], row["paper_id"]),
            )
            return dict(row)

    def finish_stage(
        self, batch_id: str, paper_id: str, stage: str, parse_version: str | None = None
    ) -> None:
        next_stage = _NEXT_STAGE.get(stage)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_jobs SET stage = ?, status = ?, "
                "parse_version = COALESCE(?, parse_version), last_error = NULL, "
                "updated_at = ? WHERE batch_id = ? AND paper_id = ? "
                "AND stage = ? AND status = 'running'",
                (
                    next_stage or stage,
                    "queued" if next_stage else "succeeded",
                    parse_version,
                    _now(),
                    batch_id,
                    paper_id,
                    stage,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Job is no longer running in the expected stage")

    def fail_stage(self, batch_id: str, paper_id: str, stage: str, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_jobs SET status = 'failed', last_error = ?, updated_at = ? "
                "WHERE batch_id = ? AND paper_id = ? AND stage = ? AND status = 'running'",
                (error[:1000], _now(), batch_id, paper_id, stage),
            )
            if cursor.rowcount != 1:
                raise ValueError("Job is no longer running in the expected stage")
