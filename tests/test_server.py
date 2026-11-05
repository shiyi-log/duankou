import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from local_port_manager import PortRegistry
from server import create_server


class PortManagerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        registry = PortRegistry(Path(self.temp_dir.name) / "registry.json", listener_provider=lambda: [])
        self.server = create_server(registry, static_dir=Path(self.temp_dir.name), register_system=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request_json(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_and_project_registration_routes(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request_json("/api/health")[1]["ok"], True)
        _, project = self.request_json("/api/projects", "POST", {"name": "Web App", "ports": [62001], "path": self.temp_dir.name})
        self.assertEqual(project["ports"], [62001])
        _, projects = self.request_json("/api/projects")
        self.assertEqual(projects["projects"][0]["id"], project["id"])
        _, report = self.request_json("/api/ports")
        self.assertEqual(report["ports"][0]["project"], "Web App")

        self.request_json("/api/projects/{}".format(project["id"]), "DELETE")
        _, report = self.request_json("/api/ports")
        self.assertEqual(report["ports"], [])

    def test_unassigned_project_port_is_rejected_with_forbidden(self):
        _, project = self.request_json("/api/projects", "POST", {"name": "API", "ports": [62002], "path": self.temp_dir.name})

        with self.assertRaises(HTTPError) as raised:
            self.request_json(
                "/api/projects/{}/validate".format(project["id"]),
                "POST",
                {"port": 62003},
            )

        self.assertEqual(raised.exception.code, 403)

    def test_project_cannot_reuse_another_assignment(self):
        self.request_json("/api/projects", "POST", {"name": "First", "ports": [62004], "path": self.temp_dir.name})

        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/projects", "POST", {"name": "Second", "ports": [62004], "path": self.temp_dir.name})

        self.assertEqual(raised.exception.code, 409)

    def test_list_payload_is_rejected_with_bad_request(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/projects", "POST", ["API", 62004])

        self.assertEqual(raised.exception.code, 400)

    def test_cross_origin_write_is_rejected(self):
        request = Request(
            self.base_url + "/api/projects",
            data=b'{"name":"untrusted","ports":[62005]}',
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://example.com"},
        )

        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)

        self.assertEqual(raised.exception.code, 403)

    def test_server_refuses_non_loopback_binding(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_server(host="0.0.0.0", register_system=False)

    def test_registration_api_requires_a_project_path(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/projects", "POST", {"name": "Missing path", "ports": [62006]})

        self.assertEqual(raised.exception.code, 400)

    def test_host_header_must_match_loopback_service(self):
        request = Request(
            self.base_url + "/api/health",
            headers={"Host": "attacker.example"},
        )

        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)

        self.assertEqual(raised.exception.code, 403)

    def test_ports_endpoint_exposes_scan_metadata_and_manual_scan(self):
        status, report = self.request_json("/api/ports")

        self.assertEqual(status, 200)
        self.assertIn("scan", report)
        self.assertGreaterEqual(report["scan"]["interval_seconds"], 1)
        self.assertTrue(report["scan"]["scanned_at"])

        status, manual = self.request_json("/api/scan", "POST", {})

        self.assertEqual(status, 200)
        self.assertTrue(manual["scan"]["scanned_at"])
        self.assertFalse(manual["scan"]["scanning"])

    def test_server_scans_listeners_in_the_background(self):
        calls = []
        registry = PortRegistry(
            Path(self.temp_dir.name) / "background.json",
            listener_provider=lambda: calls.append(time.monotonic()) or [],
        )
        server = create_server(
            registry,
            static_dir=Path(self.temp_dir.name),
            register_system=False,
            scan_interval_seconds=0.05,
        )
        try:
            deadline = time.monotonic() + 1
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(calls), 2)
        finally:
            server.server_close()

    def test_server_reuses_its_registered_port_after_restart(self):
        registry = PortRegistry(Path(self.temp_dir.name) / "stable.json", listener_provider=lambda: [])
        first = create_server(registry, static_dir=Path(self.temp_dir.name))
        port = first.server_port
        first.server_close()

        second = create_server(registry, static_dir=Path(self.temp_dir.name))
        try:
            self.assertEqual(second.server_port, port)
            self.assertEqual(registry.get_system_project()["ports"], [port])
        finally:
            second.server_close()

    def test_server_moves_its_reservation_when_previous_port_is_taken(self):
        registry = PortRegistry(Path(self.temp_dir.name) / "moved.json", listener_provider=lambda: [])
        reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reservation.bind(("127.0.0.1", 0))
        reservation.listen()
        old_port = reservation.getsockname()[1]
        registry.register_system_port(old_port, 123)

        server = create_server(registry, static_dir=Path(self.temp_dir.name))
        try:
            self.assertNotEqual(server.server_port, old_port)
            self.assertEqual(registry.get_system_project()["ports"], [server.server_port])
        finally:
            server.server_close()
            reservation.close()


if __name__ == "__main__":
    unittest.main()
