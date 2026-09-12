from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from standalone import LocalRun, verify_echo_case


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
        self.repro.command = Mock(return_value=Mock(returncode=0, stdout=""))
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

    def test_command_sends_stderr_to_console_without_changing_stdout(self):
        console = io.StringIO()
        with redirect_stderr(console):
            result = self.repro.command([sys.executable, "-c",
                                         'import sys; print("{}"); print("diagnostic", file=sys.stderr)'], "output.json")
        self.assertEqual(json.loads(result.stdout), {})
        self.assertEqual(json.loads((self.repro.out / "output.json").read_text()), {})
        self.assertIn("diagnostic", console.getvalue())
        self.assertFalse(list(self.repro.out.glob("*.stderr")))

    def test_command_timeout_keeps_partial_output_and_console_error(self):
        console = io.StringIO()
        error = subprocess.TimeoutExpired(["client"], 1, output=b"partial output", stderr=b"timeout diagnostic")
        with redirect_stderr(console), patch("reproduce.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "Command timed out"):
                self.repro.command(["client"], "timeout.log", timeout=1)
        self.assertEqual((self.repro.out / "timeout.log").read_text(), "partial output")
        self.assertIn("timeout diagnostic", console.getvalue())
        self.assertFalse(list(self.repro.out.glob("*.stderr")))

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

    def test_simple_echo_requires_matching_unique_client_and_server_logs(self):
        label, ident = "failfast-before", "failfast-before-000"
        client = self.repro.out / (label + ".log")
        server = self.repro.out / "healthy-before-server.log"
        client.write_text('ECHO %s SENT "ping" (4 bytes) RECEIVED "pingping" (8 bytes) HTTP 200\n' % ident)
        server_line = 'SERVER %s RECEIVED "pingping" (8 bytes)\n' % ident
        server.write_text(server_line)
        (self.repro.out / "client-before.prom").write_text(
            '\n'.join('%s{route_name="%s"} 1' % (name, label) for name in (
                "outbound_http_route_retry_requests_total", "outbound_http_route_retry_successes_total")))
        (self.repro.out / "client-before-proxy.log").write_text(
            "retryable=true error=backend failfast-before-empty: service in fail-fast\n")
        self.repro.codes[label] = 1
        self.repro.echo_codes[label] = {ident: 1}
        row = {"scenario": "failfast", "variant": "before", "requests": 1}
        verify_echo_case(self.repro, row)
        self.assertEqual(row["duplicateRequests"], 1)
        self.assertEqual(row["echo"]["received"], "pingping")
        for invalid in ("", server_line * 2, server_line.replace('"pingping"', '"ping"')):
            server.write_text(invalid)
            with self.assertRaisesRegex(RuntimeError, "server body disagrees"):
                verify_echo_case(self.repro, row)
        server.write_text(server_line)
        self.repro.echo_codes[label][ident] = 0
        with self.assertRaisesRegex(RuntimeError, "exit code disagrees"):
            verify_echo_case(self.repro, row)

    def test_simple_echo_timeout_preserves_partial_output(self):
        error = subprocess.TimeoutExpired(["echo-h2"], 30, output=b"partial echo")
        with patch("standalone.subprocess.run", side_effect=error):
            result = self.repro.echo_clients("healthy-before", "127.0.0.1:8080", 1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.repro.echo_codes["healthy-before"], {"healthy-before-000": 124})
        self.assertIn("partial echo", (self.repro.out / "healthy-before.log").read_text())
        self.assertIn("Client timed out", result.stdout)


if __name__ == "__main__":
    unittest.main()
