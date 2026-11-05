"""本地端口管理器的回环 HTTP API 与静态文件服务。"""

import json
import mimetypes
import os
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from local_port_manager import (
    PortConflictError,
    PortRegistry,
    build_port_report,
)


class PortManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self):
        monitor = getattr(self, "scan_monitor", None)
        if monitor is not None:
            monitor.stop()
        super().server_close()


class PortScanMonitor:
    """定时采集监听快照，并串行处理手动扫描请求。"""

    def __init__(self, registry, interval_seconds=15):
        self.registry = registry
        self.interval_seconds = max(0.01, float(interval_seconds))
        self._state_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._report = {"ports": [], "summary": {"registered": 0, "listening": 0, "conflicts": 0}}
        self._scanned_at = None
        self._next_scan_at = None
        self._error = None
        self._scanning = False
        self._thread = None

    def scan(self):
        with self._scan_lock:
            with self._state_lock:
                self._error = None
                self._scanning = True
            try:
                report = build_port_report(self.registry)
            except Exception as exc:
                with self._state_lock:
                    self._next_scan_at = datetime.now().astimezone() + timedelta(seconds=self.interval_seconds)
                    self._error = str(exc) or "无法读取本机监听端口"
                    self._scanning = False
            else:
                with self._state_lock:
                    self._report = report
                    self._scanned_at = datetime.now().astimezone()
                    self._next_scan_at = self._scanned_at + timedelta(seconds=self.interval_seconds)
                    self._error = None
                    self._scanning = False
            return self.snapshot()

    def snapshot(self):
        with self._state_lock:
            return {
                **self._report,
                "scan": {
                    "interval_seconds": self.interval_seconds,
                    "scanned_at": self._scanned_at.isoformat(timespec="seconds") if self._scanned_at else None,
                    "next_scan_at": self._next_scan_at.isoformat(timespec="seconds") if self._next_scan_at else None,
                    "scanning": self._scanning,
                    "error": self._error,
                },
            }

    def _run(self):
        while not self._stop_event.wait(self.interval_seconds):
            self.scan()

    def start(self):
        self.scan()
        self._thread = threading.Thread(target=self._run, name="port-scan-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


def create_server(registry=None, host="127.0.0.1", port=None, static_dir=None, register_system=True, scan_interval_seconds=15):
    if host != "127.0.0.1":
        raise ValueError("服务只能绑定 127.0.0.1")
    registry = registry or PortRegistry()
    web_root = Path(static_dir) if static_dir else Path(__file__).parent / "web"
    monitor = PortScanMonitor(registry, scan_interval_seconds)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalPortManager/1.0"

        def parse_request(self):
            if not super().parse_request():
                return False
            allowed_hosts = {
                "127.0.0.1:{}".format(self.server.server_port),
                "localhost:{}".format(self.server.server_port),
            }
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts:
                self._json(403, {"error": "Host 不属于本机服务地址"})
                return False
            return True

        def log_message(self, fmt, *args):
            sys.stderr.write("[port-manager] " + fmt % args + "\n")

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1024 * 1024:
                    raise ValueError("请求内容过大")
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("请求内容必须是有效 JSON") from exc

        def _check_local_origin(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            allowed = {
                "http://127.0.0.1:{}".format(self.server.server_port),
                "http://localhost:{}".format(self.server.server_port),
            }
            return origin in allowed

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/api/health":
                return self._json(200, {"ok": True, "host": "127.0.0.1"})
            if parsed.path == "/api/projects":
                try:
                    return self._json(200, registry.list_projects())
                except Exception as exc:
                    return self._json(503, {"error": str(exc)})
            if parsed.path == "/api/ports":
                return self._json(200, monitor.snapshot())
            return self._static(parsed.path)

        def do_POST(self):
            parsed = urlsplit(self.path)
            try:
                if not self._check_local_origin():
                    return self._json(403, {"error": "不允许跨来源请求"})
                if parsed.path == "/api/scan":
                    return self._json(200, monitor.scan())
                payload = self._body()
                if not isinstance(payload, dict):
                    raise ValueError("请求内容必须是 JSON 对象")
                if parsed.path == "/api/projects":
                    if not payload.get("path"):
                        raise ValueError("新项目登记必须提供项目目录")
                    project = registry.register(payload.get("name"), payload.get("ports"), path=payload.get("path"))
                    monitor.scan()
                    return self._json(201, project)
                pieces = [unquote(piece) for piece in parsed.path.strip("/").split("/")]
                if len(pieces) == 4 and pieces[0:2] == ["api", "projects"] and pieces[3] == "validate":
                    result = registry.validate_port(pieces[2], payload.get("port"))
                    return self._json(200, result)
                return self._json(404, {"error": "未找到该接口"})
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except PortConflictError as exc:
                return self._json(409, {"error": str(exc)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                return self._json(503 if isinstance(exc, RuntimeError) else 500, {"error": str(exc)})

        def do_DELETE(self):
            if not self._check_local_origin():
                return self._json(403, {"error": "不允许跨来源请求"})
            pieces = [unquote(piece) for piece in urlsplit(self.path).path.strip("/").split("/")]
            if len(pieces) == 3 and pieces[:2] == ["api", "projects"]:
                try:
                    if not registry.delete_project(pieces[2]):
                        return self._json(404, {"error": "项目不存在"})
                    monitor.scan()
                    return self._json(200, {"deleted": True})
                except PermissionError as exc:
                    return self._json(403, {"error": str(exc)})
                except Exception as exc:
                    return self._json(503 if isinstance(exc, RuntimeError) else 500, {"error": str(exc)})
            return self._json(404, {"error": "未找到该接口"})

        def _static(self, request_path):
            if not web_root.is_dir():
                return self._json(404, {"error": "前端文件不存在"})
            relative = unquote(request_path).lstrip("/") or "index.html"
            candidate = (web_root / relative).resolve()
            try:
                candidate.relative_to(web_root.resolve())
            except ValueError:
                return self._json(404, {"error": "未找到该文件"})
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if not candidate.is_file():
                return self._json(404, {"error": "未找到该文件"})
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    candidates = []
    selected_port = port
    if selected_port is None:
        previous = registry.get_system_project()
        previous_port = previous.get("ports", [None])[0] if previous else None
        if previous_port:
            candidates.append(previous_port)
        candidates.append(0)
    else:
        candidates.append(selected_port)

    last_error = None
    for candidate in candidates:
        attempts = 8 if candidate == 0 else 1
        for _ in range(attempts):
            try:
                server = PortManagerServer(("127.0.0.1", candidate), Handler)
            except OSError as exc:
                last_error = exc
                continue
            if register_system:
                try:
                    registry.register_system_port(server.server_port, os.getpid())
                except PortConflictError as exc:
                    server.server_close()
                    last_error = exc
                    if candidate == 0:
                        continue
                    break
                except Exception:
                    server.server_close()
                    raise
            server.scan_monitor = monitor
            monitor.start()
            return server
    raise OSError("无法在本机回环地址绑定可用端口") from last_error


def main():
    registry = PortRegistry()
    server = create_server(registry)
    print("本机端口管理器: http://127.0.0.1:{}".format(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
