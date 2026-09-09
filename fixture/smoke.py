#!/usr/bin/env python3
"""Exercise the fixture directly, without Linkerd. Uses only Python's stdlib."""
import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, **kwargs)


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith('{')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=os.environ.get('FIXTURE_IMAGE', 'linkerd-replay-fixture:local'))
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'results/smoke')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summary = []
    suffix = uuid.uuid4().hex[:8]
    for failure, consume in [('refused', False), ('http503', False), ('grpc14', False), ('http503', True), ('none', False)]:
        case = failure + ('-consumed' if consume else '')
        name = 'replay-fixture-smoke-' + suffix + '-' + case
        cmd = ['docker', 'run', '--rm', '-d', '--name', name, args.image, '-mode', 'server', '-failure', failure]
        if consume:
            cmd += ['-consume-first']
        started = run(cmd, check=True)
        (args.output / (case + '-container.txt')).write_text(started.stdout)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(e['event'] == 'listening' for e in events(run(['docker', 'logs', name], check=True).stdout)):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('server did not start: ' + case)
            invocations = 1 if failure == 'none' else 2
            for attempt in range(1, invocations + 1):
                cmd = ['docker', 'run', '--rm', '--network', 'container:' + name, args.image,
                       '-mode', 'client', '-addr', '127.0.0.1:8080', '-id', 'smoke',
                       '-payload', 'hello world', '-delay-ms', '200']
                if failure == 'grpc14':
                    cmd += ['-grpc']
                result = run(cmd)
                (args.output / f'{case}-client{attempt}.log').write_text(result.stdout + result.stderr)
                expected_exit = int(attempt == 1 and failure != 'none')
                assert result.returncode == expected_exit, (case, attempt, result.returncode, result.stdout, result.stderr)
                observed = events(result.stdout)
                if expected_exit == 0:
                    successes = [e for e in observed if e['event'] == 'client_success']
                    assert len(successes) == 1 and successes[0]['bytes'] == 11 and successes[0]['attempt'] == attempt
                else:
                    errors = [e['error'] for e in observed if e['event'] == 'client_failure']
                    expected = {'refused': 'RST_STREAM(REFUSED_STREAM)', 'http503': 'status=503', 'grpc14': 'grpc-status=14'}[failure]
                    assert len(errors) == 1 and expected in errors[0], errors
                summary.append({'case': case, 'invocation': attempt, 'exit': result.returncode})
            server_log = run(['docker', 'logs', name], check=True).stdout
            (args.output / (case + '-server.log')).write_text(server_log)
            observed = events(server_log)
            assert len([e for e in observed if e['event'] == 'headers']) == invocations
            rejected = [e for e in observed if e['event'] == 'reject']
            if failure == 'none':
                assert not rejected
            else:
                assert len(rejected) == 1 and rejected[0]['attempt'] == 1
                assert rejected[0]['bytes'] == (11 if consume else 0)
        finally:
            run(['docker', 'rm', '-f', name], check=True)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
