import asyncio
import json
import os

import pytest

from src.sandbox_runner import DockerCommandError, SandboxLimits, SandboxRunner, SandboxStatus


class FakeDocker(SandboxRunner):
    """只检查分支和清理规则；不代替真实容器测试。"""

    def __init__(self, *, state=None, hang=False, unavailable=False):
        super().__init__()
        self.calls = []
        self.state = state or {"ExitCode": 0, "OOMKilled": False}
        self.hang = hang
        self.unavailable = unavailable
        self.waiting = asyncio.Event()

    async def _cli(self, *args, timeout=30, check=True):
        self.calls.append(args)
        if args[0] == "create" and self.unavailable:
            raise DockerCommandError("服务不可用")
        if args[0] == "wait" and self.hang:
            self.waiting.set()
            await asyncio.wait_for(asyncio.Event().wait(), timeout)
        if args[0] == "inspect":
            return 0, json.dumps(self.state), ""
        if args[0] == "logs":
            return 0, "任务日志", "错误日志"
        return 0, "", ""


@pytest.mark.parametrize("state, expected", [
    ({"ExitCode": 0, "OOMKilled": False}, SandboxStatus.SUCCEEDED),
    ({"ExitCode": 7, "OOMKilled": False}, SandboxStatus.FAILED),
    ({"ExitCode": 137, "OOMKilled": False}, SandboxStatus.FAILED),
    ({"ExitCode": 137, "OOMKilled": True}, SandboxStatus.OOM),
])
def test_result_uses_inspected_oom_flag_not_just_exit_code(state, expected):
    async def scenario():
        runner = FakeDocker(state=state)
        result = await runner.run(["python", "-c", "print(42)"])
        assert result.status is expected
        assert result.stdout == "任务日志"
        assert result.container_removed
        assert runner.calls[-1][0] == "rm"

    asyncio.run(scenario())


def test_timeout_kills_and_removes_container():
    async def scenario():
        runner = FakeDocker(hang=True)
        result = await runner.run(["hang"], limits=SandboxLimits(timeout=0.01))
        assert result.status is SandboxStatus.TIMED_OUT
        assert "kill" in [call[0] for call in runner.calls]
        assert result.container_removed

    asyncio.run(scenario())


def test_cancel_is_propagated_after_container_cleanup():
    async def scenario():
        runner = FakeDocker(hang=True)
        task = asyncio.create_task(runner.run(["hang"], run_id="cancel-test"))
        await runner.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = runner.results["cancel-test"]
        assert result.status is SandboxStatus.CANCELLED
        assert result.container_removed

    asyncio.run(scenario())


def test_missing_docker_has_an_explicit_error():
    async def scenario():
        result = await FakeDocker(unavailable=True).run(["anything"])
        assert result.status is SandboxStatus.ERROR
        assert "服务不可用" in result.error

    asyncio.run(scenario())


def test_resource_limits_and_no_host_mount():
    args = SandboxRunner().create_args("test", ["python", "-c", "pass"], SandboxLimits())
    for option in ["--network=none", "--read-only", "--pids-limit", "--memory", "--cpus", "--cap-drop=ALL"]:
        assert option in args
    assert "--volume" not in args
    assert "--mount" not in args


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("REPOOPS_DOCKER_TESTS") != "1", reason="设置 REPOOPS_DOCKER_TESTS=1 才运行真实容器测试")
def test_real_docker_failures_and_repository_tests():
    async def scenario():
        runner = SandboxRunner()
        cases = [
            (["python", "-m", "pytest", "-q", "-m", "not docker", "-p", "no:cacheprovider"], SandboxStatus.SUCCEEDED, 30),
            (["python", "-c", "import sys; print('故意失败', file=sys.stderr); sys.exit(7)"], SandboxStatus.FAILED, 10),
            (["python", "-c", "while True: pass"], SandboxStatus.TIMED_OUT, 1),
            (["python", "-c", "blocks=[]\nwhile True: blocks.append(bytearray(8 * 1024 * 1024))"], SandboxStatus.OOM, 15),
            (["python", "-c", "import pathlib; pathlib.Path('/app/pollution').write_text('bad')"], SandboxStatus.FAILED, 10),
            (["python", "-c", "import os\nchildren=[]\nwhile True:\n try:\n  pid=os.fork()\n except OSError:\n  print('进程上限生效', flush=True); break\n if pid == 0:\n  import time; time.sleep(5); os._exit(0)\n children.append(pid)\nfor pid in children: os.waitpid(pid, 0)"], SandboxStatus.SUCCEEDED, 15),
        ]
        for command, expected, timeout in cases:
            result = await runner.run(command, limits=SandboxLimits(timeout=timeout, memory_mb=128, pids=16))
            assert result.status is expected, result
            assert result.container_removed, result
        cancelled = asyncio.create_task(runner.run(
            ["python", "-c", "import time; print('started', flush=True); time.sleep(60)"], run_id="real-cancel"))
        # 等待容器实际启动，而不是只取消尚未开始的本地协程。
        for _ in range(100):
            _, names, _ = await runner._cli("ps", "--filter", "name=repoops-", "--format={{.Names}}")
            if names.strip():
                break
            await asyncio.sleep(0.05)
        else:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            pytest.fail("容器未能启动")
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert runner.results["real-cancel"].status is SandboxStatus.CANCELLED
        assert runner.results["real-cancel"].container_removed

    asyncio.run(scenario())
