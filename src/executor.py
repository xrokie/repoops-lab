"""A minimal, synchronous job executor.

Stage 1 intentionally runs Python functions in the current process. It does
not provide concurrency, persistence, timeouts, or crash recovery yet.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class JobStatus(Enum):
    """All states supported by the Stage 1 executor."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InvalidTransitionError(RuntimeError):
    """Raised when a job is moved to a state that is not allowed."""

    def __init__(
        self,
        job_id: str,
        current_status: JobStatus,
        target_status: JobStatus,
    ) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.target_status = target_status

        super().__init__(
            f"Job {job_id!r} cannot transition "
            f"from {current_status.value!r} to {target_status.value!r}"
        )


ALLOWED_TRANSITIONS: Dict[JobStatus, Set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
}


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TransitionRecord:
    """One successful state change."""

    previous_status: JobStatus
    target_status: JobStatus
    occurred_at: datetime


@dataclass
class Job:
    """A Python function and the information recorded while it runs."""

    job_id: str
    task: Callable[[], Any]
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    transitions: List[TransitionRecord] = field(default_factory=list)

    def transition_to(
        self,
        target_status: JobStatus,
        occurred_at: Optional[datetime] = None,
    ) -> None:
        """Move to ``target_status`` if the state change is allowed."""

        if target_status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError(
                job_id=self.job_id,
                current_status=self.status,
                target_status=target_status,
            )

        changed_at = occurred_at or utc_now()
        previous_status = self.status
        self.status = target_status
        self.transitions.append(
            TransitionRecord(
                previous_status=previous_status,
                target_status=target_status,
                occurred_at=changed_at,
            )
        )


class JobExecutor:
    """Store jobs in memory and execute them one at a time."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}

    def submit(self, job_id: str, task: Callable[[], Any]) -> Job:
        """Create a pending job with a unique ID."""

        if job_id in self._jobs:
            raise ValueError(f"Job {job_id!r} already exists")

        job = Job(job_id=job_id, task=task)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job:
        """Return an existing job, or let ``KeyError`` report a missing ID."""

        return self._jobs[job_id]

    def run(self, job_id: str) -> Job:
        """Run one pending job and record either its result or its error."""

        job = self.get(job_id)

        started_at = utc_now()
        job.transition_to(JobStatus.RUNNING, occurred_at=started_at)
        job.started_at = started_at

        try:
            job.result = job.task()
        except Exception as exc:
            job.error_type = type(exc).__name__
            job.error_message = str(exc)
            finished_at = utc_now()
            job.finished_at = finished_at
            job.transition_to(JobStatus.FAILED, occurred_at=finished_at)
        else:
            finished_at = utc_now()
            job.finished_at = finished_at
            job.transition_to(JobStatus.SUCCEEDED, occurred_at=finished_at)

        return job
