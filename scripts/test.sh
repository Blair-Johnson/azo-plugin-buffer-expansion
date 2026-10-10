#!/usr/bin/env bash
set -euo pipefail
: "${AZO_HOST_MANIFEST:?Set AZO_HOST_MANIFEST to the Agent Zoo host pixi.toml}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec env -u PYTHONPATH -u PYTHONHOME pixi run --manifest-path "$AZO_HOST_MANIFEST" --frozen --no-install python -m pytest -q "$ROOT/tests" "$@"
