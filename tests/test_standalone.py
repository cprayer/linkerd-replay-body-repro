import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from standalone import LocalRun


class LocalRunTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repro = LocalRun(Path(temporary.name))
        capture = patch("standalone.capture")
        capture.start()
        self.addCleanup(capture.stop)
        (self.repro.out / "refused-before-listeners.json").write_text(
            json.dumps({"outbound": "127.0.0.1:4140", "admin": "127.0.0.1:4191"}))
        self.server, self.proxy = Mock(), Mock()
        self.server.poll.return_value = None
        self.proxy.poll.return_value = None

    def test_client_failure_stops_both_processes(self):
        self.repro.command = Mock(side_effect=RuntimeError("client timed out"))
        with patch("standalone.subprocess.Popen", side_effect=[self.server, self.proxy]), \
                patch("standalone.wait_until"), patch("standalone.free_port", return_value=8080):
            with self.assertRaisesRegex(RuntimeError, "client timed out"):
                self.repro.scenario("refused", 10, 500, "before")
        for process in (self.server, self.proxy):
            process.terminate.assert_called_once()
            process.wait.assert_called_once()

    def test_startup_failure_stops_started_server(self):
        with patch("standalone.subprocess.Popen", return_value=self.server) as start, \
                patch("standalone.wait_until", side_effect=RuntimeError("server not ready")), \
                patch("standalone.free_port", return_value=8080):
            with self.assertRaisesRegex(RuntimeError, "server not ready"):
                self.repro.scenario("refused", 10, 500, "before")
        start.assert_called_once()
        self.server.terminate.assert_called_once()

    def test_missing_metrics_fails_and_cleans_up(self):
        self.repro.command = Mock(return_value=Mock(returncode=0))
        with patch("standalone.subprocess.Popen", side_effect=[self.server, self.proxy]), \
                patch("standalone.wait_until"), patch("standalone.free_port", return_value=8080), \
                patch("standalone.urllib.request.urlopen", side_effect=OSError("metrics unavailable")):
            with self.assertRaisesRegex(OSError, "metrics unavailable"):
                self.repro.scenario("refused", 10, 500, "before")
        self.server.terminate.assert_called_once()
        self.proxy.terminate.assert_called_once()

    def test_setup_error_is_saved_as_failure(self):
        self.repro.scenario = Mock(side_effect=RuntimeError("proxy failed to start"))
        self.assertEqual(self.repro.execute(), 1)
        summary = json.loads((self.repro.out / "summary.json").read_text())
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["error"], "proxy failed to start")

    def test_packet_analysis_failure_preserves_fixture_counts(self):
        self.repro.scenario = Mock(return_value={"outbound": "127.0.0.1:4140", "backend": "127.0.0.1:8080"})
        with patch("standalone.CASES", [("refused", 10, 500)]), \
                patch("standalone.VARIANTS", ["before"]), \
                patch("standalone.verify_case", side_effect=lambda run, row: row.update(duplicateRequests=10)), \
                patch("standalone.analyze", side_effect=ValueError("incomplete capture")):
            self.assertEqual(self.repro.execute(), 1)
        summary = json.loads((self.repro.out / "summary.json").read_text())
        self.assertEqual(summary["cases"][0]["duplicateRequests"], 10)
        self.assertEqual(summary["cases"][0]["error"], "incomplete capture")
        self.assertEqual(summary["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
