"""异步任务执行器：适用于会主动等待的任务，不运行阻塞函数。"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from src.executor import InvalidTransitionError, Job, JobExecutor, JobStatus, utc_now


class AsyncJobExecutor(JobExecutor):
    """使用等待队列和执行人数限制，同时处理多个任务。"""

    def __init__(self, max_concurrency: int = 3) -> None:
        super().__init__()
        if max_concurrency < 1:
            raise ValueError("同时执行的任务数必须大于零")
        self.max_concurrency = max_concurrency
        self._slots = asyncio.Semaphore(max_concurrency)
        self._active: dict[str, asyncio.Task] = {}
        self._queued: set[str] = set()

    def submit(self, job_id: str, task: Callable[[], Awaitable[Any]]) -> Job:
        if not inspect.iscoroutinefunction(task):
            raise TypeError("请提交 async def 定义的函数；阻塞命令应放进容器")
        return super().submit(job_id, task)

    @staticmethod
    def _finish(job: Job, status: JobStatus, error: BaseException | None = None) -> None:
        if error is not None:
            job.error_type = type(error).__name__
            job.error_message = str(error)
        job.finished_at = utc_now()
        job.transition_to(status, occurred_at=job.finished_at)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job_id in self._active:
            return self._active[job_id].cancel()
        if job.status is JobStatus.PENDING:
            self._finish(job, JobStatus.CANCELLED)
            return True
        return False

    async def run(self, job_id: str, *, timeout: float | None = None) -> Job:
        return await self._run(job_id, timeout=timeout)

    async def _run(self, job_id: str, *, timeout: float | None, from_queue: bool = False) -> Job:
        if timeout is not None and timeout <= 0:
            raise ValueError("超时秒数必须大于零")
        job = self.get(job_id)
        if from_queue and job.status is JobStatus.CANCELLED:
            return job
        if (job.status is not JobStatus.PENDING or job_id in self._active
                or (job_id in self._queued and not from_queue)):
            raise InvalidTransitionError(job_id, job.status, JobStatus.RUNNING)
        # 在第一次 await 前登记，防止两个调用同时执行同一个任务。
        self._active[job_id] = asyncio.current_task()
        try:
            async with self._slots:
                job.started_at = utc_now()
                job.transition_to(JobStatus.RUNNING, occurred_at=job.started_at)
                deadline = asyncio.timeout(timeout)
                try:
                    async with deadline:
                        job.result = await job.task()
                except TimeoutError as exc:
                    status = JobStatus.TIMED_OUT if deadline.expired() else JobStatus.FAILED
                    self._finish(job, status, exc)
                except Exception as exc:
                    self._finish(job, JobStatus.FAILED, exc)
                else:
                    self._finish(job, JobStatus.SUCCEEDED)
        except asyncio.CancelledError as exc:
            self._finish(job, JobStatus.CANCELLED, exc)
            # 记录后继续向上传递，让外层知道取消已经发生。
            raise
        finally:
            self._active.pop(job_id, None)
        return job

    async def run_all(self, job_ids: Iterable[str], *, timeout: float | None = None) -> list[Job]:
        if timeout is not None and timeout <= 0:
            raise ValueError("超时秒数必须大于零")
        ids = list(job_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("同一批任务不能包含重复编号")
        jobs = [self.get(job_id) for job_id in ids]
        for job in jobs:
            if (job.status is not JobStatus.PENDING or job.job_id in self._active
                    or job.job_id in self._queued):
                raise InvalidTransitionError(job.job_id, job.status, JobStatus.RUNNING)
        self._queued.update(ids)
        queue: asyncio.Queue[str] = asyncio.Queue()
        for job_id in ids:
            queue.put_nowait(job_id)

        async def worker() -> None:
            while True:
                try:
                    job_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if self.get(job_id).status is JobStatus.CANCELLED:
                        continue
                    try:
                        await asyncio.create_task(self._run(job_id, timeout=timeout, from_queue=True))
                    except asyncio.CancelledError:
                        # 单个任务取消后继续取下一个；整批取消则交给任务组清理。
                        if asyncio.current_task().cancelling():
                            raise
                finally:
                    queue.task_done()

        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.max_concurrency, len(ids))):
                    group.create_task(worker())
        finally:
            self._queued.difference_update(ids)
            # 整批被取消时，尚未取出的任务也必须有明确的结束状态。
            for job in jobs:
                if job.status is JobStatus.PENDING:
                    self._finish(job, JobStatus.CANCELLED)
        return jobs
