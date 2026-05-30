---
name: npu-inference-deploy
description: 基于 hosts.yaml 机器清单，通过 SSH 管理 Docker 主机上的 NPU 推理服务部署与状态查询。适用于用户需要选择空闲 NPU 机器、检查网络连通性和 NPU 占用、生成部署计划、安全拉取镜像/创建或复用容器/执行每台机器的启动脚本、查看审计历史，或打开本地 Dashboard 查询部署状态和服务日志的场景。
---

# NPU 推理服务部署

## 核心规则

始终将 `scripts/npu_deploy.py` 作为执行入口。除非脚本缺少某个必要能力，并且用户明确批准偏离，否则不要临时拼接原始 SSH/Docker 命令来完成部署。

安全流程固定为：

1. 读取用户提供的 `hosts.yaml` 机器清单。
2. 运行 `check` 或 `plan`，检查 SSH 连通性和 NPU 占用。
3. 向用户展示计划，包括因为 `busy` 或 `unreachable` 被跳过的机器。
4. 只有在用户确认后才能运行 `apply`，并且必须传入精确的 `plan_id` 作为确认令牌。
5. 部署后使用 `status`、`logs` 或 `dashboard` 观察任务状态和服务日志。

## 命令

从本 skill 目录运行命令，或传入绝对路径。

真实 SSH/YAML 场景需要先安装依赖：

```bash
python -m pip install -r scripts/requirements.txt
```

常用命令：

```bash
python scripts/npu_deploy.py check --inventory hosts.yaml
python scripts/npu_deploy.py plan --inventory hosts.yaml --count 4
python scripts/npu_deploy.py apply --inventory hosts.yaml --plan .npu_deploy/plans/<plan_id>.json --confirm-plan <plan_id>
python scripts/npu_deploy.py status --state-dir .npu_deploy
python scripts/npu_deploy.py logs --inventory hosts.yaml --host npu-01 --tail 200
python scripts/npu_deploy.py dashboard --inventory hosts.yaml --state-dir .npu_deploy --port 8765
```

机器选择参数：

- 使用 `--hosts npu-01,10.0.0.3` 明确指定机器子集。
- 使用 `--tag cluster=prod --tag model_group=qwen` 按清单标签筛选机器。
- 使用 `--count 4` 在检查后选择前 4 台可达且空闲的机器。
- 使用 `--parallel 4` 限制并发 SSH 操作数量。

## 安全要求

- 禁止打印、存储或回显密码。密码必须放在 skill 目录之外，并通过 `password_file` 引用。
- 禁止在没有先生成 `plan` 的情况下运行 `apply`。
- 禁止绕过 `--confirm-plan <plan_id>` 确认门禁。
- 默认禁止删除、停止、重启或重建已有容器。脚本只会复用已有容器，并启动处于停止状态的容器。
- 除非用户明确要求单独调查，否则将 `busy`、`unreachable` 和 `unknown` 机器视为不可部署。
- 除非用户明确接受对外暴露，否则 Dashboard 必须绑定在 `127.0.0.1`。

## 机器清单和参考文档

使用 `assets/hosts.example.yaml` 作为机器清单模板。

当需要新增或修改以下内容时，阅读 `references/config-schema.md`：

- 机器清单字段
- 默认 Docker 命令行为
- NPU 空闲判断规则
- 健康检查
- 密码/密钥处理
- Dashboard 和审计行为

## 运行说明

- `plan` 只执行远程只读检查，并将本地计划 JSON 写入 `.npu_deploy/plans/`。
- `apply` 会执行远程变更：拉取镜像、在容器缺失时创建容器、在容器停止时启动容器、执行启动脚本，以及可选健康检查。
- Dashboard 历史记录从 `.npu_deploy/npu_deploy.sqlite3` 本地 SQLite 数据库读取。
- 第一版默认以 NPU 占用作为部署门禁。普通进程快照只用于可见性展示，不会阻塞部署，除非自定义检查将机器判定为 `busy`。
