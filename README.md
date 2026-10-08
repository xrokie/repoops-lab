# RepoOps 学习项目

当前版本能执行普通 Python 函数，也能并发执行异步任务、有限次数地重试网络请求，以及在 Docker 容器中运行测试命令。

对应学习计划：2026 年 10 月 6 日的异步与网络重试、10 月 7 日的隔离执行。10 月 8 日的机器人模型评测是另一条学习线，知识讲解与实际完成范围见 [三天学习讲解](notes/2026-10-06-to-08.md)。

2026-10-08 实测：全部 42 项测试通过，其中包括真实容器故障测试；容器内的 41 项普通测试也通过。本次创建的容器全部删除。记录见 [测试结果](reports/2026-10-08-tests.txt)、[异步演示](reports/2026-10-08-async-demo.txt)、[容器状态与日志](reports/2026-10-08-sandbox.jsonl)。机器人模型评测尚未执行。

## 安装与测试

需要 Python 3.11 或以上。先建立项目自己的 Python 环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

最后一条命令执行普通测试，并明确跳过需要 Docker 的真实容器测试。当前这台电脑的系统 Python 没有测试工具，已安装的环境可以这样运行：

```sh
/Users/xirui/miniconda3/bin/python3 -m pytest -q
```

## 运行示例

在仓库根目录执行：

```sh
.venv/bin/python -m examples.learning_demo
```

三个模拟请求同时开始：429 表示访问太频繁，稍后重试；读取超时也重试；400 表示请求参数有问题，直接失败。整个例子不访问外部网站。

## 真实容器测试

先启动 Docker Desktop，然后构建镜像。镜像就是容器启动所需的文件与软件包：

```sh
docker build -t repoops-sandbox:learning .
REPOOPS_DOCKER_TESTS=1 .venv/bin/python -m pytest -q
.venv/bin/python -m examples.sandbox_demo
```

测试覆盖：容器内运行仓库测试、非零退出、无限循环超时、内存耗尽、取消、只读目录、进程数量限制。真实容器测试失败时会报错，不会因为 Docker 不可用而自动跳过。

如果本机 Docker 登录工具导致构建一直等待，可以临时使用单独的配置目录构建，不修改原配置：

```sh
mkdir -p /tmp/repoops-docker-config
docker --config /tmp/repoops-docker-config --host unix:///Users/xirui/.docker/run/docker.sock build -t repoops-sandbox:learning .
```

这个地址是当前 Mac 的 Docker 服务地址，其他电脑需要使用自己的地址。

## 代码入口

| 文件 | 用途 |
| --- | --- |
| `src/executor.py` | 任务信息、状态规则、原有的同步执行器 |
| `src/async_executor.py` | 等待队列、并发上限、任务组、超时、取消 |
| `src/retry_policy.py` | 四种网络超时、重试次数、等待时间、请求去重约定 |
| `src/sandbox_runner.py` | 容器创建、资源限制、执行、日志收集和清理 |
| `tests/` | 普通测试和真实容器测试 |

当前任务记录仍保存在内存中；执行器整个进程退出后无法恢复。异步超时依赖任务主动交出执行机会；不会等待的无限循环应放进容器。网络重试不会自动证明远端支持请求去重，需要接口本身提供这个保证。
