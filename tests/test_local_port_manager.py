import json
import tempfile
import unittest
from pathlib import Path

from local_port_manager import PortRegistry, build_port_report


class PortRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.temp_dir.name) / "registry.json"
        self.registry = PortRegistry(self.registry_path, listener_provider=lambda: [])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_registration_persists_project_port_assignment(self):
        project = self.registry.register("quant-dashboard", [5173, 8000])

        self.assertEqual(project["name"], "quant-dashboard")
        self.assertEqual(project["ports"], [5173, 8000])
        stored = json.loads(self.registry_path.read_text())
        self.assertEqual(stored["projects"][project["id"]]["ports"], [5173, 8000])

    def test_another_project_cannot_claim_an_assigned_port(self):
        self.registry.register("api", [8000])

        with self.assertRaisesRegex(ValueError, "已分配"):
            self.registry.register("web", [8000])

    def test_project_cannot_claim_a_port_that_is_already_listening(self):
        registry = PortRegistry(
            self.registry_path,
            listener_provider=lambda: [
                {"port": 9000, "protocol": "tcp", "pid": 44, "process": "node"}
            ],
        )

        with self.assertRaisesRegex(ValueError, "正在被监听"):
            registry.register("web", [9000])

    def test_project_can_register_its_own_existing_listener_by_path(self):
        project_path = Path(self.temp_dir.name) / "running-project"
        project_path.mkdir()
        registry = PortRegistry(
            self.registry_path,
            listener_provider=lambda: [
                {"port": 9001, "protocol": "tcp", "pid": 44, "process": "node", "path": str(project_path)}
            ],
        )

        project = registry.register("running-project", [9001], path=str(project_path))

        self.assertEqual(project["path"], str(project_path.resolve()))

    def test_project_cannot_claim_a_listener_outside_its_path(self):
        project_path = Path(self.temp_dir.name) / "web"
        project_path.mkdir()
        registry = PortRegistry(
            self.registry_path,
            listener_provider=lambda: [
                {"port": 9002, "protocol": "tcp", "pid": 44, "process": "node", "path": "/tmp/other-project"}
            ],
        )

        with self.assertRaisesRegex(ValueError, "正在被监听"):
            registry.register("web", [9002], path=str(project_path))

    def test_broad_parent_or_root_path_cannot_claim_a_child_project_listener(self):
        listener_path = Path(self.temp_dir.name) / "other-project"
        listener_path.mkdir()
        broad_paths = [Path(self.temp_dir.name), Path("/")]

        for index, broad_path in enumerate(broad_paths):
            with self.subTest(path=str(broad_path)):
                registry = PortRegistry(
                    Path(self.temp_dir.name) / "registry-{}.json".format(index),
                    listener_provider=lambda: [
                        {
                            "port": 9010 + index,
                            "protocol": "tcp",
                            "pid": 44,
                            "process": "node",
                            "path": str(listener_path),
                        }
                    ],
                )

                with self.assertRaisesRegex(ValueError, "正在被监听"):
                    registry.register("unrelated-project", [9010 + index], path=str(broad_path))

    def test_filesystem_root_and_user_home_cannot_claim_existing_listeners(self):
        for index, broad_path in enumerate((Path("/"), Path.home())):
            with self.subTest(path=str(broad_path)):
                registry = PortRegistry(
                    Path(self.temp_dir.name) / "broad-registry-{}.json".format(index),
                    listener_provider=lambda path=broad_path, port=9020 + index: [
                        {
                            "port": port,
                            "protocol": "tcp",
                            "pid": 45,
                            "process": "node",
                            "path": str(path),
                        }
                    ],
                )

                with self.assertRaisesRegex(ValueError, "正在被监听"):
                    registry.register("broad-project", [9020 + index], path=str(broad_path))

    def test_project_path_must_be_a_real_directory(self):
        with self.assertRaisesRegex(ValueError, "项目目录必须是已存在的目录"):
            self.registry.register("web", [9003], path=" ")

    def test_project_can_only_validate_its_registered_port(self):
        project = self.registry.register("api", [8000])

        self.assertTrue(self.registry.validate_port(project["id"], 8000)["allowed"])
        with self.assertRaises(PermissionError):
            self.registry.validate_port(project["id"], 8001)

    def test_boolean_is_not_accepted_as_a_port(self):
        project = self.registry.register("one", [1])

        with self.assertRaises(ValueError):
            self.registry.validate_port(project["id"], True)

    def test_manager_assignment_cannot_be_deleted(self):
        self.registry.register_system_port(62000, 123)

        with self.assertRaises(PermissionError):
            self.registry.delete_project("local-port-manager")

        self.assertEqual(self.registry.list_projects()["projects"][0]["ports"], [62000])

    def test_port_report_merges_registered_and_listening_ports(self):
        project = self.registry.register("api", [8000])

        report = build_port_report(
            self.registry,
            listeners=[
                {"port": 8000, "protocol": "tcp", "pid": 31, "process": "uvicorn"},
                {"port": 9000, "protocol": "tcp", "pid": 32, "process": "node", "family": "IPv4", "address": "127.0.0.1:9000", "path": "/tmp/node"},
            ],
        )

        self.assertEqual(report["summary"], {"registered": 1, "listening": 2, "conflicts": 2})
        rows = {row["port"]: row for row in report["ports"]}
        self.assertEqual(rows[8000]["project"], "api")
        self.assertEqual(rows[8000]["status"], "conflict")
        self.assertEqual(rows[8000]["ownership"], "unverified")
        self.assertEqual(rows[9000]["status"], "conflict")
        self.assertEqual(rows[9000]["family"], "IPv4")
        self.assertEqual(rows[9000]["address"], "127.0.0.1:9000")
        self.assertEqual(rows[8000]["pid"], 31)

    def test_legacy_project_without_path_reports_listener_for_verification(self):
        project = self.registry.register("legacy-api", [8003])

        report = build_port_report(
            self.registry,
            listeners=[
                {
                    "port": 8003,
                    "protocol": "tcp",
                    "pid": 35,
                    "process": "uvicorn",
                    "path": "/tmp/legacy-api",
                }
            ],
        )

        self.assertEqual(report["ports"][0]["project"], project["name"])
        self.assertEqual(report["ports"][0]["status"], "conflict")
        self.assertEqual(report["ports"][0]["ownership"], "unverified")
        self.assertEqual(report["summary"]["conflicts"], 1)

    def test_listener_outside_registered_project_path_is_a_conflict(self):
        project_path = Path(self.temp_dir.name) / "owned-project"
        project_path.mkdir()
        self.registry.register("owned", [8001], path=str(project_path))

        report = build_port_report(
            self.registry,
            listeners=[{"port": 8001, "protocol": "tcp", "pid": 33, "process": "node", "path": "/tmp/other"}],
        )

        self.assertEqual(report["ports"][0]["status"], "conflict")
        self.assertEqual(report["ports"][0]["ownership"], "mismatch")
        self.assertEqual(report["summary"]["conflicts"], 1)

    def test_multiple_unregistered_listeners_on_one_port_remain_a_conflict(self):
        report = build_port_report(
            self.registry,
            listeners=[
                {"port": 8002, "protocol": "tcp", "pid": 34, "process": "node", "path": "/tmp/one"},
                {"port": 8002, "protocol": "tcp", "pid": 35, "process": "node", "path": "/tmp/two"},
            ],
        )

        self.assertEqual(report["ports"][0]["status"], "conflict")
        self.assertEqual(len(report["ports"][0]["listeners"]), 2)
        self.assertEqual(report["summary"]["conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
