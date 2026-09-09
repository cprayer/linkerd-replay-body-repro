#!/bin/sh
set -eu
fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker build -t "${FIXTURE_IMAGE:-linkerd-replay-fixture:local}" "$fixture_dir"
