"""本地项目受管启动与保活命令。"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from local_port_manager import PortRegistry, scan_listening_ports


class PortctlError(RuntimeError):
    """受管启动或端口校验失败。"""


def parse_ports(value):
    """解析逗号或空格分隔的端口号。"""
    try:
        ports = [int(item) for item in re.split(r"[,\s]+", value.strip()) if item]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口号必须是整数") from exc
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise argparse.ArgumentTypeError("端口号范围为 1 到 65535")
    if len(set(ports)) != len(ports):
        raise argparse.ArgumentTypeError("端口号不能重复")
    return ports


def validate_start(registry, project_id, ports, listeners=None):
    """确认本次启动使用的端口均属于项目且当前没有监听者。"""
    if not ports:
        raise PortctlError("至少需要指定一个端口")
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PortctlError("端口号范围为 1 到 65535")
        registry.validate_port(project_id, port)
    if listeners is None:
        listeners = registry.listener_provider()
    busy = sorted({item["port"] for item in listeners if item["port"] in ports})
    if busy:
        raise PortctlError("端口已在监听，拒绝启动：{}".format(", ".join(map(str, busy))))


def bind_project_path(registry, project_id, path):
    """将已登记项目绑定到现存的规范化工作目录。"""
    project_path = Path(path).expanduser().resolve(strict=True)
    if not project_path.is_dir():
        raise PortctlError("项目路径必须是现存目录")
    with registry._locked():
        data = registry._read()
        project = data["projects"].get(project_id)
        if project is None:
            raise PortctlError("找不到项目登记：{}".format(project_id))
        registered_path = project.get("path")
        if registered_path:
            current_path = str(Path(registered_path).expanduser().resolve())
            if current_path != str(project_path):
                raise PortctlError("项目已绑定到其他目录，不能直接改绑；请先释放登记再重新登记")
            return project_path
        project["path"] = str(project_path)
        project["ownership"] = "path-bound"
        registry._write(data)
    return project_path


def project_working_directory(registry, project_id):
    """读取并验证已绑定项目目录；无路径的旧登记必须先绑定。"""
    project = next(
        (item for item in registry.list_projects()["projects"] if item.get("id") == project_id),
        None,
    )
    if project is None:
        raise PortctlError("找不到项目登记：{}".format(project_id))
    if not project.get("path"):
        raise PortctlError("项目尚未绑定项目目录，请先运行 portctl bind")
    try:
        project_path = Path(project["path"]).expanduser().resolve(strict=True)
    except OSError as exc:
        raise PortctlError("已登记的项目目录不存在，请重新绑定") from exc
    if not project_path.is_dir():
        raise PortctlError("已登记的项目路径不是目录，请重新绑定")
    return str(project_path)


def scan_process_group_listeners(pgid):
    """只扫描给定进程组的 TCP 监听端口。"""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-g", str(pgid), "-iTCP", "-sTCP:LISTEN"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortctlError("无法检查受管项目的监听端口") from exc
    if result.returncode not in (0, 1):
        raise PortctlError("无法检查受管项目的监听端口")
    listeners = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split(None, 8)
        if len(columns) < 9:
            continue
        match = re.search(r":(\d+)(?:->.*)?(?:\s+\(LISTEN\))?$", columns[8])
        if not match:
            continue
        listeners.append(
            {
                "port": int(match.group(1)),
                "pid": int(columns[1]) if columns[1].isdigit() else None,
                "process": columns[0],
            }
        )
    return listeners


def terminate_process_group(pgid):
    """向本命令启动的进程组发送温和的终止信号。"""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _stop_child(process, terminate_group):
    """停止本命令启动的进程组并回收直接子进程。"""
    terminate_group(process.pid)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def run_managed(
    registry,
    project_id,
    ports,
    command,
    keepalive=False,
    max_restarts=5,
    spawn=subprocess.Popen,
    scan_group=scan_process_group_listeners,
    terminate_group=terminate_process_group,
    sleep=time.sleep,
    listeners=None,
    poll_interval=0.5,
):
    """在新进程组启动项目，并约束其 TCP 监听端口。"""
    if not command:
        raise PortctlError("请在 -- 后提供启动命令")
    if max_restarts < 0:
        raise PortctlError("最大重启次数不能小于 0")
    current_listeners = listeners if listeners is not None else scan_listening_ports
    validate_start(registry, project_id, ports, current_listeners())
    working_directory = project_working_directory(registry, project_id)
    restarts = 0
    while True:
        try:
            process = spawn(command, cwd=working_directory, start_new_session=True)
        except OSError as exc:
            raise PortctlError("启动失败：{}".format(exc)) from exc
        try:
            while True:
                try:
                    group_listeners = scan_group(process.pid)
                except PortctlError:
                    _stop_child(process, terminate_group)
                    raise
                unauthorized = sorted(
                    {item["port"] for item in group_listeners if item["port"] not in ports}
                )
                if unauthorized:
                    _stop_child(process, terminate_group)
                    raise PortctlError(
                        "项目启动了未授权端口 {}，已停止本次启动".format(", ".join(map(str, unauthorized)))
                    )
                return_code = process.poll()
                if return_code is not None:
                    break
                sleep(poll_interval)
        except KeyboardInterrupt:
            _stop_child(process, terminate_group)
            return 130

        # 旧进程退出后可能遗留子进程；只清理本次创建的进程组。
        if scan_group(process.pid):
            _stop_child(process, terminate_group)
        if not keepalive or restarts >= max_restarts:
            return return_code
        restarts += 1
        try:
            sleep(min(30, 2 ** (restarts - 1)))
        except KeyboardInterrupt:
            return 130
        try:
            validate_start(registry, project_id, ports, current_listeners())
        except (PortctlError, PermissionError) as exc:
            raise PortctlError("保活重启前校验失败：{}".format(exc)) from exc


def _show_projects(registry):
    """以中文显示已登记项目。"""
    print(json.dumps(registry.list_projects(), ensure_ascii=False, indent=2))


def build_parser():
    """构建命令行参数。"""
    parser = argparse.ArgumentParser(description="本地项目端口登记、校验与受管启动")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status", help="查看已登记的项目和端口")

    register_parser = subparsers.add_parser("register", help="登记项目专属端口")
    register_parser.add_argument("--name", required=True, help="项目名称")
    register_parser.add_argument("--ports", required=True, type=parse_ports, help="端口号列表，例如 8000,8001")
    register_parser.add_argument("--path", required=True, help="现存项目目录")

    bind_parser = subparsers.add_parser("bind", help="为已有登记绑定项目目录")
    bind_parser.add_argument("--project", required=True, help="项目登记编号")
    bind_parser.add_argument("--path", required=True, help="现存项目目录")

    check_parser = subparsers.add_parser("check", help="校验端口是否属于项目且未被占用")
    check_parser.add_argument("--project", required=True, help="项目登记编号")
    check_parser.add_argument("--ports", required=True, type=parse_ports, help="端口号列表")

    run_parser = subparsers.add_parser("run", help="受管启动项目服务")
    run_parser.add_argument("--project", required=True, help="项目登记编号")
    run_parser.add_argument("--ports", required=True, type=parse_ports, help="本次启动允许监听的端口")
    run_parser.add_argument("--keepalive", action="store_true", help="异常退出后按退避间隔有限重启")
    run_parser.add_argument("--max-restarts", type=int, default=5, help="最大重启次数，默认 5 次")
    run_parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 后的项目启动命令")
    return parser


def main(argv=None):
    """执行端口管理命令。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = PortRegistry()
    try:
        if args.action == "status":
            _show_projects(registry)
            return 0
        if args.action == "register":
            project_path = Path(args.path).expanduser().resolve(strict=True)
            if not project_path.is_dir():
                raise PortctlError("项目路径必须是现存目录")
            project = registry.register(args.name, args.ports, path=str(project_path))
            print("已登记项目 {}：{}".format(project["name"], ", ".join(map(str, project["ports"]))))
            print("项目目录：{}".format(project["path"]))
            print("项目编号：{}".format(project["id"]))
            return 0
        if args.action == "bind":
            path = bind_project_path(registry, args.project, args.path)
            print("已绑定项目目录：{}".format(path))
            return 0
        if args.action == "check":
            validate_start(registry, args.project, args.ports)
            print("校验通过：项目只能使用已登记且未占用的端口")
            return 0
        command = args.command[1:] if args.command and args.command[0] == "--" else args.command
        return run_managed(
            registry,
            args.project,
            args.ports,
            command,
            keepalive=args.keepalive,
            max_restarts=args.max_restarts,
        )
    except (RuntimeError, PermissionError, ValueError) as exc:
        print("端口管理失败：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
