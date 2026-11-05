"""本机端口归属登记与监听状态清单。"""

import fcntl
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from container_discovery import enrich_listeners_with_containers
from project_discovery import enrich_listeners_with_projects


class PortConflictError(ValueError):
    """端口无法分配给目标项目时抛出。"""


class PortScanError(RuntimeError):
    """无法读取操作系统监听清单时抛出。"""


def default_registry_path():
    return Path.home() / "Library" / "Application Support" / "LocalPortManager" / "registry.json"


def scan_listening_ports():
    """通过 macOS lsof 读取 TCP 监听；扫描不可用时按失败处理。"""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortScanError("无法读取本机 TCP 监听端口") from exc
    if result.returncode not in (0, 1):
        raise PortScanError("无法读取本机 TCP 监听端口")

    listeners = {}
    process_paths = {}
    for line in result.stdout.splitlines()[1:]:
        columns = line.split(None, 8)
        if len(columns) < 9:
            continue
        match = re.search(r":(\d+)(?:->.*)?(?:\s+\(LISTEN\))?$", columns[8])
        if not match:
            continue
        port = int(match.group(1))
        pid = int(columns[1]) if columns[1].isdigit() else None
        listener = {
            "port": port,
            "protocol": "tcp",
            "pid": pid,
            "process": columns[0],
            "family": columns[4],
            "address": re.sub(r"^(?:TCP|UDP)\s+", "", re.sub(r"\s+\(LISTEN\)$", "", columns[8])),
        }
        if pid not in process_paths:
            process_paths[pid] = None
            try:
                cwd_result = subprocess.run(
                    ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    universal_newlines=True,
                    timeout=2,
                )
                for cwd_line in cwd_result.stdout.splitlines():
                    if cwd_line.startswith("n"):
                        process_paths[pid] = cwd_line[1:]
                        break
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process_paths[pid]:
            listener["path"] = process_paths[pid]
        listener["address"] = re.sub(r"\s+\(LISTEN\)$", "", listener["address"])
        listeners[(port, pid, columns[0], listener["family"], listener["address"])] = listener
    ordered = sorted(listeners.values(), key=lambda item: (item["port"], item["pid"] or 0))
    return enrich_listeners_with_projects(enrich_listeners_with_containers(ordered))


def _listener_matches_project(listener_path, project_path):
    """仅认领从项目根目录或该项目 Git 工作区内运行的监听。"""
    try:
        listener_root = Path(listener_path).expanduser().resolve()
        registered_root = Path(project_path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    broad_roots = {Path("/").resolve(), Path.home().resolve()}
    if registered_root in broad_roots:
        return False
    if listener_root == registered_root:
        return True
    try:
        listener_root.relative_to(registered_root)
    except ValueError:
        return False

    try:
        result = subprocess.run(
            ["git", "-C", str(listener_root), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        return Path(result.stdout.strip()).resolve() == registered_root
    except (OSError, RuntimeError):
        return False


class PortRegistry:
    def __init__(self, path=None, listener_provider=None):
        self.path = Path(path) if path else default_registry_path()
        self.listener_provider = listener_provider or scan_listening_ports

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("端口登记文件无法读取；为避免数据丢失，已停止写入") from exc
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            raise RuntimeError("端口登记文件格式无效；为避免数据丢失，已停止写入")
        return data

    def _write(self, data):
        descriptor, temp_name = tempfile.mkstemp(prefix=".registry-", dir=str(self.path.parent))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temp_file:
                json.dump(data, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self.path)
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _validated_ports(ports):
        if not isinstance(ports, list) or not ports:
            raise ValueError("至少登记一个端口")
        normalized = []
        for port in ports:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("端口必须是 1 到 65535 之间的整数")
            if port in normalized:
                raise ValueError("同一项目不能重复登记相同端口")
            normalized.append(port)
        return sorted(normalized)

    @staticmethod
    def _name_key(name):
        return unicodedata.normalize("NFKC", name).strip().casefold()

    def register(self, name, ports, path=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("项目名称不能为空")
        normalized_ports = self._validated_ports(ports)
        normalized_path = None
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise ValueError("项目目录必须是已存在的目录")
            try:
                normalized_path = str(Path(path).expanduser().resolve(strict=True))
            except (OSError, RuntimeError, TypeError) as exc:
                raise ValueError("项目目录必须是已存在的目录") from exc
            if not Path(normalized_path).is_dir():
                raise ValueError("项目目录必须是已存在的目录")
        listeners = self.listener_provider()
        active_ports = {}
        for item in listeners:
            active_ports.setdefault(item["port"], []).append(item)
        with self._locked():
            data = self._read()
            projects = data["projects"]
            for project in projects.values():
                if self._name_key(project["name"]) == self._name_key(name):
                    raise PortConflictError("同名项目已经登记")
            assigned = {
                port: project["name"]
                for project in projects.values()
                for port in project["ports"]
            }
            for port in normalized_ports:
                if port in assigned:
                    raise PortConflictError(
                        "端口 {} 已分配给项目 {}".format(port, assigned[port])
                    )
                if port in active_ports:
                    if not normalized_path:
                        raise PortConflictError("端口 {} 当前正在被监听".format(port))
                    for listener in active_ports[port]:
                        listener_path = listener.get("path")
                        if not listener_path:
                            raise PortConflictError("端口 {} 当前正在被监听".format(port))
                        if not _listener_matches_project(listener_path, normalized_path):
                            raise PortConflictError("端口 {} 当前正在被监听".format(port))
            project_id = uuid.uuid4().hex
            project = {
                "id": project_id,
                "name": name.strip(),
                "ports": normalized_ports,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            if normalized_path:
                project["path"] = normalized_path
                project["ownership"] = "path-bound"
            else:
                project["ownership"] = "unverified"
            projects[project_id] = project
            self._write(data)
            return dict(project)

    def register_system_port(self, port, pid):
        """登记管理器已绑定的回环端口。"""
        self._validated_ports([port])
        with self._locked():
            data = self._read()
            projects = data["projects"]
            system_id = "local-port-manager"
            for project_id, project in projects.items():
                if project_id != system_id and port in project["ports"]:
                    raise PortConflictError(
                        "端口 {} 已分配给项目 {}".format(port, project["name"])
                    )
            projects[system_id] = {
                "id": system_id,
                "name": "Local Port Manager",
                "ports": [port],
                "pid": pid,
                "system": True,
                "ownership": "verified",
                "path": str(Path.cwd().resolve()),
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._write(data)
            return dict(projects[system_id])

    def list_projects(self):
        with self._locked():
            projects = self._read()["projects"].values()
            normalized = []
            for item in projects:
                project = dict(item)
                project.setdefault("ownership", "path-bound" if project.get("path") else "unverified")
                normalized.append(project)
            return {"projects": sorted(normalized, key=lambda item: item["name"].casefold())}

    def validate_port(self, project_id, port):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("端口必须是 1 到 65535 之间的整数")
        with self._locked():
            project = self._read()["projects"].get(project_id)
            if project is None or port not in project["ports"]:
                raise PermissionError("该项目未登记此端口")
            return {"allowed": True, "project": project["name"], "port": port}

    def get_system_project(self):
        with self._locked():
            project = self._read()["projects"].get("local-port-manager")
            return dict(project) if project else None

    def delete_project(self, project_id):
        if project_id == "local-port-manager":
            raise PermissionError("不能删除端口管理器自身的端口登记")
        with self._locked():
            data = self._read()
            project = data["projects"].pop(project_id, None)
            if project is None:
                return False
            self._write(data)
            return True


def build_port_report(registry, listeners=None):
    if listeners is None:
        listeners = registry.listener_provider()
    projects = registry.list_projects()["projects"]
    owners = {
        port: project["name"]
        for project in projects
        for port in project["ports"]
    }
    project_by_port = {
        port: project
        for project in projects
        for port in project["ports"]
    }

    def merge_container_details(row, listener):
        """聚合探测结果，但不改变项目登记和冲突判定字段。"""
        detected = listener.get("containers")
        if not isinstance(detected, list):
            detected = []
        single = listener.get("container")
        if isinstance(single, dict) and not detected:
            detected = [single]
        for container in detected:
            if not isinstance(container, dict):
                continue
            display = {
                key: container.get(key)
                for key in ("name", "project", "service", "working_dir", "source")
            }
            identity = (display["name"], display["project"], display["service"])
            if all(
                (item.get("name"), item.get("project"), item.get("service")) != identity
                for item in row["containers"]
            ):
                row["containers"].append(display)
        row["container"] = row["containers"][0] if len(row["containers"]) == 1 else None

    def merge_project_details(row, listener):
        """仅为未登记端口聚合 Git 提示，不重复覆盖登记项目名称。"""
        if row.get("project") is not None:
            for item in row["listeners"]:
                item.pop("detected_project", None)
            return
        detected = listener.get("detected_project")
        if not isinstance(detected, dict):
            return
        display = {
            key: detected.get(key)
            for key in ("name", "path", "source")
        }
        identity = (display["name"], display["path"], display["source"])
        if all(
            (item.get("name"), item.get("path"), item.get("source")) != identity
            for item in row["detected_projects"]
        ):
            row["detected_projects"].append(display)
        row["detected_project"] = (
            row["detected_projects"][0] if len(row["detected_projects"]) == 1 else None
        )

    rows = {}
    for project in projects:
        for port in project["ports"]:
            rows[port] = {
                "port": port,
                "protocol": "tcp",
                "project": project["name"],
                "status": "registered",
                "pid": None,
                "process": None,
                "ownership": "unverified" if not project.get("path") else "expected",
                "path": project.get("path"),
                "listener_path": None,
                "family": None,
                "address": None,
                "listeners": [],
                "container": None,
                "containers": [],
                "detected_project": None,
                "detected_projects": [],
            }
    for listener in listeners:
        port = listener["port"]
        row = rows.get(port)
        if row is None:
            row = {
                "port": port,
                "protocol": listener.get("protocol", "tcp"),
                "project": None,
                "status": "conflict",
                "pid": listener.get("pid"),
                "process": listener.get("process"),
                "ownership": "unregistered",
                "path": listener.get("path"),
                "listener_path": listener.get("path"),
                "family": listener.get("family"),
                "address": listener.get("address"),
                "listeners": [dict(listener)],
                "container": None,
                "containers": [],
                "detected_project": None,
                "detected_projects": [],
            }
            rows[port] = row
        else:
            row["listeners"].append(dict(listener))
            row.update(
                pid=listener.get("pid"),
                process=listener.get("process"),
                listener_path=listener.get("path"),
                family=listener.get("family"),
                address=listener.get("address"),
            )
        merge_container_details(row, listener)
        merge_project_details(row, listener)
        if port not in project_by_port:
            continue
        project_path = project_by_port[port].get("path")
        listener_path = listener.get("path")
        if project_path:
            if not listener_path:
                row.update(status="conflict", ownership="unverified")
                continue
            if _listener_matches_project(listener_path, project_path):
                if row["status"] != "conflict":
                    row.update(status="listening", ownership="verified")
            else:
                row.update(status="conflict", ownership="mismatch")
        else:
            row.update(status="conflict", ownership="unverified")
    port_rows = [rows[port] for port in sorted(rows)]
    return {
        "ports": port_rows,
        "summary": {
            "registered": len(owners),
            "listening": len({item["port"] for item in listeners}),
            "conflicts": sum(1 for item in port_rows if item["status"] == "conflict"),
        },
    }
