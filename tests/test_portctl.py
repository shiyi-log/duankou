import argparse
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
from unittest.mock import Mock

import portctl
from local_port_manager import PortRegistry


class PortCtlTests(unittest.TestCase):
    def test_register_passes_canonical_project_path_in_the_atomic_registration(self):
        project_path = Path(tempfile.mkdtemp()).resolve()
        registry = Mock()
        registry.register.return_value = {
            "id": "project-a",
            "name": "web",
            "ports": [8000],
            "path": str(project_path),
        }

        with patch.object(portctl, "PortRegistry", return_value=registry), redirect_stdout(StringIO()):
            result = portctl.main(["register", "--name", "web", "--ports", "8000", "--path", str(project_path)])

        self.assertEqual(result, 0)
        registry.register.assert_called_once_with("web", [8000], path=str(project_path))

    def test_bind_rejects_retargeting_a_bound_project(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            first_path = Path(temporary_directory) / "project-a"
            second_path = Path(temporary_directory) / "project-b"
            first_path.mkdir()
            second_path.mkdir()
            registry = PortRegistry(Path(temporary_directory) / "registry.json", listener_provider=lambda: [])
            project = registry.register("web", [8000], path=str(first_path))

            with self.assertRaisesRegex(portctl.PortctlError, "不能直接改绑"):
                portctl.bind_project_path(registry, project["id"], str(second_path))

    def test_register_accepts_a_listener_owned_by_the_same_project_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "running-project"
            project_path.mkdir()
            registry = PortRegistry(
                Path(temporary_directory) / "registry.json",
                listener_provider=lambda: [
                    {"port": 8000, "protocol": "tcp", "pid": 44, "process": "python", "path": str(project_path)}
                ],
            )
            with patch.object(portctl, "PortRegistry", return_value=registry), redirect_stdout(StringIO()):
                result = portctl.main(
                    ["register", "--name", "running-project", "--ports", "8000", "--path", str(project_path)]
                )

            project = registry.list_projects()["projects"][0]
            self.assertEqual(result, 0)
            self.assertEqual(project["path"], str(project_path.resolve()))
            self.assertEqual(project["ports"], [8000])

    def test_start_requires_every_requested_port_to_belong_to_project(self):
        registry = Mock()
        registry.validate_port.side_effect = [
            {"allowed": True, "port": 8000},
            PermissionError("Port is not assigned to this project"),
        ]

        with self.assertRaises(PermissionError):
            portctl.validate_start(registry, "project-a", [8000, 8001], listeners=[])

    def test_managed_run_rejects_projects_without_a_bound_directory(self):
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000]}]}

        with self.assertRaisesRegex(portctl.PortctlError, "尚未绑定项目目录"):
            portctl.run_managed(
                registry,
                "project-a",
                [8000],
                ["app"],
                listeners=lambda: [],
            )

    def test_registered_project_directory_is_used_as_child_working_directory(self):
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {
            "projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]
        }
        child = Mock(pid=101)
        child.poll.return_value = 0
        spawn = Mock(return_value=child)

        portctl.run_managed(
            registry,
            "project-a",
            [8000],
            ["app"],
            spawn=spawn,
            scan_group=lambda _pgid: [],
            listeners=lambda: [],
        )

        self.assertEqual(spawn.call_args.kwargs["cwd"], str(Path.cwd().resolve()))

    def test_start_rejects_requested_port_already_listening(self):
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]}

        with self.assertRaises(portctl.PortctlError):
            portctl.validate_start(
                registry,
                "project-a",
                [8000],
                listeners=[{"port": 8000, "pid": 9}],
            )

    def test_unregistered_listener_kills_only_the_child_process_group(self):
        process = Mock(pid=4321)
        process.poll.side_effect = [None]
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]}
        group_listeners = [{"port": 9000, "pid": 4322, "process": "child-worker"}]
        spawn = Mock(return_value=process)
        scan_group = Mock(return_value=group_listeners)
        kill_group = Mock()

        with self.assertRaisesRegex(portctl.PortctlError, "未授权端口 9000"):
            portctl.run_managed(
                registry,
                "project-a",
                [8000],
                ["app"],
                keepalive=False,
                spawn=spawn,
                scan_group=scan_group,
                terminate_group=kill_group,
                sleep=lambda _seconds: None,
            )
        kill_group.assert_called_once_with(4321)
        spawn.assert_called_once()
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])

    def test_keepalive_has_a_bounded_restart_count(self):
        processes = [Mock(pid=101), Mock(pid=102), Mock(pid=103)]
        for process in processes:
            process.poll.return_value = 0
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]}
        spawn = Mock(side_effect=processes)

        result = portctl.run_managed(
            registry,
            "project-a",
            [8000],
            ["app"],
            keepalive=True,
            max_restarts=2,
            spawn=spawn,
            scan_group=lambda _pgid: [],
            terminate_group=Mock(),
            sleep=lambda _seconds: None,
            listeners=lambda: [],
        )

        self.assertEqual(result, 0)
        self.assertEqual(spawn.call_count, 3)

    def test_conflicting_port_before_restart_is_reported_as_failure(self):
        process = Mock(pid=101)
        process.poll.return_value = 0
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]}
        listener_provider = Mock(side_effect=[[], [{"port": 8000, "pid": 999}]])

        with self.assertRaisesRegex(portctl.PortctlError, "已在监听"):
            portctl.run_managed(
                registry,
                "project-a",
                [8000],
                ["app"],
                keepalive=True,
                max_restarts=1,
                spawn=Mock(return_value=process),
                scan_group=lambda _pgid: [],
                terminate_group=Mock(),
                sleep=lambda _seconds: None,
                listeners=listener_provider,
            )

    def test_backoff_can_be_interrupted_without_spawning_again(self):
        process = Mock(pid=101)
        process.poll.return_value = 0
        registry = Mock()
        registry.validate_port.return_value = {"allowed": True, "port": 8000}
        registry.list_projects.return_value = {"projects": [{"id": "project-a", "ports": [8000], "path": str(Path.cwd())}]}
        spawn = Mock(return_value=process)

        result = portctl.run_managed(
            registry,
            "project-a",
            [8000],
            ["app"],
            keepalive=True,
            max_restarts=1,
            spawn=spawn,
            scan_group=lambda _pgid: [],
            terminate_group=Mock(),
            sleep=Mock(side_effect=KeyboardInterrupt),
            listeners=lambda: [],
        )

        self.assertEqual(result, 130)
        self.assertEqual(spawn.call_count, 1)

    def test_real_unregistered_child_listener_is_stopped_without_touching_other_socket(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            other_listener = socket.socket()
            other_listener.bind(("127.0.0.1", 0))
            other_listener.listen()
            assigned_probe = socket.socket()
            assigned_probe.bind(("127.0.0.1", 0))
            assigned_port = assigned_probe.getsockname()[1]
            assigned_probe.close()
            registry = PortRegistry(Path(temporary_directory) / "registry.json")
            project = registry.register("受管启动器测试", [assigned_port])
            portctl.bind_project_path(registry, project["id"], temporary_directory)
            command = [
                sys.executable,
                "-c",
                "import socket,time; s=socket.socket(); s.bind((\"127.0.0.1\",0)); s.listen(); time.sleep(10)",
            ]
            try:
                with self.assertRaisesRegex(portctl.PortctlError, "未授权端口"):
                    portctl.run_managed(registry, project["id"], [assigned_port], command, poll_interval=0.05)
                self.assertGreaterEqual(other_listener.fileno(), 0)
                self.assertTrue(other_listener.getsockname()[1])
            finally:
                other_listener.close()


if __name__ == "__main__":
    unittest.main()
