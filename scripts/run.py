#!/usr/bin/env python3
"""Build and prepare once, then repeat the reproduction with persistent logs."""

import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parent.parent
LINKERD_VERSION = "edge-26.8.2"
STATUSES = {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


class Batch:
    def __init__(self, args):
        self.args = args
        ident = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.out = args.artifacts.resolve() / ident
        self.out.mkdir(parents=True)
        self.env = dict(os.environ, PYTHONUNBUFFERED="1", CLUSTER_NAME=args.cluster,
                        BUILD_ARTIFACTS=str(self.out / "build"))
        proxy = self.env.get("PROXY_IMAGE", "linkerd-replay-proxy")
        self.env.setdefault("BEFORE_IMAGE", proxy + ":before")
        self.env.setdefault("AFTER_IMAGE", proxy + ":after")
        self.report = {"batch": ident, "cluster": args.cluster, "requestedRuns": args.runs,
                       "inconclusiveRetries": args.retries, "skipBuild": args.skip_build,
                       "status": "RUNNING", "runs": []}

    def save(self):
        target = self.out / "summary.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(target)
        self.write_report()

    def write_report(self):
        passed = sum(row["status"] == "PASS" for row in self.report["runs"])
        lines = ["# %s — %d/%d runs passed" % (self.report["status"], passed, self.args.runs), "",
                 "PASS = bug reproduced before the fix, all checks passed after the fix.", "",
                 "| Run | Before: duplicated requests | After: duplicated requests | Result | Logs |",
                 "|---|---|---|---|---|"]
        extra_attempts = 0
        for row in self.report["runs"]:
            duplicates = {"before": "?", "after": "?"}
            log = ""
            if row["attempts"]:
                attempt = row["attempts"][-1]
                duplicates.update(attempt.get("duplicates", {}))
                extra_attempts += len(row["attempts"]) - 1
                log = "[Open](%s)" % attempt["log"]
            lines.append("| %d | %s | %s | %s | %s |" % (
                row["run"], duplicates["before"], duplicates["after"], row["status"], log))
        if extra_attempts:
            lines.extend(["", "%d extra attempt(s) after INCONCLUSIVE; all attempts are saved in the run logs." % extra_attempts])
        if self.report.get("error"):
            lines.extend(["", "Error: " + self.report["error"]])
        lines.extend(["", "Counts cover all scenarios in the final attempt. '?' means data is incomplete.", "",
                      "[Full log](runner.log) · [Raw results](summary.json)"])
        (self.out / "report.md").write_text("\n".join(lines) + "\n")

    def emit(self, message):
        print(message, flush=True)
        with (self.out / "runner.log").open("a") as log:
            log.write(message + "\n")

    def command(self, argv, relative_log, check=True):
        path = self.out / relative_log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as log, (self.out / "runner.log").open("a") as combined:
            combined.write("$ " + shlex.join(str(value) for value in argv) + "\n")
            combined.flush()
            process = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors="replace",
                                       start_new_session=True)
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    combined.write(line)
                    combined.flush()
                code = process.wait()
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                raise
            finally:
                process.stdout.close()
        if check and code:
            raise RuntimeError("Command exited %d; see %s" % (code, path))
        return code

    def prepare(self):
        self.emit("Checking prerequisites...")
        for tool in ("bash", "docker", "kind", "kubectl"):
            if not shutil.which(tool):
                raise RuntimeError("Missing prerequisite: " + tool)
        self.command(["docker", "info"], "setup/docker.log")
        cache = ROOT / ".cache" / "linkerd2"
        candidates = [self.env.get("LINKERD_BIN", "linkerd"),
                      str(cache / "bin" / "linkerd"),
                      str(Path.home() / ".linkerd2" / "bin" / "linkerd")]
        if "LINKERD_BIN" in self.env:
            candidates = [self.env["LINKERD_BIN"]]
        for candidate in candidates:
            executable = shutil.which(candidate)
            if not executable:
                continue
            code = self.command([executable, "version", "--client", "--short"], "setup/linkerd-version.log", check=False)
            if code == 0 and (self.out / "setup/linkerd-version.log").read_text().strip() == LINKERD_VERSION:
                self.env["LINKERD_BIN"] = executable
                break
        else:
            if "LINKERD_BIN" in self.env:
                raise RuntimeError("LINKERD_BIN must point to Linkerd " + LINKERD_VERSION)
            if not shutil.which("curl"):
                raise RuntimeError("curl is required to install Linkerd " + LINKERD_VERSION)
            installer = self.out / "setup/install-linkerd.sh"
            self.emit("Installing Linkerd " + LINKERD_VERSION + "...")
            self.command(["curl", "--proto", "=https", "--tlsv1.2", "-sSfL",
                          "https://run.linkerd.io/install-edge", "-o", str(installer)], "setup/download-linkerd.log")
            self.env.update(INSTALLROOT=str(cache), LINKERD2_VERSION=LINKERD_VERSION)
            self.command(["sh", str(installer)], "setup/install-linkerd.log")
            self.env["LINKERD_BIN"] = str(cache / "bin" / "linkerd")
        if not self.args.skip_build:
            self.emit("Building proxy images (the first build may take several minutes)...")
            self.command(["bash", "scripts/build-proxies.sh"], "setup/build-proxies.log")
            self.emit("Building fixture image...")
            self.command(["bash", "fixture/build.sh"], "setup/build-fixture.log")
        self.emit("Preparing cluster " + self.args.cluster + "...")
        self.command(["bash", "scripts/cluster.sh", "up"], "setup/cluster.log")

    def repeat(self):
        for number in range(1, self.args.runs + 1):
            row = {"run": number, "status": "RUNNING", "attempts": []}
            self.report["runs"].append(row)
            self.save()
            for attempt in range(1, self.args.retries + 2):
                folder = Path("run-%03d" % number) / ("attempt-%02d" % attempt)
                self.emit("Run %d/%d, attempt %d/%d" % (number, self.args.runs, attempt, self.args.retries + 1))
                code = self.command([sys.executable, "scripts/reproduce.py", "--cluster", self.args.cluster,
                                     "--artifacts", str(self.out / folder)], folder / "console.log", check=False)
                result = {"attempt": attempt, "exitCode": code, "status": STATUSES.get(code, "FAIL"),
                          "log": str(folder / "console.log")}
                summaries = list((self.out / folder).glob("*/summary.json"))
                try:
                    if len(summaries) != 1:
                        raise ValueError("Expected one reproduction summary")
                    summary = json.loads(summaries[0].read_text())
                    result["summary"] = str(summaries[0].relative_to(self.out))
                    if code not in STATUSES or summary["status"] != STATUSES[code]:
                        raise ValueError("Summary status disagrees with exit code")
                    result["duplicates"] = {}
                    for variant in ("before", "after"):
                        cases = [case for case in summary.get("cases", []) if case["variant"] == variant]
                        if len(cases) == 5 and all("duplicateRequests" in case for case in cases):
                            result["duplicates"][variant] = sum(case["duplicateRequests"] for case in cases)
                except (ValueError, KeyError, OSError, TypeError) as error:
                    result.update(status="FAIL", error=str(error))
                row["attempts"].append(result)
                row["status"] = result["status"]
                self.save()
                self.emit("Run %d attempt %d: %s" % (number, attempt, result["status"]))
                if result["status"] != "INCONCLUSIVE":
                    break
        counts = collections.Counter(row["status"] for row in self.report["runs"])
        self.report["counts"] = {status: counts[status] for status in STATUSES.values()}
        self.report["attemptCounts"] = dict(collections.Counter(
            attempt["status"] for row in self.report["runs"] for attempt in row["attempts"]))
        return 1 if counts["FAIL"] else 2 if counts["INCONCLUSIVE"] else 0

    def execute(self):
        self.emit("Batch artifacts: " + str(self.out))
        self.save()
        code = 1
        try:
            self.prepare()
            code = self.repeat()
        except (Exception, KeyboardInterrupt) as error:
            self.report["error"] = str(error) or "Interrupted"
            self.emit("FAIL: " + self.report["error"])
        finally:
            self.report["status"] = STATUSES[code]
            self.save()
        self.emit("\n" + (self.out / "report.md").read_text())
        self.emit("Report: " + str(self.out / "report.md"))
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=positive, default=1, help="Number of runs (default: 1)")
    parser.add_argument("--retries", type=nonnegative, default=2,
                        help="Extra attempts per INCONCLUSIVE run (default: 2); FAIL is never retried")
    parser.add_argument("--skip-build", action="store_true", help="Reuse existing proxy and fixture images")
    parser.add_argument("--cluster", default=os.environ.get("CLUSTER_NAME", "linkerd-replay-repro"))
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".artifacts" / "batches")
    return Batch(parser.parse_args()).execute()


if __name__ == "__main__":
    sys.exit(main())
