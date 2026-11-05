import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from local_port_manager import PortRegistry, PortScanError
from server import PortScanMonitor


class PortScanMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def registry(self, provider):
        return PortRegistry(Path(self.temp_dir.name) / "registry.json", listener_provider=provider)

    def test_snapshots_do_not_trigger_another_listener_scan(self):
        calls = []
        monitor = PortScanMonitor(self.registry(lambda: calls.append(True) or []))
        monitor.scan()
        monitor.snapshot()
        monitor.snapshot()
        self.assertEqual(len(calls), 1)

    def test_failed_scan_preserves_last_successful_result_and_time(self):
        outcomes = iter([
            [{"port": 8088, "process": "OrbStack"}],
            PortScanError("本机扫描不可用"),
            [],
        ])

        def provider():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monitor = PortScanMonitor(self.registry(provider))
        with patch("server.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)
            successful = monitor.scan()
            clock.now.return_value = datetime(2026, 11, 6, 1, 1, tzinfo=timezone.utc)
            failed = monitor.scan()
        self.assertEqual(failed["ports"], successful["ports"])
        self.assertEqual(failed["scan"]["scanned_at"], successful["scan"]["scanned_at"])
        self.assertEqual(failed["scan"]["error"], "本机扫描不可用")
        self.assertTrue(failed["scan"]["next_scan_at"])
        recovered = monitor.scan()
        self.assertEqual(recovered["ports"], [])
        self.assertIsNone(recovered["scan"]["error"])

    def test_in_progress_scan_does_not_block_reading_the_last_snapshot(self):
        started = threading.Event()
        release = threading.Event()
        read_finished = threading.Event()
        snapshots = []

        def provider():
            started.set()
            release.wait(timeout=2)
            return []

        monitor = PortScanMonitor(self.registry(provider))
        scanning = threading.Thread(target=monitor.scan)
        scanning.start()
        self.assertTrue(started.wait(timeout=1))

        def read():
            snapshots.append(monitor.snapshot())
            read_finished.set()

        reading = threading.Thread(target=read)
        reading.start()
        try:
            self.assertTrue(read_finished.wait(timeout=0.2), "读取快照不应等待扫描完成")
            self.assertTrue(snapshots[0]["scan"]["scanning"])
        finally:
            release.set()
            scanning.join(timeout=2)
            reading.join(timeout=2)
