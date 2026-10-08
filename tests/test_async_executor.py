import asyncio

import pytest

from src.async_executor import AsyncJobExecutor
from src.executor import InvalidTransitionError, JobStatus


def test_three_jobs_really_overlap_and_obey_limit():
    async def scenario():
        executor = AsyncJobExecutor(3)
        started = 0
        peak = 0
        active = 0
        all_started = asyncio.Event()

        async def task():
            nonlocal started, active, peak
            started += 1
            active += 1
            peak = max(peak, active)
            if started == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), 1)
            active -= 1
            return 42

        for i in range(3):
            executor.submit(str(i), task)
        jobs = await executor.run_all(["0", "1", "2"])
        assert peak == 3
        assert all(job.status is JobStatus.SUCCEEDED and job.result == 42 for job in jobs)
        assert executor._active == {}

    asyncio.run(scenario())


def test_queue_limits_many_jobs_and_failure_does_not_stop_others():
    async def scenario():
        executor = AsyncJobExecutor(2)
        active = peak = 0
        two_started = asyncio.Event()

        async def task():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await asyncio.wait_for(two_started.wait(), 1)
            active -= 1

        async def failure():
            raise ValueError("故意报错")

        executor.submit("bad", failure)
        for i in range(8):
            executor.submit(str(i), task)
        jobs = await executor.run_all(["bad", *map(str, range(8))])
        assert jobs[0].status is JobStatus.FAILED
        assert all(job.status is JobStatus.SUCCEEDED for job in jobs[1:])
        assert peak == 2

    asyncio.run(scenario())


def test_timeout_runs_cleanup_and_records_times():
    async def scenario():
        cleaned = asyncio.Event()

        async def hanging():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        executor = AsyncJobExecutor()
        executor.submit("hang", hanging)
        job = await executor.run("hang", timeout=0.01)
        assert cleaned.is_set()
        assert job.status is JobStatus.TIMED_OUT
        assert job.started_at <= job.finished_at
        assert executor._active == {}

    asyncio.run(scenario())


def test_cancel_batch_cleans_running_and_queued_jobs():
    async def scenario():
        executor = AsyncJobExecutor(1)
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def hanging():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        for i in range(3):
            executor.submit(str(i), hanging)
        batch = asyncio.create_task(executor.run_all(["0", "1", "2"]))
        await started.wait()
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
        assert cleaned.is_set()
        assert all(executor.get(str(i)).status is JobStatus.CANCELLED for i in range(3))
        assert executor._active == {}
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


def test_cancel_one_job_does_not_abandon_queue():
    async def scenario():
        executor = AsyncJobExecutor(1)
        started = asyncio.Event()

        async def hanging():
            started.set()
            await asyncio.Event().wait()

        async def succeeding():
            return "继续执行"

        executor.submit("cancel", hanging)
        executor.submit("next", succeeding)
        batch = asyncio.create_task(executor.run_all(["cancel", "next"]))
        await started.wait()
        assert executor.cancel("cancel")
        jobs = await batch
        assert [job.status for job in jobs] == [JobStatus.CANCELLED, JobStatus.SUCCEEDED]
        assert executor._active == {}

    asyncio.run(scenario())


def test_duplicate_running_and_finished_jobs_are_rejected():
    async def scenario():
        executor = AsyncJobExecutor()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def task():
            started.set()
            await finish.wait()

        executor.submit("one", task)
        first = asyncio.create_task(executor.run("one"))
        await started.wait()
        with pytest.raises(InvalidTransitionError):
            await executor.run("one")
        finish.set()
        await first
        with pytest.raises(InvalidTransitionError):
            await executor.run("one")

    asyncio.run(scenario())


def test_cancel_before_start_does_not_call_function():
    async def scenario():
        async def task():
            pytest.fail("已经取消的任务不应执行")

        executor = AsyncJobExecutor()
        job = executor.submit("pending", task)
        assert executor.cancel("pending")
        assert job.status is JobStatus.CANCELLED
        assert job.started_at is None
        assert job.finished_at is not None
        assert not executor.cancel("pending")
        with pytest.raises(InvalidTransitionError):
            await executor.run("pending")

    asyncio.run(scenario())


def test_task_own_timeout_error_is_failure_not_deadline():
    async def scenario():
        async def task():
            raise TimeoutError("任务内部抛出的错误")

        executor = AsyncJobExecutor()
        executor.submit("internal", task)
        assert (await executor.run("internal", timeout=1)).status is JobStatus.FAILED

    asyncio.run(scenario())


def test_sync_function_and_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        AsyncJobExecutor(0)
    with pytest.raises(TypeError):
        AsyncJobExecutor().submit("sync", lambda: 1)


def test_queued_job_cannot_be_stolen_by_another_batch_or_run():
    async def scenario():
        executor = AsyncJobExecutor(1)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def task():
            started.set()
            await finish.wait()

        executor.submit("first", task)
        executor.submit("queued", task)
        batch = asyncio.create_task(executor.run_all(["first", "queued"]))
        await started.wait()
        with pytest.raises(InvalidTransitionError):
            await executor.run("queued")
        with pytest.raises(InvalidTransitionError):
            await executor.run_all(["queued"])
        finish.set()
        jobs = await batch
        assert all(job.status is JobStatus.SUCCEEDED for job in jobs)
        assert executor._queued == set()

    asyncio.run(scenario())
