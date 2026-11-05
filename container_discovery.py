"""以只读方式识别 Docker 与 OrbStack 发布端口的容器信息。"""

import ipaddress
import json
import re
import subprocess


# 只请求端口与 Compose 归属字段，避免读取容器环境变量、命令参数或完整配置。
_INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},'
    '"State":{"Running":{{json .State.Running}}},'
    '"Config":{"Labels":{'
    '"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"com.docker.compose.project.working_dir":'
    '{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}}},'
    '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}}}'
)


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def discover_containers(runner=None):
    """读取运行中容器的发布端口；Docker 不可用时不影响宿主扫描。"""
    runner = runner or subprocess.run
    options = {
        "check": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "universal_newlines": True,
        "timeout": 3,
    }
    try:
        listed = runner(["docker", "ps", "--quiet", "--no-trunc"], **options)
        if listed.returncode != 0:
            return []
        container_ids = [item.strip() for item in listed.stdout.splitlines() if item.strip()]
        if not container_ids:
            return []
        inspected = runner(["docker", "inspect", "--format", _INSPECT_FORMAT] + container_ids, **options)
        if inspected.returncode != 0:
            return []
        payload = [json.loads(line) for line in inspected.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []

    containers = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("State"), dict):
            continue
        if item["State"].get("Running") is not True:
            continue
        network = item.get("NetworkSettings")
        config = item.get("Config")
        if not isinstance(network, dict) or not isinstance(config, dict):
            continue
        published = network.get("Ports")
        labels = config.get("Labels") or {}
        if not isinstance(published, dict) or not isinstance(labels, dict):
            continue
        ports = []
        for container_port, bindings in published.items():
            match = re.fullmatch(r"(\d+)/tcp", container_port)
            if not match or not isinstance(bindings, list):
                continue
            target_port = int(match.group(1))
            if not 1 <= target_port <= 65535:
                continue
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                host_port = binding.get("HostPort")
                host_ip = binding.get("HostIp", "")
                if not isinstance(host_port, str) or not host_port.isdecimal():
                    continue
                if not 1 <= int(host_port) <= 65535 or not isinstance(host_ip, str):
                    continue
                try:
                    normalized_ip = str(ipaddress.ip_address(host_ip)) if host_ip else ""
                except ValueError:
                    continue
                ports.append({
                    "host_ip": normalized_ip,
                    "host_port": int(host_port),
                    "container_port": target_port,
                    "protocol": "tcp",
                })
        name = _text(item.get("Name"))
        if not ports or not name:
            continue
        containers.append({
            "id": _text(item.get("Id")),
            "name": name.lstrip("/"),
            "project": _text(labels.get("com.docker.compose.project")),
            "service": _text(labels.get("com.docker.compose.service")),
            "working_dir": _text(labels.get("com.docker.compose.project.working_dir")),
            "source": "docker",
            "ports": ports,
        })
    return containers


def _is_container_proxy(listener):
    # lsof 默认会截断长进程名，因此同时接受它实际输出的代理进程短名。
    process = (listener.get("process") or "").casefold()
    return process in {
        "orbstack", "docker-proxy", "docker-pr", "com.docke",
        "com.docker.backend", "rootlessport", "vpnkit",
    }


def _matches_binding(listener, binding):
    if listener.get("protocol", "tcp") != "tcp" or listener.get("port") != binding["host_port"]:
        return False
    address = listener.get("address") or ""
    host = address.rsplit(":", 1)[0].strip("[]")
    host_ip = binding["host_ip"]
    family = listener.get("family")
    if host == "*":
        if host_ip in ("", "0.0.0.0"):
            return family != "IPv6"
        return host_ip == "::" and family != "IPv4"
    try:
        listener_ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if host_ip == "":
        return True
    published_ip = ipaddress.ip_address(host_ip)
    if listener_ip.version != published_ip.version:
        return False
    return published_ip.is_unspecified or listener_ip == published_ip


def enrich_listeners_with_containers(listeners, containers=None):
    """为已确认的容器代理监听附加元数据，不将其转为已登记归属。"""
    if containers is None:
        containers = discover_containers() if any(_is_container_proxy(item) for item in listeners) else []
    enriched = []
    for listener in listeners:
        result = dict(listener)
        matches = []
        if _is_container_proxy(listener):
            for container in containers:
                if any(_matches_binding(listener, binding) for binding in container["ports"]):
                    matches.append(
                        {
                            "name": container.get("name"),
                            "project": container.get("project"),
                            "service": container.get("service"),
                            "working_dir": container.get("working_dir"),
                            "source": container.get("source", "docker"),
                        }
                    )
        result["containers"] = matches
        if len(matches) == 1:
            result["container"] = matches[0]
        enriched.append(result)
    return enriched
