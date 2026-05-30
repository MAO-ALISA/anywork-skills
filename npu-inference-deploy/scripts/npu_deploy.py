#!/usr/bin/env python3
"""
Safe NPU inference service deployment helper.

This script intentionally uses a plan/apply split:
- plan: read-only remote checks and local plan creation
- apply: remote mutations, gated by --confirm-plan <plan_id>
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import html
import http.server
import json
import os
from pathlib import Path
import queue
import re
import shlex
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse


DEFAULT_STATE_DIR = ".npu_deploy"
DEFAULT_PARALLEL = 4


class DeployError(Exception):
    pass


class ConfigError(DeployError):
    pass


class RemoteCommandError(DeployError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_id(prefix: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{os.getpid()}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_yaml_or_json(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Inventory not found: {path}")
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise ConfigError(
            "PyYAML is required for YAML inventories. Install it with: python -m pip install pyyaml"
        ) from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("Inventory root must be a mapping with defaults and hosts.")
    return data


def read_secret(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.exists():
        raise ConfigError(f"Secret file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def redact_text(value: str | None, secrets: list[str]) -> str:
    if value is None:
        return ""
    redacted = str(value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def redact_obj(value, secrets: list[str]):
    if isinstance(value, dict):
        return {k: redact_obj(v, secrets) for k, v in value.items() if k not in {"password"}}
    if isinstance(value, list):
        return [redact_obj(v, secrets) for v in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def json_print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def sh_quote(value: str) -> str:
    return shlex.quote(str(value))


def format_template(template: str, host: dict) -> str:
    values = dict(host)
    values.setdefault("name", host.get("name") or host.get("ip"))
    try:
        return template.format(**values)
    except KeyError as exc:
        raise ConfigError(f"Unknown placeholder in command template: {exc}") from exc


def normalize_inventory(raw: dict) -> tuple[dict, list[dict]]:
    defaults = raw.get("defaults") or {}
    hosts_raw = raw.get("hosts")
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping.")
    if not isinstance(hosts_raw, list) or not hosts_raw:
        raise ConfigError("hosts must be a non-empty list.")

    hosts: list[dict] = []
    seen: set[str] = set()
    for item in hosts_raw:
        if not isinstance(item, dict):
            raise ConfigError("Each host entry must be a mapping.")
        host = dict(defaults)
        host.update(item)
        if not host.get("ip"):
            raise ConfigError("Each host must define ip.")
        if not host.get("username"):
            raise ConfigError(f"Host {host.get('ip')} must define username.")
        host.setdefault("name", host["ip"])
        host.setdefault("ssh_port", 22)
        host.setdefault("connect_timeout", 10)
        host.setdefault("command_timeout", 600)
        host.setdefault("tags", {})
        host.setdefault("container_name", "npu-inference-service")
        host.setdefault(
            "create_command",
            "docker run -d --name {container_name} --restart unless-stopped --network host {image} tail -f /dev/null",
        )
        host.setdefault("npu_busy_threshold_percent", 10)
        host.setdefault(
            "npu_status_command",
            "if command -v npu-smi >/dev/null 2>&1; then npu-smi info; "
            "elif command -v nvidia-smi >/dev/null 2>&1; then "
            "nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits; "
            "else echo NPU_CHECK_UNAVAILABLE; fi",
        )
        host.setdefault(
            "process_snapshot_command",
            "ps -eo user,pid,pcpu,pmem,comm --sort=-pcpu | head -20",
        )
        key = str(host["name"])
        if key in seen:
            raise ConfigError(f"Duplicate host name: {key}")
        seen.add(key)
        hosts.append(host)
    return defaults, hosts


def validate_deploy_host(host: dict) -> list[str]:
    missing = []
    for key in ["image", "container_name", "workdir", "start_script"]:
        if not host.get(key):
            missing.append(key)
    if not host.get("password_file") and not host.get("key_file"):
        missing.append("password_file or key_file")
    return missing


def host_matches_tags(host: dict, tags: list[str]) -> bool:
    host_tags = host.get("tags") or {}
    for tag in tags:
        if "=" not in tag:
            raise ConfigError(f"Tag filter must use key=value: {tag}")
        key, value = tag.split("=", 1)
        if str(host_tags.get(key)) != value:
            return False
    return True


def select_inventory_hosts(hosts: list[dict], names_or_ips: list[str], tags: list[str]) -> list[dict]:
    selected = hosts
    if names_or_ips:
        lookup = {str(h.get("name")): h for h in hosts}
        lookup.update({str(h.get("ip")): h for h in hosts})
        missing = [name for name in names_or_ips if name not in lookup]
        if missing:
            raise ConfigError(f"Unknown host(s): {', '.join(missing)}")
        ordered = []
        seen: set[str] = set()
        for name in names_or_ips:
            host = lookup[name]
            key = str(host["name"])
            if key not in seen:
                ordered.append(host)
                seen.add(key)
        selected = ordered
    if tags:
        selected = [host for host in selected if host_matches_tags(host, tags)]
    if not selected:
        raise ConfigError("No hosts matched the selection.")
    return selected


class AuditStore:
    def __init__(self, state_dir: Path):
        ensure_dir(state_dir)
        self.path = state_dir / "npu_deploy.sqlite3"
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists tasks (
                id text primary key,
                type text not null,
                status text not null,
                created_at text not null,
                updated_at text not null,
                plan_id text,
                summary text
            );
            create table if not exists task_hosts (
                id integer primary key autoincrement,
                task_id text not null,
                host text not null,
                ip text,
                status text not null,
                started_at text not null,
                ended_at text,
                stdout text,
                stderr text,
                error text,
                metadata_json text,
                foreign key(task_id) references tasks(id)
            );
            """
        )
        self.conn.commit()

    def create_task(self, task_id: str, task_type: str, plan_id: str | None = None) -> None:
        now = utc_now()
        self.conn.execute(
            "insert or replace into tasks(id,type,status,created_at,updated_at,plan_id,summary) values(?,?,?,?,?,?,?)",
            (task_id, task_type, "running", now, now, plan_id, ""),
        )
        self.conn.commit()

    def update_task(self, task_id: str, status: str, summary: dict | str | None = None) -> None:
        if isinstance(summary, (dict, list)):
            summary_value = json.dumps(summary, ensure_ascii=False)
        else:
            summary_value = summary or ""
        self.conn.execute(
            "update tasks set status=?, updated_at=?, summary=? where id=?",
            (status, utc_now(), summary_value, task_id),
        )
        self.conn.commit()

    def add_host_event(
        self,
        task_id: str,
        host: dict,
        status: str,
        stdout: str = "",
        stderr: str = "",
        error: str = "",
        metadata: dict | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            insert into task_hosts(task_id,host,ip,status,started_at,ended_at,stdout,stderr,error,metadata_json)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                str(host.get("name") or host.get("ip")),
                str(host.get("ip") or ""),
                status,
                started_at or utc_now(),
                ended_at or utc_now(),
                stdout,
                stderr,
                error,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def recent_tasks(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "select * from tasks order by created_at desc limit ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def task_hosts(self, task_id: str | None = None, limit: int = 200) -> list[dict]:
        if task_id:
            rows = self.conn.execute(
                "select * from task_hosts where task_id=? order by id desc limit ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "select * from task_hosts order by id desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class SSHRunner:
    def __init__(self, host: dict):
        self.host = host
        self.client = None
        self.secrets: list[str] = []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self) -> None:
        try:
            import paramiko  # type: ignore
        except Exception as exc:
            raise ConfigError(
                "paramiko is required for SSH operations. Install it with: python -m pip install paramiko"
            ) from exc

        password = read_secret(self.host.get("password_file"))
        key_passphrase = read_secret(self.host.get("key_passphrase_file"))
        self.secrets = [s for s in [password, key_passphrase] if s]

        kwargs = {
            "hostname": self.host["ip"],
            "port": int(self.host.get("ssh_port", 22)),
            "username": self.host["username"],
            "timeout": int(self.host.get("connect_timeout", 10)),
            "banner_timeout": int(self.host.get("connect_timeout", 10)),
            "auth_timeout": int(self.host.get("connect_timeout", 10)),
            "look_for_keys": False,
            "allow_agent": False,
        }
        if self.host.get("key_file"):
            kwargs["key_filename"] = str(Path(self.host["key_file"]).expanduser())
            if key_passphrase:
                kwargs["passphrase"] = key_passphrase
        else:
            kwargs["password"] = password

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(**kwargs)
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def run(self, command: str, timeout: int | None = None) -> dict:
        if self.client is None:
            raise RemoteCommandError("SSH client is not connected.")
        effective_timeout = timeout or int(self.host.get("command_timeout", 600))
        started = utc_now()
        stdin, stdout, stderr = self.client.exec_command(command, timeout=effective_timeout)
        del stdin
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return {
            "command": command,
            "exit_code": exit_code,
            "stdout": redact_text(out, self.secrets),
            "stderr": redact_text(err, self.secrets),
            "started_at": started,
            "ended_at": utc_now(),
        }


def classify_npu(output: str, exit_code: int, threshold: int) -> tuple[str, str, dict]:
    text = output or ""
    upper = text.upper()
    meta: dict = {"threshold": threshold}
    if exit_code != 0:
        return "unknown", "NPU status command failed", meta
    if "NPU_BUSY" in upper:
        return "busy", "custom status marker NPU_BUSY", meta
    if "NPU_IDLE" in upper:
        return "idle", "custom status marker NPU_IDLE", meta
    if "NPU_CHECK_UNAVAILABLE" in upper:
        return "unknown", "NPU status command unavailable", meta

    csv_utils: list[int] = []
    csv_memory: list[int] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            csv_utils.append(int(parts[0]))
            csv_memory.append(int(parts[1]))
    if csv_utils:
        meta["utilization_percent"] = csv_utils
        meta["memory_used_mb"] = csv_memory
        if any(value >= threshold for value in csv_utils) or any(value > 0 for value in csv_memory):
            return "busy", "GPU/NPU utilization or memory is in use", meta
        return "idle", "GPU/NPU utilization and memory are zero", meta

    percents = [int(match.group(1)) for match in re.finditer(r"(?<!\d)(\d{1,3})\s*%", text)]
    if percents:
        meta["percent_values"] = percents
        if any(value >= threshold for value in percents):
            return "busy", f"utilization >= {threshold}%", meta
        return "idle", f"all parsed utilization values < {threshold}%", meta

    return "unknown", "could not classify NPU output; use a custom npu_status_command", meta


def check_one_host(host: dict) -> dict:
    started = utc_now()
    result = {
        "host": host.get("name") or host.get("ip"),
        "ip": host.get("ip"),
        "status": "unknown",
        "reachable": False,
        "deployable": False,
        "reason": "",
        "npu_stdout": "",
        "npu_stderr": "",
        "process_stdout": "",
        "process_stderr": "",
        "metadata": {},
        "started_at": started,
        "ended_at": "",
    }
    try:
        with SSHRunner(host) as ssh:
            result["reachable"] = True
            npu = ssh.run(host["npu_status_command"], timeout=int(host.get("command_timeout", 600)))
            result["npu_stdout"] = npu["stdout"]
            result["npu_stderr"] = npu["stderr"]
            status, reason, meta = classify_npu(
                npu["stdout"] + "\n" + npu["stderr"],
                int(npu["exit_code"]),
                int(host.get("npu_busy_threshold_percent", 10)),
            )
            result["status"] = status
            result["reason"] = reason
            result["metadata"]["npu"] = meta
            if host.get("process_snapshot_command"):
                process = ssh.run(host["process_snapshot_command"], timeout=30)
                result["process_stdout"] = process["stdout"]
                result["process_stderr"] = process["stderr"]
            missing = validate_deploy_host(host)
            if missing:
                result["deployable"] = False
                result["reason"] = f"missing deployment field(s): {', '.join(missing)}"
            else:
                result["deployable"] = status == "idle"
    except Exception as exc:
        result["status"] = "unreachable"
        result["reason"] = str(exc)
    result["ended_at"] = utc_now()
    return result


def run_parallel(hosts: list[dict], parallel: int, func) -> list[dict]:
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        future_to_host = {executor.submit(func, host): host for host in hosts}
        for future in concurrent.futures.as_completed(future_to_host):
            results.append(future.result())
    order = {str(host.get("name") or host.get("ip")): index for index, host in enumerate(hosts)}
    results.sort(key=lambda item: order.get(str(item.get("host")), 999999))
    return results


def make_plan(
    selected_hosts: list[dict],
    checks: list[dict],
    count: int | None,
    state_dir: Path,
) -> dict:
    plan_id = safe_id("plan")
    check_by_host = {str(item["host"]): item for item in checks}
    deployable_hosts = []
    plan_hosts = []

    for host in selected_hosts:
        host_key = str(host.get("name") or host.get("ip"))
        check = check_by_host[host_key]
        will_deploy = bool(check.get("deployable"))
        if count is not None:
            will_deploy = False
        entry = {
            "name": host_key,
            "ip": host["ip"],
            "status": check["status"],
            "reason": check["reason"],
            "will_deploy": will_deploy,
            "image": host.get("image"),
            "container_name": host.get("container_name"),
            "create_command": format_template(host["create_command"], host) if host.get("image") else "",
            "workdir": host.get("workdir"),
            "start_script": host.get("start_script"),
            "healthcheck": host.get("healthcheck", ""),
        }
        if check.get("deployable"):
            deployable_hosts.append(entry)
        plan_hosts.append(entry)

    if count is not None:
        if len(deployable_hosts) < count:
            raise DeployError(
                f"Requested {count} idle host(s), but only {len(deployable_hosts)} are deployable."
            )
        selected_names = {entry["name"] for entry in deployable_hosts[:count]}
        for entry in plan_hosts:
            entry["will_deploy"] = entry["name"] in selected_names

    plan = {
        "plan_id": plan_id,
        "created_at": utc_now(),
        "summary": {
            "selected_hosts": len(selected_hosts),
            "deploy_targets": sum(1 for item in plan_hosts if item["will_deploy"]),
            "idle": sum(1 for item in checks if item["status"] == "idle"),
            "busy": sum(1 for item in checks if item["status"] == "busy"),
            "unreachable": sum(1 for item in checks if item["status"] == "unreachable"),
            "unknown": sum(1 for item in checks if item["status"] == "unknown"),
        },
        "hosts": plan_hosts,
        "checks": checks,
    }
    plans_dir = state_dir / "plans"
    ensure_dir(plans_dir)
    plan_path = plans_dir / f"{plan_id}.json"
    plan["plan_path"] = str(plan_path)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return plan


def remote_must(ssh: SSHRunner, command: str, step: str) -> dict:
    result = ssh.run(command)
    if int(result["exit_code"]) != 0:
        raise RemoteCommandError(
            f"{step} failed with exit {result['exit_code']}: {result['stderr'] or result['stdout']}"
        )
    return result


def deploy_one_host(host: dict, plan_entry: dict) -> dict:
    started = utc_now()
    logs: list[dict] = []
    status = "succeeded"
    error = ""
    try:
        with SSHRunner(host) as ssh:
            image = str(host["image"])
            container_name = str(host["container_name"])
            create_command = format_template(str(host["create_command"]), host)
            workdir = str(host["workdir"])
            start_script = str(host["start_script"])

            image_check = ssh.run(f"docker image inspect {sh_quote(image)} >/dev/null 2>&1")
            logs.append({**image_check, "step": "image_inspect"})
            if int(image_check["exit_code"]) != 0:
                logs.append(remote_must(ssh, f"docker pull {sh_quote(image)}", "docker pull"))

            inspect = ssh.run(
                f"docker inspect {sh_quote(container_name)} --format '{{{{.State.Running}}}}' 2>/dev/null"
            )
            logs.append({**inspect, "step": "container_inspect"})
            if int(inspect["exit_code"]) != 0:
                logs.append(remote_must(ssh, create_command, "container create"))
            elif "true" not in inspect["stdout"].strip().lower():
                logs.append(remote_must(ssh, f"docker start {sh_quote(container_name)}", "container start"))

            exec_command = (
                f"docker exec -w {sh_quote(workdir)} {sh_quote(container_name)} "
                f"bash -lc {sh_quote(start_script)}"
            )
            logs.append(remote_must(ssh, exec_command, "service start"))

            if host.get("healthcheck"):
                logs.append(remote_must(ssh, str(host["healthcheck"]), "healthcheck"))
    except Exception as exc:
        status = "failed"
        error = str(exc)

    stdout = "\n\n".join(
        f"## {item.get('step', item.get('command', 'command'))}\n$ {item.get('command', '')}\n{item.get('stdout', '')}"
        for item in logs
    )
    stderr = "\n\n".join(item.get("stderr", "") for item in logs if item.get("stderr"))
    return {
        "host": host.get("name") or host.get("ip"),
        "ip": host.get("ip"),
        "status": status,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "metadata": {"plan_entry": plan_entry, "steps": len(logs)},
        "started_at": started,
        "ended_at": utc_now(),
    }


def load_inventory(args) -> tuple[dict, list[dict]]:
    raw = load_yaml_or_json(Path(args.inventory))
    return normalize_inventory(raw)


def command_check(args) -> int:
    _, hosts = load_inventory(args)
    selected = select_inventory_hosts(hosts, split_csv(args.hosts), args.tag or [])
    store = AuditStore(Path(args.state_dir))
    task_id = safe_id("check")
    store.create_task(task_id, "check")
    checks = run_parallel(selected, args.parallel, check_one_host)
    for item in checks:
        host = {"name": item["host"], "ip": item["ip"]}
        store.add_host_event(
            task_id,
            host,
            item["status"],
            stdout=item.get("npu_stdout", ""),
            stderr=item.get("npu_stderr", ""),
            error=item.get("reason", ""),
            metadata=item.get("metadata", {}),
            started_at=item.get("started_at"),
            ended_at=item.get("ended_at"),
        )
    summary = {
        "task_id": task_id,
        "hosts": len(checks),
        "idle": sum(1 for x in checks if x["status"] == "idle"),
        "busy": sum(1 for x in checks if x["status"] == "busy"),
        "unreachable": sum(1 for x in checks if x["status"] == "unreachable"),
        "unknown": sum(1 for x in checks if x["status"] == "unknown"),
    }
    store.update_task(task_id, "completed", summary)
    output = {"task_id": task_id, "summary": summary, "hosts": checks}
    json_print(output) if args.json else print_human_check(output)
    return 0


def print_human_check(output: dict) -> None:
    print(f"Task: {output['task_id']}")
    for item in output["hosts"]:
        deployable = "deployable" if item.get("deployable") else "skip"
        print(f"- {item['host']} ({item['ip']}): {item['status']} [{deployable}] - {item['reason']}")


def command_plan(args) -> int:
    _, hosts = load_inventory(args)
    selected = select_inventory_hosts(hosts, split_csv(args.hosts), args.tag or [])
    state_dir = Path(args.state_dir)
    store = AuditStore(state_dir)
    task_id = safe_id("plan")
    store.create_task(task_id, "plan")
    checks = run_parallel(selected, args.parallel, check_one_host)
    plan = make_plan(selected, checks, args.count, state_dir)
    for item in checks:
        store.add_host_event(
            task_id,
            {"name": item["host"], "ip": item["ip"]},
            item["status"],
            stdout=item.get("npu_stdout", ""),
            stderr=item.get("npu_stderr", ""),
            error=item.get("reason", ""),
            metadata=item.get("metadata", {}),
            started_at=item.get("started_at"),
            ended_at=item.get("ended_at"),
        )
    store.update_task(task_id, "completed", plan["summary"])
    output = {"task_id": task_id, **plan}
    json_print(output) if args.json else print_human_plan(output)
    return 0


def print_human_plan(plan: dict) -> None:
    print(f"Plan ID: {plan['plan_id']}")
    print(f"Plan path: {plan['plan_path']}")
    print(f"Summary: {json.dumps(plan['summary'], ensure_ascii=False)}")
    print("Hosts:")
    for item in plan["hosts"]:
        marker = "DEPLOY" if item["will_deploy"] else "SKIP"
        print(
            f"- [{marker}] {item['name']} ({item['ip']}): {item['status']} - {item['reason']}"
        )
        if item["will_deploy"]:
            print(f"  image: {item['image']}")
            print(f"  container: {item['container_name']}")
            print(f"  start: cd {item['workdir']} && {item['start_script']}")
            if item.get("healthcheck"):
                print(f"  healthcheck: {item['healthcheck']}")
    print()
    print("To apply after user confirmation:")
    print(
        f"python scripts/npu_deploy.py apply --inventory <hosts.yaml> --plan {plan['plan_path']} --confirm-plan {plan['plan_id']}"
    )


def command_apply(args) -> int:
    plan_path = Path(args.plan)
    if not plan_path.exists():
        raise DeployError(f"Plan file not found: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_id = plan.get("plan_id")
    if args.confirm_plan != plan_id:
        raise DeployError(
            "Refusing to apply: --confirm-plan must exactly match the plan_id from the plan file."
        )
    _, hosts = load_inventory(args)
    host_lookup = {str(host.get("name") or host.get("ip")): host for host in hosts}
    targets = [entry for entry in plan.get("hosts", []) if entry.get("will_deploy")]
    if not targets:
        raise DeployError("Plan contains no deploy targets.")
    missing = [entry["name"] for entry in targets if entry["name"] not in host_lookup]
    if missing:
        raise DeployError(f"Plan references hosts not present in inventory: {', '.join(missing)}")

    task_id = safe_id("apply")
    store = AuditStore(Path(args.state_dir))
    store.create_task(task_id, "apply", plan_id=plan_id)

    def apply_entry(entry: dict) -> dict:
        return deploy_one_host(host_lookup[entry["name"]], entry)

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as executor:
        futures = [executor.submit(apply_entry, entry) for entry in targets]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            store.add_host_event(
                task_id,
                {"name": result["host"], "ip": result["ip"]},
                result["status"],
                stdout=result.get("stdout", ""),
                stderr=result.get("stderr", ""),
                error=result.get("error", ""),
                metadata=result.get("metadata", {}),
                started_at=result.get("started_at"),
                ended_at=result.get("ended_at"),
            )
    succeeded = sum(1 for item in results if item["status"] == "succeeded")
    failed = sum(1 for item in results if item["status"] == "failed")
    status = "completed" if failed == 0 else "failed"
    summary = {"task_id": task_id, "plan_id": plan_id, "succeeded": succeeded, "failed": failed}
    store.update_task(task_id, status, summary)
    output = {"task_id": task_id, "plan_id": plan_id, "summary": summary, "hosts": results}
    json_print(output) if args.json else print_human_apply(output)
    return 0 if failed == 0 else 2


def print_human_apply(output: dict) -> None:
    print(f"Task: {output['task_id']}")
    print(f"Plan: {output['plan_id']}")
    print(f"Summary: {json.dumps(output['summary'], ensure_ascii=False)}")
    for item in output["hosts"]:
        message = f"- {item['host']} ({item['ip']}): {item['status']}"
        if item.get("error"):
            message += f" - {item['error']}"
        print(message)


def command_status(args) -> int:
    store = AuditStore(Path(args.state_dir))
    tasks = store.recent_tasks(args.limit)
    if args.json:
        json_print({"tasks": tasks})
        return 0
    for task in tasks:
        print(
            f"{task['created_at']} {task['id']} {task['type']} {task['status']} plan={task['plan_id'] or '-'}"
        )
        if args.verbose:
            for event in store.task_hosts(task["id"], limit=1000):
                err = f" error={event['error']}" if event["error"] else ""
                print(f"  - {event['host']} {event['status']}{err}")
    return 0


def command_logs(args) -> int:
    _, hosts = load_inventory(args)
    selected = select_inventory_hosts(hosts, [args.host], [])
    host = selected[0]
    command = host.get("log_command")
    if not command:
        command = f"docker logs --tail {int(args.tail)} {sh_quote(str(host['container_name']))} 2>&1"
    with SSHRunner(host) as ssh:
        result = ssh.run(str(command), timeout=int(host.get("command_timeout", 600)))
    if args.json:
        json_print(result)
    else:
        if result["stderr"]:
            print(result["stderr"], file=sys.stderr)
        print(result["stdout"])
    return int(result["exit_code"])


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NPU Deploy Dashboard</title>
  <style>
    :root { color-scheme: light; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #1f2933; }
    header { background: #233142; color: white; padding: 16px 24px; }
    main { padding: 20px 24px; display: grid; gap: 18px; }
    section { background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 16px; }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border-bottom: 1px solid #e6e9ef; padding: 8px; text-align: left; vertical-align: top; }
    th { color: #52606d; font-weight: 600; }
    code, pre { font-family: Consolas, monospace; }
    pre { background: #101820; color: #f8fafc; padding: 12px; overflow: auto; border-radius: 6px; max-height: 360px; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    input, button { font: inherit; padding: 8px 10px; border: 1px solid #cbd2d9; border-radius: 6px; }
    button { background: #2f80ed; color: white; border-color: #2f80ed; cursor: pointer; }
    .failed, .busy, .unreachable { color: #b42318; font-weight: 600; }
    .completed, .succeeded, .idle { color: #067647; font-weight: 600; }
    .running, .unknown { color: #b54708; font-weight: 600; }
  </style>
</head>
<body>
  <header><h1>NPU Deploy Dashboard</h1></header>
  <main>
    <section>
      <h2>Recent Tasks</h2>
      <table id="tasks"><thead><tr><th>Created</th><th>ID</th><th>Type</th><th>Status</th><th>Plan</th><th>Summary</th></tr></thead><tbody></tbody></table>
    </section>
    <section>
      <h2>Host Events</h2>
      <table id="events"><thead><tr><th>Time</th><th>Task</th><th>Host</th><th>Status</th><th>Error</th></tr></thead><tbody></tbody></table>
    </section>
    <section>
      <h2>Service Logs</h2>
      <div class="row">
        <input id="host" placeholder="host name or ip">
        <input id="tail" type="number" value="200" min="1" max="5000">
        <button onclick="loadLogs()">Load Logs</button>
      </div>
      <pre id="logs">Enter a host and load logs.</pre>
    </section>
  </main>
  <script>
    async function getJson(url) {
      const res = await fetch(url);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }
    function cls(status) { return String(status || '').toLowerCase(); }
    function esc(value) {
      return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    async function refresh() {
      const data = await getJson('/api/tasks');
      document.querySelector('#tasks tbody').innerHTML = data.tasks.map(t =>
        `<tr><td>${esc(t.created_at)}</td><td><code>${esc(t.id)}</code></td><td>${esc(t.type)}</td><td class="${cls(t.status)}">${esc(t.status)}</td><td>${esc(t.plan_id || '')}</td><td><code>${esc(t.summary || '')}</code></td></tr>`
      ).join('');
      const events = await getJson('/api/events');
      document.querySelector('#events tbody').innerHTML = events.events.map(e =>
        `<tr><td>${esc(e.started_at)}</td><td><code>${esc(e.task_id)}</code></td><td>${esc(e.host)}<br>${esc(e.ip || '')}</td><td class="${cls(e.status)}">${esc(e.status)}</td><td>${esc(e.error || '')}</td></tr>`
      ).join('');
    }
    async function loadLogs() {
      const host = document.getElementById('host').value;
      const tail = document.getElementById('tail').value || 200;
      document.getElementById('logs').textContent = 'Loading...';
      try {
        const data = await getJson(`/api/logs?host=${encodeURIComponent(host)}&tail=${encodeURIComponent(tail)}`);
        document.getElementById('logs').textContent = (data.stderr ? data.stderr + '\\n' : '') + data.stdout;
      } catch (err) {
        document.getElementById('logs').textContent = String(err);
      }
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    store: AuditStore
    inventory_path: str | None = None

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
                return
            if parsed.path == "/api/tasks":
                self._json(200, {"tasks": self.store.recent_tasks(50)})
                return
            if parsed.path == "/api/events":
                self._json(200, {"events": self.store.task_hosts(limit=300)})
                return
            if parsed.path == "/api/logs":
                if not self.inventory_path:
                    self._json(400, {"error": "dashboard was started without --inventory"})
                    return
                params = urllib.parse.parse_qs(parsed.query)
                host = (params.get("host") or [""])[0]
                tail = int((params.get("tail") or ["200"])[0])
                if not host:
                    self._json(400, {"error": "host is required"})
                    return
                args = argparse.Namespace(inventory=self.inventory_path)
                _, hosts = load_inventory(args)
                selected = select_inventory_hosts(hosts, [host], [])
                h = selected[0]
                command = h.get("log_command") or f"docker logs --tail {tail} {sh_quote(str(h['container_name']))} 2>&1"
                with SSHRunner(h) as ssh:
                    result = ssh.run(str(command), timeout=int(h.get("command_timeout", 600)))
                self._json(200, result)
                return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            self._json(500, {"error": str(exc), "trace": traceback.format_exc(limit=5)})

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def command_dashboard(args) -> int:
    store = AuditStore(Path(args.state_dir))
    DashboardHandler.store = store
    DashboardHandler.inventory_path = args.inventory
    bind = args.bind
    port = int(args.port)
    with socketserver.ThreadingTCPServer((bind, port), DashboardHandler) as httpd:
        print(f"Dashboard listening on http://{bind}:{port}")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe NPU inference deployment over SSH.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_inventory(p):
        p.add_argument("--inventory", default="hosts.yaml", help="Path to hosts.yaml or hosts.json.")
        p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local audit and plan directory.")

    def add_selectors(p):
        p.add_argument("--hosts", help="Comma-separated host names or IP addresses.")
        p.add_argument("--tag", action="append", help="Filter hosts by tag key=value. Repeatable.")
        p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="Max concurrent SSH operations.")
        p.add_argument("--json", action="store_true", help="Emit JSON.")

    p = sub.add_parser("check", help="Check SSH reachability and NPU occupancy.")
    add_common_inventory(p)
    add_selectors(p)
    p.set_defaults(func=command_check)

    p = sub.add_parser("plan", help="Create a deployment plan after read-only checks.")
    add_common_inventory(p)
    add_selectors(p)
    p.add_argument("--count", type=int, help="Select first N reachable and idle hosts.")
    p.set_defaults(func=command_plan)

    p = sub.add_parser("apply", help="Apply a confirmed deployment plan.")
    add_common_inventory(p)
    p.add_argument("--plan", required=True, help="Path to plan JSON.")
    p.add_argument("--confirm-plan", required=True, help="Must exactly match plan_id.")
    p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="Max concurrent SSH operations.")
    p.add_argument("--json", action="store_true", help="Emit JSON.")
    p.set_defaults(func=command_apply)

    p = sub.add_parser("status", help="Show local audit task history.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local audit directory.")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=command_status)

    p = sub.add_parser("logs", help="Fetch service logs from one host.")
    add_common_inventory(p)
    p.add_argument("--host", required=True, help="Host name or IP.")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=command_logs)

    p = sub.add_parser("dashboard", help="Start local web dashboard.")
    p.add_argument("--inventory", default=None, help="Path to inventory for live logs.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local audit directory.")
    p.add_argument("--bind", default="127.0.0.1", help="Bind address. Default keeps dashboard local.")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=command_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
