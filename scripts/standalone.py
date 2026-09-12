#!/usr/bin/env python3
"""Run the HTTP/2 reproduction using local processes inside the image."""

import argparse
import datetime
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from reproduce import CASES, PAYLOAD, VARIANTS, Run, require, verify_case, write_json
from run import Batch, positive, nonnegative
from packets import analyze, capture


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until(check, process, description):
    deadline = time.monotonic() + 30
    while True:
        require(process.poll() is None, description + " exited before becoming ready")
        if check():
            return
        require(time.monotonic() < deadline, description + " did not become ready")
        time.sleep(0.1)


def can_connect(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class LocalRun:
    command = Run.command

    def __init__(self, artifacts):
        ident = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.out = artifacts / ident
        self.out.mkdir(parents=True)
        self.codes = {}
        self.results = []
        self.report = {"run": ident, "status": "RUNNING", "mode": "standalone",
                       "upstreamRevision": "e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f",
                       "payload": PAYLOAD, "cases": self.results}

    def scenario(self, case, count, delay, variant):
        label = case + "-" + variant
        server_name = ("healthy" if case == "failfast" else case) + "-" + variant
        backend = free_port()
        server_args = ["audit-h2", "-mode", "server", "-listen", "127.0.0.1:" + str(backend),
                       "-failure", {"refused": "refused", "consumed503": "http503",
                                    "early503": "http503"}.get(case, "none")]
        if case == "consumed503":
            server_args.append("-consume-first")
        ready = self.out / (label + "-listeners.json")
        processes = []
        with (self.out / (server_name + "-server.log")).open("a") as server_log, \
                (self.out / ("client-" + variant + "-proxy.log")).open("a") as proxy_log, \
                capture(self.out / (label + ".pcap")):
            try:
                server = subprocess.Popen(server_args, stdout=server_log, stderr=subprocess.STDOUT)
                processes.append(server)
                wait_until(lambda: can_connect(backend), server, label + " server")
                proxy = subprocess.Popen(["replay-" + variant, label, "127.0.0.1:" + str(backend), str(ready)],
                                         stdout=proxy_log, stderr=subprocess.STDOUT)
                processes.append(proxy)
                wait_until(ready.is_file, proxy, label + " proxy")
                listeners = json.loads(ready.read_text())
                listeners["backend"] = "127.0.0.1:" + str(backend)
                write_json(ready, listeners)
                client = ["audit-h2", "-mode", "client", "-addr", listeners["outbound"],
                          "-authority", label + ":8080", "-id", label, "-payload", PAYLOAD,
                          "-delay-ms", str(delay), "-concurrency", str(count)]
                if case == "failfast":
                    client.append("-warmup=false")
                print("Running " + label, flush=True)
                result = self.command(client, label + ".log", check=False, timeout=60)
                self.codes[label] = result.returncode
                for line in result.stdout.splitlines():
                    if line.startswith("ECHO " + label + "-000 "):
                        print(line, flush=True)
                with urllib.request.urlopen("http://" + listeners["admin"] + "/metrics", timeout=10) as response:
                    metrics = response.read().decode()
                (self.out / (label + ".prom")).write_text(metrics)
                with (self.out / ("client-" + variant + ".prom")).open("a") as output:
                    output.write("\n".join(line for line in metrics.splitlines()
                                           if ('route_name="' + label + '"') in line) + "\n")
                require(all(process.poll() is None for process in processes), label + ": process exited unexpectedly")
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        return listeners

    def execute(self):
        code = 1
        try:
            for case, count, delay in CASES:
                for variant in VARIANTS:
                    listeners = self.scenario(case, count, delay, variant)
                    row = {"scenario": case, "variant": variant, "requests": count, "delayMs": delay,
                           "packetReport": case + "-" + variant + "-packets.md"}
                    self.results.append(row)
                    try:
                        verify_case(self, row)
                        packet_duplicates = analyze(self.out / (case + "-" + variant + ".pcap"), listeners, count)
                        row["packetDuplicateRequests"] = packet_duplicates
                        require(packet_duplicates == row["duplicateRequests"],
                                "Packet evidence disagrees with fixture logs; see " + row["packetReport"])
                        if case == "failfast" and (row["proxyRetriedRequests"] == 0 or
                                                   (variant == "before" and row["duplicateRequests"] == 0)):
                            row.update(status="INCONCLUSIVE", error="No unread-body retry was observed")
                        else:
                            row["status"] = "PASS"
                    except Exception as error:
                        row.update(status="FAIL", error=str(error))
                    print(json.dumps(row), flush=True)
            statuses = [row["status"] for row in self.results]
            code = 1 if "FAIL" in statuses else 2 if "INCONCLUSIVE" in statuses else 0
            self.report["status"] = {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}[code]
        except (Exception, KeyboardInterrupt) as error:
            self.report.update(status="FAIL", error=str(error) or "Interrupted")
            print(self.report["error"], file=sys.stderr)
        finally:
            captures = sorted(self.out.glob("*.pcap"))
            if captures:
                lines = ["# Packet evidence", "", "PCAP files contain loopback traffic from this isolated reproduction. "
                         "Counts below use captured request headers and DATA, independently of fixture logs.", "",
                         "Open a count to compare DATA and find packet numbers, payloads, and Wireshark filters. "
                         "'?' means analysis is incomplete.", "",
                         "| Scenario | Before: duplicated requests | After: duplicated requests | PCAP |",
                         "|---|---|---|---|"]
                rows = {(row["scenario"], row["variant"]): row for row in self.results}
                for case, _, _ in CASES:
                    cells, links = [], []
                    for variant in VARIANTS:
                        label = case + "-" + variant
                        row = rows.get((case, variant), {})
                        cells.append("[%s](%s)" % (row["packetDuplicateRequests"], row["packetReport"])
                                     if "packetDuplicateRequests" in row else "?")
                        if (self.out / (label + ".pcap")).exists():
                            links.append("[%s](%s.pcap)" % (variant, label))
                    lines.append("| %s | %s | %s | %s |" % (case, *cells, " · ".join(links)))
                (self.out / "packets.md").write_text("\n".join(lines) + "\n")
                self.report["packetReport"] = "packets.md"
            self.report["clientExitCodes"] = self.codes
            write_json(self.out / "summary.json", self.report)
        return code


class LocalBatch(Batch):
    def prepare(self):
        self.emit("Running prebuilt proxies and HTTP/2 fixtures...")

    def reproduction_command(self, artifacts):
        return [sys.executable, "scripts/standalone.py", "--once", str(artifacts)]


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=positive, default=5)
    parser.add_argument("--retries", type=nonnegative, default=2)
    parser.add_argument("--artifacts", type=Path, default=Path("/results"))
    parser.add_argument("--once", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    if args.once:
        return LocalRun(args.once).execute()
    args.cluster, args.skip_build = "standalone", True
    batch = LocalBatch(args)
    return batch.execute()


if __name__ == "__main__":
    sys.exit(main())
