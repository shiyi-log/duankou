import json
import subprocess
import unittest
from unittest.mock import patch

from container_discovery import discover_containers, enrich_listeners_with_containers
from local_port_manager import PortRegistry, build_port_report
import tempfile
from pathlib import Path


class ContainerDiscoveryTests(unittest.TestCase):
    def _docker_runner(self, payload, returncode=0):
        def runner(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 3)
            if command == ["docker", "ps", "--quiet", "--no-trunc"]:
                return subprocess.CompletedProcess(command, returncode, "container-id\n", "")
            self.assertEqual(command[:3], ["docker", "inspect", "--format"])
            self.assertEqual(command[4:], ["container-id"])
            return subprocess.CompletedProcess(
                command, returncode, "\n".join(json.dumps(item) for item in payload), ""
            )

        return runner

    def test_compose_metadata_and_tcp_host_port_are_extracted(self):
        containers = discover_containers(
            runner=self._docker_runner(
                [
                    {
                        "Id": "container-id",
                        "Name": "/dns-frontend-1",
                        "State": {"Running": True},
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "dns",
                                "com.docker.compose.service": "frontend",
                                "com.docker.compose.project.working_dir": "/Users/shiyi/Documents/DNS",
                            }
                        },
                        "NetworkSettings": {
                            "Ports": {
                                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8088"}],
                                "53/udp": [{"HostIp": "127.0.0.1", "HostPort": "5353"}],
                            }
                        },
                    }
                ]
            )
        )

        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["name"], "dns-frontend-1")
        self.assertEqual(containers[0]["project"], "dns")
        self.assertEqual(containers[0]["service"], "frontend")
        self.assertEqual(containers[0]["working_dir"], "/Users/shiyi/Documents/DNS")
        self.assertEqual(containers[0]["source"], "docker")
        self.assertEqual(
            containers[0]["ports"],
            [{"host_ip": "127.0.0.1", "host_port": 8088, "container_port": 8080, "protocol": "tcp"}],
        )

    def test_container_name_is_preserved_when_compose_labels_are_missing(self):
        containers = discover_containers(
            runner=self._docker_runner(
                [
                    {
                        "Id": "postgres-id",
                        "Name": "/quant-terminal-postgres",
                        "State": {"Running": True},
                        "Config": {"Labels": {}},
                        "NetworkSettings": {
                            "Ports": {
                                "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5433"}]
                            }
                        },
                    }
                ]
            )
        )

        self.assertEqual(containers[0]["name"], "quant-terminal-postgres")
        self.assertIsNone(containers[0]["project"])
        self.assertIsNone(containers[0]["service"])

    def test_only_running_containers_and_tcp_published_ports_are_candidates(self):
        containers = discover_containers(
            runner=self._docker_runner(
                [
                    {
                        "Id": "stopped",
                        "Name": "/stopped",
                        "State": {"Running": False},
                        "Config": {"Labels": {}},
                        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "8080"}]}},
                    },
                    {
                        "Id": "no-port",
                        "Name": "/no-port",
                        "State": {"Running": True},
                        "Config": {"Labels": {}},
                        "NetworkSettings": {"Ports": {"8080/tcp": None}},
                    },
                ]
            )
        )

        self.assertEqual(containers, [])

    def test_matching_host_address_enriches_listener_without_changing_registration_fields(self):
        containers = [
            {
                "id": "container-id",
                "name": "dns-frontend-1",
                "project": "dns",
                "service": "frontend",
                "working_dir": "/Users/shiyi/Documents/DNS",
                "source": "docker",
                "ports": [
                    {"host_ip": "127.0.0.1", "host_port": 8088, "container_port": 8080, "protocol": "tcp"}
                ],
            }
        ]

        enriched = enrich_listeners_with_containers(
            [
                {
                    "port": 8088,
                    "protocol": "tcp",
                    "address": "127.0.0.1:8088",
                    "process": "OrbStack",
                },
                {
                    "port": 8088,
                    "protocol": "tcp",
                    "address": "*:8088",
                    "process": "node",
                },
            ],
            containers=containers,
        )

        self.assertEqual(enriched[0]["container"]["project"], "dns")
        self.assertEqual(
            enriched[0]["containers"],
            [{
                "name": "dns-frontend-1",
                "project": "dns",
                "service": "frontend",
                "working_dir": "/Users/shiyi/Documents/DNS",
                "source": "docker",
            }],
        )
        self.assertEqual(enriched[1]["containers"], [])
        self.assertNotIn("container", enriched[1])

    def test_multiple_matching_containers_remain_visible_as_ambiguous_list(self):
        containers = [
            {
                "id": "one",
                "name": "one",
                "project": "one",
                "service": "web",
                "working_dir": None,
                "source": "docker",
                "ports": [{"host_ip": "0.0.0.0", "host_port": 9000, "container_port": 80, "protocol": "tcp"}],
            },
            {
                "id": "two",
                "name": "two",
                "project": None,
                "service": None,
                "working_dir": None,
                "source": "docker",
                "ports": [{"host_ip": "0.0.0.0", "host_port": 9000, "container_port": 80, "protocol": "tcp"}],
            },
        ]

        enriched = enrich_listeners_with_containers(
            [{"port": 9000, "protocol": "tcp", "address": "*:9000", "process": "OrbStack"}],
            containers=containers,
        )

        self.assertEqual([item["name"] for item in enriched[0]["containers"]], ["one", "two"])
        self.assertNotIn("container", enriched[0])

    def test_docker_failure_keeps_listeners_available_for_local_scan(self):
        listeners = [{"port": 8080, "protocol": "tcp", "address": "*:8080", "process": "OrbStack"}]

        with patch("container_discovery.subprocess.run", side_effect=FileNotFoundError):
            enriched = enrich_listeners_with_containers(listeners)

        self.assertEqual(enriched[0]["port"], 8080)
        self.assertEqual(enriched[0]["containers"], [])

    def test_report_shows_container_names_without_claiming_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = PortRegistry(Path(directory) / "registry.json", listener_provider=lambda: [])
            report = build_port_report(
                registry,
                listeners=[
                    {
                        "port": 8088,
                        "protocol": "tcp",
                        "pid": 1065,
                        "process": "OrbStack",
                        "address": "127.0.0.1:8088",
                        "container": {
                            "id": "dns-id",
                            "name": "dns-frontend-1",
                            "project": "dns",
                            "service": "frontend",
                            "working_dir": "/Users/shiyi/Documents/DNS",
                            "source": "docker",
                            "ports": [],
                        },
                        "containers": [
                            {
                                "id": "dns-id",
                                "name": "dns-frontend-1",
                                "project": "dns",
                                "service": "frontend",
                                "working_dir": "/Users/shiyi/Documents/DNS",
                                "source": "docker",
                                "ports": [],
                            }
                        ],
                    }
                ],
            )

        port = report["ports"][0]
        self.assertEqual(port["project"], None)
        self.assertEqual(port["status"], "conflict")
        self.assertEqual(port["container"]["name"], "dns-frontend-1")
        self.assertEqual(port["containers"][0]["project"], "dns")

    def test_container_metadata_does_not_override_registered_path_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            project_path = Path(directory) / "registered-project"
            project_path.mkdir()
            registry = PortRegistry(Path(directory) / "registry.json", listener_provider=lambda: [])
            registry.register("registered-project", [9090], path=str(project_path))
            report = build_port_report(
                registry,
                listeners=[
                    {
                        "port": 9090,
                        "protocol": "tcp",
                        "process": "OrbStack",
                        "path": "/tmp/unrelated",
                        "container": {"name": "different-project", "project": "different-project"},
                        "containers": [
                            {"name": "different-project", "project": "different-project"}
                        ],
                    }
                ],
            )

        port = report["ports"][0]
        self.assertEqual(port["project"], "registered-project")
        self.assertEqual(port["status"], "conflict")
        self.assertEqual(port["ownership"], "mismatch")
        self.assertEqual(port["container"]["project"], "different-project")


if __name__ == "__main__":
    unittest.main()
