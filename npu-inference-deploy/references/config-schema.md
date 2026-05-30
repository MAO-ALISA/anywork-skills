# NPU 推理服务部署配置

## 机器清单结构

使用包含 `defaults` 和 `hosts` 的 YAML 文件。

```yaml
defaults:
  image: "registry.example.com/team/inference:latest"
  container_name: "npu-inference-service"
  create_command: "docker run -d --name {container_name} --restart unless-stopped --network host {image} tail -f /dev/null"
  workdir: "/workspace/service"
  start_script: "./start.sh"
  healthcheck: "curl -fsS http://127.0.0.1:8000/health"

hosts:
  - name: "npu-01"
    ip: "10.0.0.1"
    username: "deploy"
    password_file: "D:/secure/npu-01.password"
    tags:
      cluster: "prod-a"
      model_group: "qwen"
```

单台机器的字段会覆盖 `defaults` 中的同名字段。

部署时需要最终解析出以下字段：

- `ip`
- `username`
- `password_file` 或 `key_file` 二选一
- `image`
- `container_name`
- `workdir`
- `start_script`

可选字段：

- `name`：稳定机器标识，用于 CLI 选择和 Dashboard 展示。
- `ssh_port`：默认值为 `22`。
- `connect_timeout`：SSH 连接超时时间，单位为秒。
- `command_timeout`：远程命令超时时间，单位为秒。
- `tags`：用于机器筛选的字符串键值标签。
- `create_command`：Docker 容器创建命令模板。
- `healthcheck`：服务启动后在远程主机执行的健康检查命令。
- `log_command`：`logs` 子命令使用的远程日志命令；默认使用 `docker logs`。
- `npu_status_command`：`check` 和 `plan` 使用的只读 NPU 状态检查命令。
- `npu_busy_threshold_percent`：判断机器忙碌的利用率阈值。
- `process_snapshot_command`：用于可见性展示的只读进程快照命令。

## 密码和密钥处理

- 将密钥、密码等敏感信息放在 skill 目录之外，并且不要提交到 git。
- `password_file` 文件中只放 SSH 密码本身。
- `key_file` 可以和 `key_passphrase_file` 配合使用。
- CLI 在写入日志或 SQLite 记录前会对已加载的敏感值做脱敏。
- 不要把明文密码直接写进 `hosts.yaml`。

## 部署行为

`apply` 的行为是幂等且保守的：

- 只有当 `docker image inspect` 找不到镜像时才执行拉取。
- 只有当 `docker inspect` 找不到容器时才创建容器。
- 只有当容器存在但未运行时才启动容器。
- 启动服务时使用：

```bash
docker exec -w <workdir> <container_name> bash -lc <start_script>
```

默认 `create_command` 支持 `{image}` 和 `{container_name}` 占位符。自定义命令也可以使用 `{workdir}`，以及机器清单里的字段，例如 `{ip}` 或 `{name}`。

工具默认不会删除、停止、重启或重建容器。

## NPU 空闲判断

默认检查会运行 `npu_status_command`。

状态分类：

- `unreachable`：SSH 连接失败。
- `unknown`：NPU 命令不可用、命令非零退出，或输出无法分类。
- `busy`：解析出的利用率大于或等于 `npu_busy_threshold_percent`。
- `idle`：命令执行成功，且没有任何利用率超过阈值。

如果要用于严格生产环境，建议提供站点自定义的 `npu_status_command`。该命令最好输出明确的利用率百分比，或直接通过包装脚本输出 `NPU_BUSY` / `NPU_IDLE`。

## 本地状态

CLI 只写入本地审计状态：

- `.npu_deploy/plans/<plan_id>.json`
- `.npu_deploy/npu_deploy.sqlite3`

计划和历史记录不得包含密码。它们可以包含机器名、IP、命令、stdout、stderr 和失败摘要。

## Dashboard

启动命令：

```bash
python scripts/npu_deploy.py dashboard --inventory hosts.yaml --state-dir .npu_deploy --port 8765
```

Dashboard 默认监听 `127.0.0.1`，展示以下内容：

- 最近任务
- 每台机器的任务状态
- 失败原因
- 已保存的 stdout/stderr
- 在提供机器清单时，通过 `logs` API 查看实时容器日志
