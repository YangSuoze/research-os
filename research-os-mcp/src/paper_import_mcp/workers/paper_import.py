"""单进程论文导入 worker：三个阶段按 1 / N / 1 流水线执行。"""

from __future__ import annotations

import asyncio
import fcntl
import logging

from paper_import_mcp.config import settings
from paper_import_mcp.db.paper_imports import PaperImportRepository
from paper_import_mcp.services.paper_import import PaperImportService

logger = logging.getLogger(__name__)
_STAGES = ("mineru", "index_extract", "neo4j")


class PaperImportWorker:
    def __init__(
        self,
        repository: PaperImportRepository,
        service: PaperImportService,
        *,
        extract_concurrency: int = settings.paper_extract_concurrency,
        poll_interval: float = 1.0,
    ) -> None:
        if extract_concurrency < 1:
            raise ValueError("extract_concurrency must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.repository = repository
        self.service = service
        self.poll_interval = poll_interval
        self.limits = {"mineru": 1, "index_extract": extract_concurrency, "neo4j": 1}
        self.active = {stage: 0 for stage in _STAGES}
        self.tasks: set[asyncio.Task[None]] = set()

    async def _dispatch(self) -> int:
        scheduled = 0
        for stage in _STAGES:
            while self.active[stage] < self.limits[stage]:
                job = await asyncio.to_thread(self.repository.claim_next, stage)
                if job is None:
                    break
                self.active[stage] += 1
                scheduled += 1
                task = asyncio.create_task(self._run_stage(stage, job))
                self.tasks.add(task)
                task.add_done_callback(self._task_done)
        return scheduled

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                logger.exception("论文 worker 未能更新任务状态")

    async def _run_stage(self, stage: str, job: dict) -> None:
        batch_id, paper_id = job["batch_id"], job["paper_id"]
        try:
            parse_version = None
            if stage == "mineru":
                await self.service.run_mineru(paper_id)
            elif stage == "index_extract":
                parse_version = await self.service.run_index_extract(
                    paper_id, job["task_root_id"]
                )
            else:
                await self.service.run_neo4j(
                    paper_id, job["parse_version"], job["task_root_id"]
                )
            await asyncio.to_thread(
                self.repository.finish_stage, batch_id, paper_id, stage, parse_version
            )
        except Exception as exc:
            logger.exception("论文导入失败：paper_id=%s stage=%s", paper_id, stage)
            await asyncio.to_thread(
                self.repository.fail_stage, batch_id, paper_id, stage, str(exc)
            )
        finally:
            self.active[stage] -= 1

    async def run_forever(self) -> None:
        recovered = await asyncio.to_thread(self.repository.recover_running)
        if recovered:
            logger.info("重新排队 %d 个中断的论文任务", recovered)
        try:
            while True:
                await self._dispatch()
                if self.tasks:
                    await asyncio.wait(
                        self.tasks,
                        timeout=self.poll_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await asyncio.sleep(self.poll_interval)
        finally:
            # 停止接新任务后等在途阶段完成，再释放独占锁。
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run_exclusive(self) -> None:
        """API 和命令行共用同一把锁；已有 worker 退出后自动接管。"""

        with self.repository.db_path.with_suffix(".worker.lock").open("a+b") as lock:
            waiting = False
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        logger.info("已有论文导入 worker，等待其退出后接管")
                        waiting = True
                    await asyncio.sleep(self.poll_interval)
            logger.info("论文导入 worker 已启动")
            try:
                await self.run_forever()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def run_until_idle(self) -> None:
        """运行当前队列直到没有活跃任务；用于测试或一次性处理。"""

        await asyncio.to_thread(self.repository.recover_running)
        while True:
            scheduled = await self._dispatch()
            if not self.tasks:
                if not scheduled:
                    return
                continue
            await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)


async def _main() -> None:
    logging.basicConfig(level=settings.log_level)
    repository = PaperImportRepository()
    await PaperImportWorker(repository, PaperImportService()).run_exclusive()
