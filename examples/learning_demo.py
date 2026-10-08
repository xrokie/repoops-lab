"""不连接外部网站，演示并发、429、网络超时和单个任务失败。"""

import asyncio
import json
from dataclasses import asdict

import httpx

from src.async_executor import AsyncJobExecutor
from src.retry_policy import RetryPolicy, request_with_retry


async def main() -> None:
    executor = AsyncJobExecutor(3)
    calls: dict[str, int] = {}
    retries = []

    async def simulated_server(request):
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        print(f"收到请求 {path}，第 {calls[path]} 次")
        await asyncio.sleep(0.05)
        if path == "/rate-limit" and calls[path] == 1:
            return httpx.Response(429)
        if path == "/timeout" and calls[path] == 1:
            raise httpx.ReadTimeout("模拟服务器回包超时", request=request)
        if path == "/bad-input":
            return httpx.Response(400)
        return httpx.Response(200, json={"path": path, "ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(simulated_server)) as client:
        def make_task(path):
            async def task():
                response = await request_with_retry(
                    client, "GET", f"https://demo.invalid{path}",
                    policy=RetryPolicy(base_delay=0.01, jitter=0), records=retries,
                )
                return response.json()
            return task

        for path in ["/rate-limit", "/timeout", "/bad-input"]:
            executor.submit(path, make_task(path))
        jobs = await executor.run_all(["/rate-limit", "/timeout", "/bad-input"])
        print(json.dumps({
            "jobs": [{"id": job.job_id, "status": job.status.value,
                      "result": job.result, "error": job.error_type} for job in jobs],
            "requests": calls, "retries": [asdict(record) for record in retries],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
