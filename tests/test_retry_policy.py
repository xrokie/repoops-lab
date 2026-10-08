import asyncio
import ssl

import httpx
import pytest

from src.retry_policy import RetryPolicy, request_with_retry


def test_429_and_read_timeout_then_success():
    async def scenario():
        calls = 0
        delays = []
        records = []

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            if calls == 2:
                raise httpx.ReadTimeout("模拟读取超时", request=request)
            return httpx.Response(200, json={"ok": True})

        async def sleep(delay):
            delays.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await request_with_retry(
                client, "GET", "https://test.invalid", policy=RetryPolicy(jitter=0),
                sleep=sleep, records=records,
            )
        assert response.json() == {"ok": True}
        assert calls == 3
        assert delays == [1, 0.5]
        assert [record.reason for record in records] == ["429", "ReadTimeout"]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
def test_non_retryable_http_errors_stop_immediately(status):
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(status)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await request_with_retry(client, "GET", "https://test.invalid")
        assert calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("key, dedup, expected", [(None, False, 1), ("same-key", False, 1), ("same-key", True, 3)])
def test_post_requires_actual_server_deduplication(key, dedup, expected):
    async def scenario():
        keys = []

        def handler(request):
            keys.append(request.headers.get("Idempotency-Key"))
            raise httpx.ReadTimeout("写入后回包丢失", request=request)

        async def sleep(delay):
            pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await request_with_retry(client, "POST", "https://test.invalid", json={"value": 1},
                                         idempotency_key=key, server_deduplicates=dedup, sleep=sleep)
        assert keys == [key] * expected

    asyncio.run(scenario())


def test_cancellation_interrupts_backoff_without_retry():
    async def scenario():
        sleeping = asyncio.Event()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(request_with_retry(client, "GET", "https://test.invalid", sleep=sleep))
            await sleeping.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == 1

    asyncio.run(scenario())


def test_server_long_retry_after_is_not_shortened():
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "120"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await request_with_retry(client, "GET", "https://test.invalid")
        assert calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout])
def test_all_four_timeouts_have_bounded_retries(error_type):
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            assert request.extensions["timeout"] == {"connect": 3.0, "read": 10.0, "write": 10.0, "pool": 3.0}
            raise error_type("模拟超时", request=request)

        async def sleep(delay):
            pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(error_type):
                await request_with_retry(client, "GET", "https://test.invalid", sleep=sleep)
        assert calls == 3

    asyncio.run(scenario())


def test_certificate_error_is_not_retried():
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            try:
                raise ssl.SSLCertVerificationError("证书不可信")
            except ssl.SSLCertVerificationError as exc:
                raise httpx.ConnectError("证书验证失败", request=request) from exc

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ConnectError):
                await request_with_retry(client, "GET", "https://test.invalid")
        assert calls == 1

    asyncio.run(scenario())


def test_request_stream_is_not_automatically_replayed():
    async def scenario():
        calls = 0

        async def stream():
            yield b"one-shot"

        def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("回包丢失", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await request_with_retry(client, "POST", "https://test.invalid",
                                         idempotency_key="one", server_deduplicates=True,
                                         content=stream())
        assert calls == 1

    asyncio.run(scenario())
