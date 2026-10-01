import pytest

from src.executor import InvalidTransitionError, JobExecutor, JobStatus


def test_job_succeeds_and_records_result() -> None:
    executor = JobExecutor()
    executor.submit("success", lambda: 40 + 2)

    job = executor.run("success")

    assert job.status is JobStatus.SUCCEEDED
    assert job.result == 42
    assert job.error_type is None
    assert job.error_message is None
    assert job.started_at is not None
    assert job.finished_at is not None
    assert [record.target_status for record in job.transitions] == [
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
    ]


def test_job_failure_is_recorded() -> None:
    def failing_task() -> None:
        raise ValueError("broken input")

    executor = JobExecutor()
    executor.submit("failure", failing_task)

    job = executor.run("failure")

    assert job.status is JobStatus.FAILED
    assert job.result is None
    assert job.error_type == "ValueError"
    assert job.error_message == "broken input"
    assert [record.target_status for record in job.transitions] == [
        JobStatus.RUNNING,
        JobStatus.FAILED,
    ]


def test_invalid_transition_is_rejected() -> None:
    executor = JobExecutor()
    job = executor.submit("invalid-transition", lambda: None)

    with pytest.raises(InvalidTransitionError, match="cannot transition"):
        job.transition_to(JobStatus.SUCCEEDED)

    assert job.status is JobStatus.PENDING
    assert job.transitions == []


def test_completed_job_cannot_run_twice() -> None:
    call_count = 0

    def counted_task() -> str:
        nonlocal call_count
        call_count += 1
        return "done"

    executor = JobExecutor()
    executor.submit("run-once", counted_task)
    executor.run("run-once")

    with pytest.raises(InvalidTransitionError, match="cannot transition"):
        executor.run("run-once")

    assert call_count == 1
    assert executor.get("run-once").status is JobStatus.SUCCEEDED


def test_executor_continues_after_a_failed_job() -> None:
    def failing_task() -> None:
        raise RuntimeError("first job failed")

    executor = JobExecutor()
    executor.submit("first", failing_task)
    executor.submit("second", lambda: "second job finished")

    first_job = executor.run("first")
    second_job = executor.run("second")

    assert first_job.status is JobStatus.FAILED
    assert second_job.status is JobStatus.SUCCEEDED
    assert second_job.result == "second job finished"
