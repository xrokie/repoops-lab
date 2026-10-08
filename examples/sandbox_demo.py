"""运行真实容器，打印各类故障的状态与日志。"""

import asyncio
import json
from dataclasses import asdict

from src.sandbox_runner import SandboxLimits, SandboxRunner


async def main() -> None:
    runner = SandboxRunner()
    for run_id, command, timeout in [
        ("tests", ["python", "-m", "pytest", "-q", "-m", "not docker", "-p", "no:cacheprovider"], 30),
        ("exit-error", ["python", "-c", "import sys; print('故意失败', file=sys.stderr); sys.exit(7)"], 10),
        ("hang", ["python", "-c", "while True: pass"], 1),
        ("oom", ["python", "-c", "blocks=[]\nwhile True: blocks.append(bytearray(8 * 1024 * 1024))"], 15),
    ]:
        result = await runner.run(command, limits=SandboxLimits(timeout=timeout), run_id=run_id)
        record = asdict(result)
        record["status"] = result.status.value
        print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
