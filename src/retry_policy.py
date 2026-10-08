"""有次数上限的网络重试；只有确认不会重复产生副作用时才重试。"""

import asyncio
import random
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Awaitable, Callable

import httpx


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 10.0
    jitter: float = 0.1
    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    write_timeout: float = 10.0
    pool_timeout: float = 3.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("尝试次数必须大于零")
        if min(self.base_delay, self.max_delay, self.jitter) < 0:
            raise ValueError("等待时间不能为负数")
        if min(self.connect_timeout, self.read_timeout, self.write_timeout, self.pool_timeout) <= 0:
            raise ValueError("网络超时时间必须大于零")

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect_timeout, read=self.read_timeout,
                             write=self.write_timeout, pool=self.pool_timeout)

    def delay(self, attempt: int, retry_after: str | None = None) -> float | None:
        delay = min(self.max_delay, self.base_delay * 2 ** (attempt - 1)
                    + random.uniform(0, self.jitter))
        if retry_after:
            try:
                required = float(retry_after)
            except ValueError:
                try:
                    required = (parsedate_to_datetime(retry_after)
                                - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    required = 0
            if required > self.max_delay:
                return None  # 不愿等这么久就结束，不能提前再次请求。
            delay = max(delay, required)
        return delay


@dataclass(frozen=True)
class RetryRecord:
    attempt: int
    reason: str
    delay: float


RETRYABLE_ERRORS = (
    httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
    httpx.WriteError, httpx.RemoteProtocolError,
)
RETRYABLE_STATUS = {429, 502, 503, 504}


def is_certificate_error(error: BaseException) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


async def request_with_retry(
    client: httpx.AsyncClient, method: str, url: str, *,
    policy: RetryPolicy | None = None,
    idempotency_key: str | None = None,
    server_deduplicates: bool = False,
    records: list[RetryRecord] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs,
) -> httpx.Response:
    """重复请求的安全性依赖接口约定；一个请求编号本身不保证去重。"""
    policy = policy or RetryPolicy()
    method = method.upper()
    safe_to_retry = method in {"GET", "HEAD", "OPTIONS"} or bool(
        idempotency_key and server_deduplicates
    )
    # 流或文件不一定能从头再发送，不能仅凭请求编号就重试。
    if kwargs.get("files") or not isinstance(kwargs.get("content"), (type(None), str, bytes)):
        safe_to_retry = False
    headers = httpx.Headers(kwargs.pop("headers", None))
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    for attempt in range(1, policy.max_attempts + 1):
        try:
            response = await client.request(method, url, headers=headers,
                                            timeout=policy.timeout(), **kwargs)
        except RETRYABLE_ERRORS as exc:
            if not safe_to_retry or is_certificate_error(exc) or attempt == policy.max_attempts:
                raise
            reason = type(exc).__name__
            delay = policy.delay(attempt)
        else:
            if response.status_code not in RETRYABLE_STATUS or not safe_to_retry:
                response.raise_for_status()
                return response
            delay = policy.delay(attempt, response.headers.get("Retry-After"))
            if attempt == policy.max_attempts or delay is None:
                response.raise_for_status()
            reason = str(response.status_code)
            await response.aclose()
        if records is not None:
            records.append(RetryRecord(attempt, reason, delay))
        await sleep(delay)
