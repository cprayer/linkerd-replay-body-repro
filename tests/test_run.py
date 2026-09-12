import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts/run.py"


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "fixture").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "scripts/run.py").write_text(RUNNER.read_text())
        self.trace = self.root / "trace.log"
        self.env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        TEST_TRACE=str(self.trace), TEST_ROOT=str(self.root),
                        LINKERD_BIN=str(self.root / "bin/linkerd"))
        for name in ("docker", "kind", "kubectl", "linkerd"):
            path = self.root / "bin" / name
            path.write_text('#!/bin/sh\nif [ "${0##*/}" = linkerd ]; then echo edge-26.8.2; fi\n')
            path.chmod(0o755)
        for relative in ("scripts/build-proxies.sh", "fixture/build.sh", "scripts/cluster.sh"):
            (self.root / relative).write_text(
                'echo "' + relative + '" >> "$TEST_TRACE"\n'
                'echo "preparing ' + relative + '"\n'
                'if [ "${TEST_SETUP_FAIL:-}" = "' + relative + '" ]; then exit 7; fi\n')
        (self.root / "scripts/reproduce.py").write_text('''import argparse
import json
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--cluster")
parser.add_argument("--artifacts", type=Path)
args = parser.parse_args()
state = Path(os.environ["TEST_ROOT"]) / "state"
index = int(state.read_text()) if state.exists() else 0
state.write_text(str(index + 1))
with open(os.environ["TEST_TRACE"], "a") as trace:
    trace.write("reproduce " + args.cluster + " " + os.environ["BEFORE_IMAGE"] + "\\n")
code = json.loads(os.environ["TEST_CODES"])[index]
print("fixture stdout attempt", index + 1)
print("fixture stderr attempt", index + 1, file=sys.stderr)
if not os.environ.get("TEST_NO_SUMMARY"):
    out = args.artifacts / "fixture-run"
    out.mkdir(parents=True)
    (out / "server.log").write_text("server diagnostic")
    for variant in ("before", "after"):
        for scenario in ("refused", "failfast"):
            (out / (scenario + "-" + variant + ".log")).write_text("actual echo response")
    status = os.environ.get("TEST_SUMMARY_STATUS") or {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}.get(code, "FAIL")
    (out / "summary.json").write_text(json.dumps({"status": status, "cases": [
        {"scenario": scenario, "variant": variant, "status": status,
         "duplicateRequests": (10 if scenario == "refused" else 2 if scenario == "failfast" else 0) if variant == "before" else 0,
         **({"echo": {"sent": "ping", "received": os.environ.get("TEST_ECHO", "pingping") if variant == "before" else "ping",
                      "id": scenario + "-" + variant + "-000", "log": scenario + "-" + variant + ".log",
                      "delayMs": 500 if scenario == "refused" else 0}} if scenario in ("refused", "failfast") else {})}
        for variant in ("before", "after")
        for scenario in ("refused", "consumed503", "early503", "healthy", "failfast")]}))
sys.exit(code)
''')

    def run_batch(self, codes, *arguments, **environment):
        result = subprocess.run([sys.executable, str(self.root / "scripts/run.py"), *arguments],
                                env=dict(self.env, TEST_CODES=json.dumps(codes), **environment),
                                capture_output=True, text=True, timeout=30)
        summaries = list((self.root / ".artifacts/batches").glob("*/summary.json"))
        self.assertEqual(len(summaries), 1, result.stdout + result.stderr)
        self.out = summaries[0].parent
        return result, json.loads(summaries[0].read_text())

    def test_prepares_once_and_keeps_each_run_and_both_output_streams(self):
        result, report = self.run_batch([0, 0, 0], "--runs", "3", "--cluster", "isolated",
                                       PROXY_IMAGE="custom-proxy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text().splitlines()
        self.assertEqual(trace[:3], ["scripts/build-proxies.sh", "fixture/build.sh", "scripts/cluster.sh"])
        self.assertEqual(trace[3:], ["reproduce isolated custom-proxy:before"] * 3)
        self.assertEqual(report["counts"], {"PASS": 3, "FAIL": 0, "INCONCLUSIVE": 0})
        for row in report["runs"]:
            attempt = row["attempts"][0]
            self.assertTrue((self.out / attempt["summary"]).is_file())
            log = (self.out / attempt["log"]).read_text()
            self.assertIn("fixture stdout", log)
            self.assertIn("fixture stderr", log)
        self.assertIn("fixture stdout attempt 3", (self.out / "runner.log").read_text())
        report_md = (self.out / "report.md").read_text()
        self.assertIn("| 3 | 12 | 0 | PASS | [Open](run-003/attempt-01/console.log) |", report_md)
        self.assertIn("# PASS — 3/3 runs passed", report_md)
        self.assertIn("| 3 | 12 | 0 | PASS |", result.stdout)
        self.assertNotIn("fixture stdout", result.stdout)
        self.assertFalse((self.out / "errors.log").exists())

    def test_inconclusive_retry_preserves_original_result(self):
        result, report = self.run_batch([2, 0])
        self.assertEqual(result.returncode, 0)
        self.assertEqual([a["status"] for a in report["runs"][0]["attempts"]], ["INCONCLUSIVE", "PASS"])
        self.assertEqual(report["attemptCounts"], {"INCONCLUSIVE": 1, "PASS": 1})

    def test_report_displays_observed_echo_and_links_raw_response(self):
        result, report = self.run_batch([0], TEST_ECHO="unexpected response")
        self.assertEqual(result.returncode, 0)
        echo = report["runs"][0]["attempts"][0]["echo"]["refused"]["before"]
        self.assertEqual(echo["received"], "unexpected response")
        immediate = report["runs"][0]["attempts"][0]["echo"]["failfast"]["before"]
        self.assertEqual(immediate["received"], "unexpected response")
        self.assertEqual(immediate["delayMs"], 0)
        self.assertIn("| failfast | 0 ms |", (self.out / "report.md").read_text())
        self.assertTrue((self.out / echo["log"]).is_file())
        self.assertIn('["unexpected response"](' + echo["log"] + ')', (self.out / "report.md").read_text())

    def test_exhausted_retries_exit_two(self):
        result, report = self.run_batch([2, 2, 2])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(len(report["runs"][0]["attempts"]), 3)
        self.assertFalse((self.out / "errors.log").exists())

    def test_failure_is_not_retried_or_hidden_by_later_pass(self):
        result, report = self.run_batch([1, 0], "--runs", "2")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([row["status"] for row in report["runs"]], ["FAIL", "PASS"])
        self.assertEqual([len(row["attempts"]) for row in report["runs"]], [1, 1])
        errors = list(self.out.rglob("errors.log"))
        self.assertEqual(errors, [self.out / "errors.log"])
        self.assertIn("fixture stdout attempt 1", errors[0].read_text())
        self.assertIn("fixture stderr attempt 1", errors[0].read_text())
        self.assertIn("server diagnostic", errors[0].read_text())
        self.assertIn("[Error details](errors.log)", (self.out / "report.md").read_text())

    def test_failure_takes_priority_over_inconclusive(self):
        result, report = self.run_batch([2, 1], "--runs", "2", "--retries", "0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "FAIL")

    def test_skip_build_still_checks_cluster(self):
        result, report = self.run_batch([0], "--skip-build")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.trace.read_text().splitlines()[0], "scripts/cluster.sh")
        self.assertEqual(len(self.trace.read_text().splitlines()), 2)
        self.assertTrue(report["skipBuild"])

    def test_setup_failure_writes_report_and_stops(self):
        result, report = self.run_batch([], TEST_SETUP_FAIL="scripts/build-proxies.sh")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["runs"], [])
        self.assertIn("Command exited 7", report["error"])
        self.assertEqual(self.trace.read_text().splitlines(), ["scripts/build-proxies.sh"])
        self.assertIn("Command exited 7", (self.out / "errors.log").read_text())

    def test_missing_summary_does_not_pass(self):
        result, report = self.run_batch([0], TEST_NO_SUMMARY="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Expected one", report["runs"][0]["attempts"][0]["error"])
        self.assertIn("Expected one", (self.out / "errors.log").read_text())

    def test_inconsistent_summary_does_not_pass(self):
        result, report = self.run_batch([0], TEST_SUMMARY_STATUS="FAIL")
        self.assertEqual(result.returncode, 1)
        self.assertIn("disagrees", report["runs"][0]["attempts"][0]["error"])

    def test_unexpected_exit_does_not_retry(self):
        result, report = self.run_batch([9])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["runs"][0]["attempts"][0]["exitCode"], 9)
        self.assertEqual(len(report["runs"][0]["attempts"]), 1)

    def test_invalid_counts_fail_before_preparation(self):
        for arguments in (("--runs", "0"), ("--runs", "-1"), ("--retries", "-1")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(RUNNER), *arguments], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
