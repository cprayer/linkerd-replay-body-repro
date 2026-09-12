#!/usr/bin/env python3
"""Run the HTTP/2 reproduction using local processes inside the image."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from reproduce import CASES, PAYLOAD, VARIANTS, Run, metric, require, verify_case, write_json
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
        self.echo_codes = {}
        self.results = []
        self.report = {"run": ident, "status": "RUNNING", "mode": "standalone",
                       "upstreamRevision": "e5de317dfe0feb8f6ff06f07c4e8ec9f61ab7a8f",
                       "payload": PAYLOAD, "cases": self.results}

    def echo_clients(self, label, addr, count):
        commands = [["echo-h2", "-mode", "client", "-addr", addr, "-id", label + "-%03d" % index]
                    for index in range(count)]
        with (self.out / "commands.jsonl").open("a") as log:
            for command in commands:
                log.write(json.dumps({"argv": command, "log": label + ".log"}) + "\n")

        def invoke(command):
            try:
                return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, timeout=30)
            except subprocess.TimeoutExpired as error:
                partial = error.stdout or b""
                return subprocess.CompletedProcess(command, 124, partial.decode(errors="replace") + "\nClient timed out\n")

        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(invoke, commands))
        self.echo_codes[label] = {command[-1]: result.returncode for command, result in zip(commands, results)}
        output = "".join(result.stdout for result in results)
        (self.out / (label + ".log")).write_text(output)
        return subprocess.CompletedProcess(commands, int(any(result.returncode for result in results)), output)

    def scenario(self, case, count, delay, variant):
        label = case + "-" + variant
        server_name = ("healthy" if case == "failfast" else case) + "-" + variant
        backend = free_port()
        server_args = ["audit-h2", "-mode", "server", "-listen", "127.0.0.1:" + str(backend),
                       "-failure", {"refused": "refused", "consumed503": "http503",
                                    "early503": "http503"}.get(case, "none")]
        if case == "consumed503":
            server_args.append("-consume-first")
        if case in ("healthy", "failfast"):
            server_args = ["echo-h2", "-mode", "server", "-addr", "127.0.0.1:" + str(backend)]
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
                print("Running " + label, flush=True)
                if case in ("healthy", "failfast"):
                    result = self.echo_clients(label, listeners["outbound"], count)
                else:
                    client = ["audit-h2", "-mode", "client", "-addr", listeners["outbound"],
                              "-authority", label + ":8080", "-id", label, "-payload", PAYLOAD,
                              "-delay-ms", str(delay), "-concurrency", str(count)]
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
                        if case in ("healthy", "failfast"):
                            verify_echo_case(self, row)
                        else:
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


def verify_echo_case(run, row):
    case, variant, count = row["scenario"], row["variant"], row["requests"]
    label = case + "-" + variant
    responses = re.findall(r'^ECHO (\S+) SENT "ping" \(4 bytes\) RECEIVED "(ping|pingping)" \((4|8) bytes\) HTTP 200$',
                           (run.out / (label + ".log")).read_text(), re.MULTILINE)
    expected_ids = {label + "-%03d" % index for index in range(count)}
    require(len(responses) == count and {r[0] for r in responses} == expected_ids,
            label + ": missing, repeated, or unexpected client responses")
    server = re.findall(r'^SERVER (\S+) RECEIVED "(ping|pingping)" \((4|8) bytes\)$',
                        (run.out / ("healthy-" + variant + "-server.log")).read_text(), re.MULTILINE)
    server = [r for r in server if r[0].startswith(label + "-")]
    require(sorted(server) == sorted(responses), label + ": server body disagrees with client response")
    duplicates = [ident for ident, body, _ in responses if body == PAYLOAD * 2]
    for ident, body, size in responses:
        require(int(size) == len(body) and run.echo_codes[label][ident] == int(body != PAYLOAD),
                label + ": byte count or client exit code disagrees with echo")
    require(run.codes[label] == int(bool(duplicates)), label + ": unexpected client exit code")
    metrics = run.out / ("client-" + variant + ".prom")
    retries = metric(metrics, "outbound_http_route_retry_requests_total", label)
    successes = metric(metrics, "outbound_http_route_retry_successes_total", label)
    require(retries == successes, label + ": proxy retry failed")
    row.update(payloadMatches=count - len(duplicates), duplicateRequests=len(duplicates), duplicateIds=duplicates,
               responseStatuses={"200": count}, proxyRetriedRequests=retries, proxyRetrySuccesses=successes,
               clientExitCodes=run.echo_codes[label])
    if case == "failfast":
        failures = len(re.findall(r"retryable=true error=.*failfast-" + variant + r"-empty.*service in fail-fast",
                                 (run.out / ("client-" + variant + "-proxy.log")).read_text()))
        row["failFastRetryLogs"] = failures
        require(0 <= retries <= count and failures == retries, label + ": FailFast logs disagree with retries")
        if variant == "before":
            require(len(duplicates) == retries, label + ": FailFast retries did not duplicate exactly once")
        example = next(r for r in responses if r[0] == label + "-000")
        row["echo"] = {"sent": PAYLOAD, "received": example[1], "id": example[0], "log": label + ".log", "delayMs": 0}
    else:
        require(retries == 0, label + ": healthy backend unexpectedly retried")
    if variant == "after" or case == "healthy":
        require(not duplicates, label + ": body was duplicated")


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
