"""Capture loopback packets and decode them independently of fixture logs."""

from contextlib import contextmanager
import html
import re
import shlex
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET


@contextmanager
def capture(path):
    process = subprocess.Popen(["tcpdump", "--immediate-mode", "-U", "-B", "65536", "-i", "lo", "-p", "-s", "0",
                                "-w", str(path), "tcp"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace")
    try:
        deadline = time.monotonic() + 10
        while not path.exists() or path.stat().st_size < 24:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Packet capture did not start")
            time.sleep(0.05)
        yield
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            raise RuntimeError("Packet capture did not stop: " + output.strip())
        if process.returncode:
            raise RuntimeError("Packet capture failed: " + output.strip())
        dropped = re.search(r"(\d+) packets dropped by kernel", output)
        if not dropped or int(dropped[1]):
            raise RuntimeError("Packet capture loss is nonzero or unknown: " + output.strip())


def field(node, name, attribute="show"):
    value = node.find('.//field[@name="' + name + '"]')
    return value.get(attribute, "") if value is not None else ""


def requests_from_pdml(path, outbound, backend):
    streams = {}
    for _, packet in ET.iterparse(path, events=("end",)):
        if packet.tag != "packet":
            continue
        source, destination = field(packet, "tcp.srcport"), field(packet, "tcp.dstport")
        port = destination if destination in (outbound, backend) else source
        if port not in (outbound, backend):
            packet.clear()
            continue
        sending = destination == port
        tcp = field(packet, "tcp.stream")
        number = field(packet, "frame.number")
        for frame in packet.findall('.//field[@name="http2.stream"]'):
            stream = field(frame, "http2.streamid")
            if not stream or stream == "0":
                continue
            key = (tcp, stream)
            headers = {field(header, "http2.header.name"): field(header, "http2.header.value")
                       for header in frame.findall('.//field[@name="http2.header"]')}
            if sending and headers.get(":path") in ("/", "/audit.Service/Call"):
                if not headers.get("x-audit-id") or key in streams:
                    raise ValueError("Missing audit ID or repeated request headers in packet " + number)
                streams[key] = {"id": headers["x-audit-id"], "leg": "input" if port == outbound else "output",
                                "tcp": tcp, "stream": stream, "headers": number, "frames": [],
                                "body": b"", "ended": False, "responseEnded": False, "status": "", "reset": ""}
            row = streams.get(key)
            if row is None:
                if sending and field(frame, "http2.type") == "0":
                    raise ValueError("DATA without decoded request headers in packet " + number)
                continue
            kind = field(frame, "http2.type")
            if sending:
                if kind == "0":
                    data = field(frame, "http2.data.data", "value") or field(frame, "http2.data.segment", "value")
                    body = bytes.fromhex(data)
                    length = int(field(frame, "http2.length"))
                    padding = field(frame, "http2.pad_length")
                    if field(frame, "http2.flags.padded") == "True":
                        length -= 1 + int(padding)
                    reassembled = field(frame, "http2.body.reassembled.data", "value")
                    if reassembled:
                        complete = bytes.fromhex(reassembled)
                        if not complete.startswith(row["body"]):
                            raise ValueError("Reassembled DATA disagrees in packet " + number)
                        body = complete[len(row["body"]):]
                    if len(body) != length:
                        raise ValueError("Undecoded DATA bytes in packet " + number)
                    row["body"] += body
                    row["frames"].append(number)
                if kind in ("0", "1") and field(frame, "http2.flags.end_stream") == "True":
                    row["ended"] = True
            else:
                row["status"] = headers.get(":status", row["status"])
                if kind == "3":
                    row["reset"] = field(frame, "http2.rst_stream.error")
                if kind in ("0", "1") and field(frame, "http2.flags.end_stream") == "True":
                    row["responseEnded"] = True
        packet.clear()
    return list(streams.values())


def compare(streams, count):
    inputs = {row["id"]: row for row in streams if row["leg"] == "input"}
    outputs = [row for row in streams if row["leg"] == "output"]
    if len(inputs) != count or sum(row["leg"] == "input" for row in streams) != count:
        raise ValueError("Packet capture has missing or repeated client requests")
    if {row["id"] for row in outputs} != set(inputs):
        raise ValueError("Packet capture has missing or unmatched backend requests")
    duplicates = set()
    for row in streams:
        if not row["reset"] and not (row["status"] and row["responseEnded"]):
            raise ValueError("Incomplete captured response: " + row["id"])
        if not row["ended"] and not row["reset"] and row["status"] != "503":
            raise ValueError("Incomplete captured request body: " + row["id"])
    for row in outputs:
        incoming = inputs[row["id"]]
        if row["reset"] and not row["body"]:
            row["comparison"] = "RESET (no DATA)"
        elif row["status"] == "503" and not row["body"]:
            row["comparison"] = "503 (no DATA)"
        elif row["body"] == incoming["body"]:
            row["comparison"] = "MATCH"
        elif incoming["body"] and row["body"] == incoming["body"] * 2:
            row["comparison"] = "DUPLICATED"
            duplicates.add(row["id"])
        else:
            raise ValueError("Captured body differs unexpectedly: " + row["id"])
    return len(duplicates)


def analyze(path, listeners, count):
    outbound, backend = (listeners[key].rsplit(":", 1)[1] for key in ("outbound", "backend"))
    command = ["tshark", "-n", "-r", path.name, "-d", "tcp.port==" + outbound + ",http2",
               "-d", "tcp.port==" + backend + ",http2", "-o", "tcp.desegment_tcp_streams:TRUE",
               "-Y",
               "http2 && (tcp.port == " + outbound + " || tcp.port == " + backend + ")"]
    with path.with_suffix(".http2.txt").open("w") as decoded, tempfile.TemporaryFile(mode="w+") as pdml:
        for output, options in ((decoded, ["-V", "-x"]), (pdml, ["-T", "pdml"])):
            result = subprocess.run(command + options, cwd=path.parent, stdout=output, stderr=subprocess.PIPE,
                                    text=True, errors="replace", timeout=60)
            if result.returncode:
                raise RuntimeError("TShark failed: " + result.stderr.strip())
        pdml.seek(0)
        streams = requests_from_pdml(pdml, outbound, backend)
    duplicates = compare(streams, count)
    report = path.with_name(path.stem + "-packets.md")
    lines = ["# " + path.stem + " — packet evidence", "",
             "%d/%d requests have duplicated DATA at proxy output." % (duplicates, count), "",
             "Derived from captured HTTP/2 headers and DATA, without reading fixture logs. "
             "Retries are separate output streams; their body lengths are never added together.", "",
             "[PCAP](%s) · [TShark decode](%s)" % (path.name, path.with_suffix(".http2.txt").name), "",
             "Client → proxy: TCP destination port **%s**. Proxy → backend: TCP destination port **%s**." % (outbound, backend), "",
             "| Request ID | Leg | TCP stream | HTTP/2 stream | Header packet | DATA packets | Bytes | Response | Comparison |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in sorted(streams, key=lambda row: (row["id"], row["leg"], int(row["headers"]))):
        lines.append("| %s | %s | %s | %s | %s | %s | %d | %s | %s |" % (
            row["id"], row["leg"], row["tcp"], row["stream"], row["headers"],
            ", ".join(row["frames"]) or "—", len(row["body"]),
            "RST " + row["reset"] if row["reset"] else row["status"], row.get("comparison", "reference")))
    lines.extend(["", "## Inspect independently", "",
                  "Open the PCAP in Wireshark. Use **Analyze → Decode As… → HTTP2** for both TCP ports above. "
                  "Select a row with `tcp.stream == N && http2.streamid == M`; "
                  "add `tcp.dstport == PORT && http2.type == 0` for request DATA. "
                  "A DATA packet number may repeat when a TCP packet carries multiple HTTP/2 frames.", "",
                  "Or run this command from this directory to decode the PCAP directly:", "",
                  "```sh", shlex.join(command + ["-V", "-x"]), "```", "", "## Captured request bodies", ""])
    for row in streams:
        if row["body"]:
            lines.extend(["- `%s` %s (TCP %s / HTTP/2 %s): <code>%s</code>" % (
                row["id"], row["leg"], row["tcp"], row["stream"],
                html.escape(row["body"].decode("utf-8", errors="backslashreplace")))])
    report.write_text("\n".join(lines) + "\n")
    return duplicates
