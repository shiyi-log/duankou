import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_port_manager import PortRegistry, build_port_report
from project_discovery import enrich_listeners_with_projects


class ProjectDiscoveryTests(unittest.TestCase):
    def test_exact_git_root_is_reported_as_a_discovery_hint(self):
        project_path = Path("/tmp/cc-switch")
        resolved_project_path = project_path.resolve()
        result = subprocess.CompletedProcess([], 0, str(resolved_project_path) + "\n", "")
        listener = {"port": 9999, "process": "python", "path": str(project_path)}
        with patch("project_discovery.subprocess.run", return_value=result) as run:
            enriched = enrich_listeners_with_projects([listener])

        run.assert_called_once()
        self.assertNotIn("project", enriched[0])
        self.assertEqual(enriched[0]["detected_project"], {
            "name": "cc-switch", "path": str(resolved_project_path), "source": "git",
        })

    def test_nested_directory_is_not_claimed_as_project_root(self):
        listener = {"port": 9998, "path": "/tmp/project/src"}
        result = subprocess.CompletedProcess([], 0, "/tmp/project\n", "")
        with patch("project_discovery.subprocess.run", return_value=result):
            enriched = enrich_listeners_with_projects([listener])

        self.assertNotIn("detected_project", enriched[0])

    def test_git_failure_leaves_listener_unidentified(self):
        listener = {"port": 9997, "path": "/tmp/not-a-project"}
        result = subprocess.CompletedProcess([], 128, "", "not a git repository")
        with patch("project_discovery.subprocess.run", return_value=result):
            enriched = enrich_listeners_with_projects([listener])

        self.assertNotIn("detected_project", enriched[0])

    def test_git_timeout_leaves_listener_available_without_detection(self):
        listener = {"port": 9996, "path": "/tmp/slow-project"}
        with patch(
            "project_discovery.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 2),
        ):
            enriched = enrich_listeners_with_projects([listener])

        self.assertEqual(enriched[0]["port"], 9996)
        self.assertNotIn("detected_project", enriched[0])

    def test_repeated_listener_processes_share_one_git_lookup(self):
        project_path = Path("/tmp/cc-switch")
        result = subprocess.CompletedProcess([], 0, str(project_path) + "\n", "")
        listeners = [
            {"port": 9999, "pid": 20, "path": str(project_path)},
            {"port": 9998, "pid": 21, "path": str(project_path)},
        ]
        with patch("project_discovery.subprocess.run", return_value=result) as run:
            enriched = enrich_listeners_with_projects(listeners)

        run.assert_called_once()
        self.assertEqual(
            [item["detected_project"]["name"] for item in enriched],
            ["cc-switch", "cc-switch"],
        )

    def test_home_and_filesystem_root_are_not_discovered(self):
        home = Path("/Users/example")
        listeners = [
            {"port": 10001, "path": str(home)},
            {"port": 10002, "path": "/"},
        ]
        with patch("project_discovery.Path.home", return_value=home) as home_lookup, patch(
            "project_discovery.subprocess.run"
        ) as run:
            enriched = enrich_listeners_with_projects(listeners)

        home_lookup.assert_called_once()
        run.assert_not_called()
        self.assertTrue(all("detected_project" not in item for item in enriched))

    def test_container_listener_is_not_assigned_a_git_project(self):
        listener = {
            "port": 8088,
            "path": "/tmp/project",
            "containers": [{"name": "web", "project": "compose-app"}],
        }
        with patch("project_discovery.subprocess.run") as run:
            enriched = enrich_listeners_with_projects([listener])

        run.assert_not_called()
        self.assertNotIn("detected_project", enriched[0])

    def test_port_report_aggregates_detection_hints_without_claiming_project(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = PortRegistry(Path(directory) / "registry.json", listener_provider=lambda: [])
            report = build_port_report(
                registry,
                listeners=[
                    {
                        "port": 9999,
                        "protocol": "tcp",
                        "process": "python",
                        "detected_project": {
                            "name": "cc-switch", "path": "/Users/shiyi/cc-switch", "source": "git",
                        },
                    }
                ],
            )

        port = report["ports"][0]
        self.assertIsNone(port["project"])
        self.assertEqual(port["status"], "conflict")
        self.assertEqual(port["detected_project"]["name"], "cc-switch")
        self.assertEqual(port["detected_projects"], [
            {"name": "cc-switch", "path": "/Users/shiyi/cc-switch", "source": "git"},
        ])

    def test_registered_port_does_not_repeat_detected_project_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = PortRegistry(Path(directory) / "registry.json", listener_provider=lambda: [])
            registry.register("registered-project", [9995])
            report = build_port_report(
                registry,
                listeners=[
                    {
                        "port": 9995,
                        "protocol": "tcp",
                        "process": "python",
                        "detected_project": {
                            "name": "cc-switch", "path": "/Users/shiyi/cc-switch", "source": "git",
                        },
                    }
                ],
            )

        port = report["ports"][0]
        self.assertEqual(port["project"], "registered-project")
        self.assertIsNone(port["detected_project"])
        self.assertEqual(port["detected_projects"], [])
        self.assertNotIn("detected_project", port["listeners"][0])


if __name__ == "__main__":
    unittest.main()
