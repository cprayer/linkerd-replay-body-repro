from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from packets import analyze, capture, compare, requests_from_pdml


class PacketTest(unittest.TestCase):
    def test_capture_checks_loss_without_writing_diagnostic_files(self):
        for output, fails in (("0 packets dropped by kernel\n", False),
                              ("2 packets dropped by kernel\n", True), ("missing statistics\n", True)):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "capture.pcap"
                path.write_bytes(bytes(24))
                process = Mock(returncode=0)
                process.poll.return_value = None
                process.communicate.return_value = (output, None)
                with patch("packets.subprocess.Popen", return_value=process):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "Packet capture loss"):
                            with capture(path):
                                pass
                    else:
                        with capture(path):
                            pass
                process.send_signal.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_tshark_failure_keeps_error_in_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.pcap"
            result = Mock(returncode=1, stderr="invalid capture")
            with patch("packets.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "TShark failed: invalid capture"):
                    analyze(path, {"outbound": "127.0.0.1:4140", "backend": "127.0.0.1:8080"}, 1)
            self.assertFalse(list(Path(directory).glob("*.log")))

    def test_multiplexed_data_reassembly_and_retries(self):
        document = ET.Element("pdml")

        def fields(parent, values):
            for name, value in values.items():
                ET.SubElement(parent, "field", name=name, show=value, value=value)

        def packet(tcp, destination, frames):
            node = ET.SubElement(document, "packet")
            fields(node, {"tcp.stream": tcp, "tcp.dstport": destination,
                          "tcp.srcport": "50000" if destination in ("4140", "8080") else "8080",
                          "frame.number": str(len(document))})
            for stream, kind, values in frames:
                frame = ET.SubElement(node, "field", name="http2.stream")
                fields(frame, {"http2.streamid": stream, "http2.type": kind})
                for name in ("x-audit-id", ":path", ":status"):
                    if name in values:
                        header = ET.SubElement(frame, "field", name="http2.header")
                        fields(header, {"http2.header.name": name, "http2.header.value": values[name]})
                fields(frame, {k: v for k, v in values.items() if k.startswith("http2.")})

        def headers(stream, ident, path="/audit.Service/Call"):
            return stream, "1", {"x-audit-id": ident, ":path": path}

        def data(stream, hex_value, ended=True, reassembled=""):
            values = {"http2.data.data": hex_value, "http2.length": "1",
                      "http2.flags.end_stream": str(ended)}
            if reassembled:
                values["http2.body.reassembled.data"] = reassembled
            return stream, "0", values

        packet("0", "4140", [headers("1", "a", "/"), headers("3", "b"),
                              data("1", "61"), data("3", "62")])
        packet("0", "50000", [(s, "1", {":status": "200", "http2.flags.end_stream": "True"}) for s in ("1", "3")])
        packet("1", "8080", [headers("1", "a", "/"), headers("3", "b"), headers("5", "b"),
                              data("1", "61", False), data("3", "62"),
                              data("1", "6161", reassembled="6161"), data("5", "62")])
        packet("1", "50000", [(s, "1", {":status": "503" if s == "3" else "200",
                                          "http2.flags.end_stream": "True"}) for s in ("1", "3", "5")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packets.pdml"
            ET.ElementTree(document).write(path)
            streams = requests_from_pdml(path, "4140", "8080")
        self.assertEqual(compare(streams, 2), 1)
        outputs = [row for row in streams if row["leg"] == "output"]
        self.assertEqual([row["body"] for row in outputs], [b"aa", b"b", b"b"])
        self.assertEqual([row["comparison"] for row in outputs], ["DUPLICATED", "MATCH", "MATCH"])
        outputs[0]["responseEnded"] = False
        with self.assertRaisesRegex(ValueError, "Incomplete captured response"):
            compare(streams, 2)

    def test_missing_or_truncated_capture_cannot_report_zero_duplicates(self):
        with self.assertRaisesRegex(ValueError, "missing or repeated"):
            compare([], 1)
        stream = {"id": "a", "leg": "input", "reset": "", "status": "200",
                  "ended": False, "responseEnded": True, "body": b""}
        with self.assertRaisesRegex(ValueError, "missing or unmatched"):
            compare([stream], 1)
        with self.assertRaisesRegex(ValueError, "Incomplete captured request"):
            compare([stream, dict(stream, leg="output")], 1)


if __name__ == "__main__":
    unittest.main()
