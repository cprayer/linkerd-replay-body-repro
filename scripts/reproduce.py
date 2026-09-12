#!/usr/bin/env python3
"""Run the ReplayBody reproducer in an existing, dedicated kind cluster."""

import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = "ping"
CASES = [("refused", 10, 500), ("consumed503", 10, 500),
         ("early503", 10, 500), ("healthy", 10, 0), ("failfast", 20, 0)]
VARIANTS = ("before", "after")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def events(path):
    result = []
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "event" in value:
            result.append(value)
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def timestamp(value):
    # Python 3.9 accepts microseconds; the Go fixture emits nanoseconds.
    return datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00"))


def image_parts(image):
    require("@" not in image and ":" in image.rsplit("/", 1)[-1],
            "Proxy images must have an explicit tag: " + image)
    return image.rsplit(":", 1)


class Run:
    def __init__(self, args):
        self.args = args
        self.ident = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.namespace = "replay-body-repro-" + self.ident
        self.context = "kind-" + args.cluster
        self.out = args.artifacts / self.ident
        self.out.mkdir(parents=True)
        self.k = ["kubectl", "--context", self.context, "-n", self.namespace]
        self.created = False
        self.results = []
        self.codes = {}
        self.report = {"run": self.ident, "namespace": self.namespace, "context": self.context,
                       "fixtureImage": args.fixture_image,
                       "proxyImages": {"before": args.before_image, "after": args.after_image},
                       "payload": PAYLOAD, "cases": self.results, "status": "RUNNING"}

    def command(self, argv, log, check=True, timeout=90):
        with (self.out / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                     "argv": argv, "log": log}) + "\n")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b""
            stderr = error.stderr or b""
            (self.out / log).write_text(output.decode(errors="replace") if isinstance(output, bytes) else output)
            if stderr:
                print(stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr, end="", file=sys.stderr)
            raise RuntimeError("Command timed out; see " + str(self.out / log)) from error
        (self.out / log).write_text(result.stdout)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if check:
            require(result.returncode == 0, "Command failed (%d); see %s" % (result.returncode, self.out / log))
        return result

    def prepare(self):
        for tool in ("docker", "kind", "kubectl", self.args.linkerd_bin):
            require(shutil.which(tool) is not None, "Missing prerequisite: " + tool)
        clusters = self.command(["kind", "get", "clusters"], "clusters.log").stdout.splitlines()
        require(self.args.cluster in clusters, "Cluster is missing; run scripts/cluster.sh up first")
        images = [self.args.fixture_image, self.args.before_image, self.args.after_image]
        for value in images[1:]:
            image_parts(value)
        self.command(["docker", "image", "inspect"] + images, "images.json")
        self.command(["kind", "load", "docker-image", "--name", self.args.cluster] + images,
                     "load-images.log", timeout=300)
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {
            "name": self.namespace, "labels": {"app.kubernetes.io/managed-by": "linkerd-replay-body-repro"},
            "annotations": {"linkerd.io/inject": "enabled"}}}
        write_json(self.out / "namespace.json", namespace)
        self.command(self.k + ["create", "-f", str(self.out / "namespace.json")], "create-namespace.log")
        self.created = True
        write_json(self.out / "manifest.json", {"apiVersion": "v1", "kind": "List", "items": self.manifests()})
        self.command(self.k + ["apply", "-f", str(self.out / "manifest.json")], "apply.log")
        self.command(self.k + ["wait", "--for=condition=Available", "deployment", "--all", "--timeout=180s"],
                     "wait-deployments.log", timeout=190)
        self.command(self.k + ["wait", "--for=condition=Ready", "pod", "--all", "--timeout=180s"],
                     "wait-pods.log", timeout=190)
        deadline = time.monotonic() + 120
        while True:
            routes = json.loads(self.command(self.k + ["get", "httproutes", "-o", "json"], "routes-ready.json").stdout)["items"]
            if len(routes) == len(CASES) * len(VARIANTS) and all(route_ready(route) for route in routes):
                break
            require(time.monotonic() < deadline, "HTTPRoutes were not accepted/resolved; see routes-ready.json")
            time.sleep(2)

    def manifests(self):
        def deployment(name, arguments, annotations, client=False):
            container = {"name": "audit", "image": self.args.fixture_image, "imagePullPolicy": "Never"}
            if client:
                container["command"] = ["/bin/sleep", "3600"]
            else:
                container["args"] = arguments
                container["readinessProbe"] = {"tcpSocket": {"port": 8080}, "periodSeconds": 1}
            return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": self.namespace},
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": {
                        "metadata": {"labels": {"app": name}, "annotations": annotations},
                        "spec": {"containers": [container]}}}}

        def service(name, selector):
            return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": self.namespace},
                    "spec": {"selector": {"app": selector}, "ports": [{"name": "http", "port": 8080,
                        "targetPort": 8080, "appProtocol": "kubernetes.io/h2c"}]}}

        def route(name, backends=None):
            return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
                    "metadata": {"name": name, "namespace": self.namespace, "annotations": {
                        "retry.linkerd.io/http": "503", "retry.linkerd.io/limit": "1", "retry.linkerd.io/timeout": "30s"}},
                    "spec": {"parentRefs": [{"group": "core", "kind": "Service", "name": name, "port": 8080}],
                             "rules": [{"backendRefs": backends} if backends else {}]}}

        items = []
        for variant, image in (("before", self.args.before_image), ("after", self.args.after_image)):
            repository, tag = image_parts(image)
            items.append(deployment("client-" + variant, [], {
                "config.linkerd.io/proxy-image": repository, "config.linkerd.io/proxy-version": tag,
                "config.linkerd.io/image-pull-policy": "Never",
                "config.linkerd.io/proxy-log-level": "linkerd=debug,warn"}, client=True))
            for case, failure in (("refused", "refused"), ("consumed503", "http503"),
                                  ("early503", "http503"), ("healthy", "none")):
                name = case + "-" + variant
                arguments = ["-mode", "server", "-listen", ":8080", "-failure", failure]
                if case == "consumed503":
                    arguments.append("-consume-first")
                items.extend([deployment(name, arguments, {"linkerd.io/inject": "disabled"}), service(name, name), route(name)])
            name = "failfast-" + variant
            empty = name + "-empty"
            healthy = "healthy-" + variant
            items.extend([service(name, healthy), service(empty, empty), route(name, [
                {"name": empty, "port": 8080, "weight": 100}, {"name": healthy, "port": 8080, "weight": 1}])])
        return items

    def clients(self):
        for case, count, delay in CASES:
            for variant in VARIANTS:
                label = case + "-" + variant
                print("Running %s (%d requests)..." % (label, count), flush=True)
                command = self.k + ["exec", "deployment/client-" + variant, "-c", "audit", "--", "/audit-h2",
                                   "-mode", "client", "-addr", label + ":8080", "-authority", label + ":8080",
                                   "-id", label, "-payload", PAYLOAD, "-delay-ms", str(delay), "-concurrency", str(count)]
                if case == "failfast":
                    command.append("-warmup=false")
                self.codes[label] = self.command(command, label + ".log", check=False, timeout=60).returncode
        self.report["clientExitCodes"] = self.codes

    def collect(self):
        errors = []
        commands = [(self.k + ["get", "pods", "-o", "json"], "pods.json"),
                    (self.k + ["get", "httproutes,services,endpointslices", "-o", "json"], "resources.json"),
                    (self.k + ["get", "events", "-o", "json"], "events.json")]
        for variant in VARIANTS:
            for case in ("refused", "consumed503", "early503", "healthy"):
                name = case + "-" + variant
                commands.append((self.k + ["logs", "deployment/" + name, "-c", "audit"], name + "-server.log"))
            commands.append((self.k + ["logs", "deployment/client-" + variant, "-c", "linkerd-proxy"], "client-" + variant + "-proxy.log"))
        for command, log in commands:
            try:
                self.command(command, log)
            except Exception as error:
                errors.append(str(error))
        try:
            pods = json.loads((self.out / "pods.json").read_text())["items"]
            for variant in VARIANTS:
                matches = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app") == "client-" + variant]
                require(len(matches) == 1, "Expected one client pod: " + variant)
                self.command([self.args.linkerd_bin, "diagnostics", "proxy-metrics", "--context", self.context,
                              "-n", self.namespace, "pod/" + matches[0]["metadata"]["name"]], "client-" + variant + ".prom")
        except Exception as error:
            errors.append(str(error))
        self.report["collectionErrors"] = errors
        require(not errors, "; ".join(errors))

    def verify(self):
        inconclusive = False
        for case, count, delay in CASES:
            for variant in VARIANTS:
                row = {"scenario": case, "variant": variant, "requests": count, "delayMs": delay}
                self.results.append(row)
                try:
                    verify_case(self, row)
                    if case == "failfast" and (row["proxyRetriedRequests"] == 0 or
                                               (variant == "before" and row["duplicateRequests"] == 0)):
                        row["status"] = "INCONCLUSIVE"
                        row["error"] = "Weighted selection did not reproduce an unread-body retry; rerun"
                        inconclusive = True
                    else:
                        row["status"] = "PASS"
                except Exception as error:
                    row.update(status="FAIL", error=str(error))
        pods = json.loads((self.out / "pods.json").read_text())["items"]
        require(len(pods) == 10, "Unexpected pod count")
        for pod in pods:
            require(pod["status"]["phase"] == "Running", "Pod is not running: " + pod["metadata"]["name"])
            containers = {container["name"]: container for container in
                          pod["spec"]["containers"] + pod["spec"].get("initContainers", [])}
            require(containers["audit"]["image"] == self.args.fixture_image, "Unexpected fixture image")
            app = pod["metadata"].get("labels", {}).get("app", "")
            if app.startswith("client-"):
                expected_image = self.args.before_image if app == "client-before" else self.args.after_image
                require(containers.get("linkerd-proxy", {}).get("image") == expected_image, "Unexpected injected proxy image: " + app)
            else:
                require("linkerd-proxy" not in containers, "Server was unexpectedly meshed: " + app)
            statuses = pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", [])
            require(all(status["restartCount"] == 0 for status in statuses), "Container restarted: " + pod["metadata"]["name"])
            require(any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", [])), "Pod is not ready")
        resources = json.loads((self.out / "resources.json").read_text())["items"]
        routes = [value for value in resources if value["kind"] == "HTTPRoute"]
        require(len(routes) == 10 and all(route_ready(route) for route in routes), "Route acceptance changed")
        for variant in VARIANTS:
            slices = [value for value in resources if value["kind"] == "EndpointSlice" and
                      value["metadata"].get("labels", {}).get("kubernetes.io/service-name") == "failfast-" + variant + "-empty"]
            require(slices and not any(value.get("endpoints") for value in slices), "FailFast backend was not empty")
        if any(row["status"] == "FAIL" for row in self.results):
            return "FAIL", 1
        return ("INCONCLUSIVE", 2) if inconclusive else ("PASS", 0)

    def cleanup(self):
        if not self.created:
            self.report["cleanup"] = "Namespace was not created"
        elif self.args.keep:
            self.report["cleanup"] = "Kept namespace (--keep)"
        else:
            self.command(self.k + ["delete", "namespace", self.namespace, "--wait=true", "--timeout=60s"],
                         "cleanup.log", timeout=70)
            self.report["cleanup"] = "Deleted namespace"


def route_ready(route):
    parents = route.get("status", {}).get("parents", [])
    for parent in parents:
        conditions = {c["type"]: c for c in parent.get("conditions", [])}
        if all(conditions.get(name, {}).get("status") == "True" and
               conditions[name].get("observedGeneration", route["metadata"]["generation"]) == route["metadata"]["generation"]
               for name in ("Accepted", "ResolvedRefs")):
            return True
    return False


def metric(path, name, route):
    matches = [line for line in path.read_text().splitlines() if line.startswith(name + "{") and
               ('route_name="' + route + '"') in line]
    require(len(matches) == 1, "Expected one %s series for %s; found %d" % (name, route, len(matches)))
    return int(float(matches[0].rsplit(" ", 1)[1]))


def verify_case(run, row):
    case, variant, count = row["scenario"], row["variant"], row["requests"]
    label = case + "-" + variant
    client = events(run.out / (label + ".log"))
    summaries = [event for event in client if event["event"] == "client_summary"]
    require(len(summaries) == 1 and summaries[0]["total"] == count, label + ": missing/incorrect client summary")
    summary = summaries[0]
    responses = [event for event in client if event["event"] == "client_response"]
    require(len(responses) == count and len({event["id"] for event in responses}) == count, label + ": missing/duplicate responses")
    server_name = ("healthy" if case == "failfast" else case) + "-" + variant
    server = [event for event in events(run.out / (server_name + "-server.log")) if event.get("id", "").startswith(label + "-")]
    incoming = [event for event in client if event["event"] == "client_data"]
    digest = hashlib.sha256(PAYLOAD.encode()).hexdigest()
    require(all(event["bytes"] == len(PAYLOAD) and event["sha256"] == digest and not event.get("error") for event in incoming), label + ": invalid client DATA")
    require(all(n == 1 for n in collections.Counter(event["id"] for event in incoming).values()), label + ": client sent DATA twice")
    duplicate_ids = []
    for response in responses:
        ident = response["id"]
        related = [event for event in server if event.get("id") == ident]
        headers = [event for event in related if event["event"] == "headers"]
        rejected = [event for event in related if event["event"] == "reject"]
        if case == "early503":
            require(response["status"] == "503" and len(headers) == len(rejected) == 1 and rejected[0]["bytes"] == 0 and
                    not any(event["event"] == "data" for event in related), label + ": early 503 control changed")
            continue
        require(response["status"] == "200", label + ": unexpected response status")
        payload = response["body"]
        require(response["response_id"] == ident and payload in (PAYLOAD, PAYLOAD * 2) and response["bytes"] == len(payload) and
                response["sha256"] == hashlib.sha256(payload.encode()).hexdigest(), label + ": unexpected payload/ID/hash")
        if payload == PAYLOAD * 2:
            duplicate_ids.append(ident)
        accepted = [event for event in related if event["event"] == "success"]
        expected_attempt = 1 if case in ("healthy", "failfast") else 2
        require(len(accepted) == 1 and accepted[0]["bytes"] == len(payload) and accepted[0]["attempt"] == expected_attempt and
                response["attempt"] == expected_attempt and len(headers) == expected_attempt, label + ": unexpected server attempts")
        data = [event for event in related if event["event"] == "data" and event["attempt"] == expected_attempt]
        require(sum(event["frame_bytes"] for event in data) == len(payload), label + ": server DATA disagrees with response")
        if case == "refused":
            require(len(rejected) == 1 and rejected[0]["failure"] == "refused" and rejected[0]["bytes"] == 0 and
                    not any(event["event"] == "data" and event["attempt"] == 1 for event in related), label + ": refusal consumed DATA")
            sent = next(event for event in incoming if event["id"] == ident)
            require(timestamp(headers[1]["time"]) < timestamp(sent["time"]), label + ": retry did not precede client DATA")
        elif case == "consumed503":
            require(len(rejected) == 1 and rejected[0]["bytes"] == len(PAYLOAD), label + ": first attempt did not consume body")
    early = case == "early503"
    require(len(incoming) == (0 if early else count), label + ": unexpected number of client writes")
    require(summary["failed"] == (count if early else len(duplicate_ids)) and
            summary["succeeded"] == (0 if early else count - len(duplicate_ids)), label + ": client result disagrees with body audit")
    require(run.codes[label] == (1 if summary["failed"] else 0), label + ": unexpected client exit code")
    metrics = run.out / ("client-" + variant + ".prom")
    retries = metric(metrics, "outbound_http_route_retry_requests_total", label)
    successes = metric(metrics, "outbound_http_route_retry_successes_total", label)
    row.update(payloadMatches=summary["succeeded"], duplicateRequests=len(duplicate_ids), duplicateIds=duplicate_ids,
               responseStatuses=dict(collections.Counter(event["status"] for event in responses)),
               clientDataWrites=len(incoming), proxyRetriedRequests=retries, proxyRetrySuccesses=successes)
    if case in ("refused", "failfast"):
        example = next(response for response in responses if response["id"] == label + "-000")
        row["echo"] = {"sent": PAYLOAD, "received": example["body"], "id": example["id"], "log": label + ".log", "delayMs": row["delayMs"]}
    require(retries == successes, label + ": proxy retry failed")
    if case == "failfast":
        failures = len(re.findall(r"retryable=true error=.*failfast-" + variant + r"-empty.*service in fail-fast",
                                 (run.out / ("client-" + variant + "-proxy.log")).read_text()))
        row["failFastRetryLogs"] = failures
        require(0 <= retries <= count and failures == retries, label + ": FailFast logs disagree with retries")
        if variant == "before":
            require(len(duplicate_ids) == retries, label + ": FailFast retries did not duplicate exactly once")
    else:
        require(retries == (count if case in ("refused", "consumed503") else 0), label + ": unexpected proxy retry count")
    if variant == "after" or case in ("consumed503", "healthy", "early503"):
        require(not duplicate_ids, label + ": body was duplicated")
    elif case == "refused":
        require(len(duplicate_ids) == count, label + ": delayed refusal did not reproduce consistently")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.environ.get("CLUSTER_NAME", "linkerd-replay-repro"))
    parser.add_argument("--fixture-image", default=os.environ.get("FIXTURE_IMAGE", "linkerd-replay-fixture:local"))
    parser.add_argument("--before-image", default=os.environ.get("BEFORE_IMAGE", "linkerd-replay-proxy:before"))
    parser.add_argument("--after-image", default=os.environ.get("AFTER_IMAGE", "linkerd-replay-proxy:after"))
    parser.add_argument("--linkerd-bin", default=os.environ.get("LINKERD_BIN", "linkerd"))
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".artifacts")
    parser.add_argument("--keep", action="store_true", help="Keep this run's namespace for inspection")
    run = Run(parser.parse_args())
    print("Artifacts: " + str(run.out), flush=True)
    code = 1
    collected = False
    try:
        run.prepare()
        run.clients()
        run.collect()
        collected = True
        run.report["status"], code = run.verify()
    except (Exception, KeyboardInterrupt) as error:
        run.report.update(status="FAIL", error=str(error) or "Interrupted")
        if run.created and not collected:
            try:
                run.collect()
            except Exception as collection_error:
                run.report["collectionError"] = str(collection_error)
    finally:
        try:
            run.cleanup()
        except Exception as error:
            run.report.update(cleanup="Failed: " + str(error), status="FAIL")
            code = 1
        write_json(run.out / "summary.json", run.report)
    print("\n%-14s %-7s %7s %9s %7s %s" % ("Scenario", "Proxy", "Matches", "Duplicate", "Retries", "Result"))
    for row in run.results:
        print("%-14s %-7s %7s %9s %7s %s" % (row["scenario"], row["variant"], row.get("payloadMatches", "?"),
              row.get("duplicateRequests", "?"), row.get("proxyRetriedRequests", "?"), row["status"]))
        if row.get("error"):
            print("  " + row["error"])
    if run.report.get("error"):
        print(run.report["error"], file=sys.stderr)
    print(run.report["status"] + ": " + str(run.out / "summary.json"))
    return code


if __name__ == "__main__":
    sys.exit(main())
