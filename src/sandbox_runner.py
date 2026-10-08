"""在有限资源、没有宿主目录挂载的 Docker 容器中执行命令。"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from enum import Enum


class SandboxStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OOM = "oom"
    ERROR = "error"


@dataclass(frozen=True)
class SandboxLimits:
    timeout: float = 10.0
    cpus: float = 0.5
    memory_mb: int = 128
    pids: int = 32

    def __post_init__(self) -> None:
        if min(self.timeout, self.cpus, self.memory_mb, self.pids) <= 0:
            raise ValueError("时间、CPU、内存和进程数限制都必须大于零")


@dataclass
class SandboxResult:
    run_id: str
    status: SandboxStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    container_removed: bool = False


class DockerCommandError(RuntimeError):
    pass


class SandboxRunner:
    def __init__(self, image: str = "repoops-sandbox:learning", *, docker: str = "docker") -> None:
        self.image = image
        self.docker = docker
        self.results: dict[str, SandboxResult] = {}

    async def _cli(self, *args: str, timeout: float = 30, check: bool = True) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self.docker, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        # 持续读取，但只保留末尾 64 KiB，防止日志挤满执行器内存。
        async def read_tail(stream) -> bytes:
            tail = b""
            while chunk := await stream.read(8192):
                tail = (tail + chunk)[-65536:]
            return tail

        async def collect():
            async with asyncio.TaskGroup() as group:
                out = group.create_task(read_tail(process.stdout))
                err = group.create_task(read_tail(process.stderr))
                group.create_task(process.wait())
            return out.result(), err.result()

        try:
            stdout, stderr = await asyncio.wait_for(collect(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        output = stdout.decode(errors="replace")
        error = stderr.decode(errors="replace")
        if check and process.returncode:
            raise DockerCommandError(error or output or f"Docker 退出码：{process.returncode}")
        return process.returncode, output, error

    def create_args(self, name: str, command: list[str], limits: SandboxLimits) -> list[str]:
        return [
            "create", "--name", name, "--pull=never",
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534",
            "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.pids),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=1",
            "--log-opt=compress=false",
            self.image, *command,
        ]

    async def _collect(self, name: str, result: SandboxResult) -> None:
        _, state_json, _ = await self._cli("inspect", "--format={{json .State}}", name)
        state = json.loads(state_json)
        result.exit_code = state["ExitCode"]
        _, result.stdout, result.stderr = await self._cli("logs", "--tail=200", name)
        if result.status not in {SandboxStatus.TIMED_OUT, SandboxStatus.CANCELLED}:
            if state.get("OOMKilled"):
                result.status = SandboxStatus.OOM
            elif result.exit_code == 0:
                result.status = SandboxStatus.SUCCEEDED
            else:
                result.status = SandboxStatus.FAILED

    async def run(self, command: list[str], *, limits: SandboxLimits | None = None,
                  run_id: str | None = None) -> SandboxResult:
        if not command:
            raise ValueError("执行命令不能为空")
        limits = limits or SandboxLimits()
        run_id = run_id or uuid.uuid4().hex
        if run_id in self.results:
            raise ValueError("执行编号已经使用过")
        name = f"repoops-{uuid.uuid4().hex}"
        result = SandboxResult(run_id, SandboxStatus.ERROR)
        self.results[run_id] = result
        created = False
        try:
            await self._cli(*self.create_args(name, command, limits))
            created = True
            await self._cli("start", name)
            try:
                await self._cli("wait", name, timeout=limits.timeout)
            except TimeoutError:
                result.status = SandboxStatus.TIMED_OUT
                await self._cli("kill", name, check=False)
            await self._collect(name, result)
        except asyncio.CancelledError:
            result.status = SandboxStatus.CANCELLED
            if created:
                try:
                    await self._cli("kill", name, check=False)
                    await self._collect(name, result)
                except (DockerCommandError, OSError, TimeoutError) as exc:
                    result.error = str(exc)
            raise
        except (DockerCommandError, OSError, TimeoutError, ValueError, KeyError) as exc:
            result.status = SandboxStatus.ERROR
            result.error = str(exc)
        finally:
            # 即使 create 在取消时尚未返回，也按预先生成的名字尝试删除。
            try:
                code, _, error = await self._cli("rm", "--force", name, check=False)
                result.container_removed = code == 0
                if created and code:
                    result.status = SandboxStatus.ERROR
                    result.error = f"容器清理失败：{error}"
            except (OSError, TimeoutError) as exc:
                result.status = SandboxStatus.ERROR
                result.error = f"容器清理失败：{exc}"
        return result
