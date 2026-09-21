#!/usr/bin/env bash
# validate_release.sh — LEG-102: prove the built release artifact installs and
# runs, as an external consumer would see it.
#
# Builds the wheel, installs it into a THROWAWAY venv (dependencies resolved
# from the index, exactly as a consumer's `pip install legio` would), and runs
# a headless, domain-free smoke against the *installed* package — never the
# source tree:
#
#   - `import legio` from the wheel; `legio.__version__ == the pinned version`;
#   - boot a node (materializer from the wheel) over the `examples/` single
#     source (the consumer-side tool implementations ride PYTHONPATH, the same
#     way a consumer repo provides its own tools);
#   - submit → status round-trip completes with the documented output.
#
# The result is recorded (pinned version + smoke outcome + timestamp) in
# `docs/VALIDATIONS/release-artifact-<version>.md`. The gate is
# `make validate-release`. Loud on any failure (AGENTS.md rule 9): a missing
# index, a broken wheel or a failing smoke each abort with a non-zero exit.
#
# USAGE: scripts/validate_release.sh [VERSION]   (default: the Makefile VERSION)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-0.1.0}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$REPO_ROOT"

echo "validate-release: building the wheel (uv build)"
uv build --quiet --out-dir "$WORK/dist"

WHEEL="$(ls "$WORK"/dist/*.whl)"
echo "validate-release: artifact wheel=$WHEEL"

echo "validate-release: throwaway venv + install (consumer-style, deps from index)"
uv venv --quiet "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" "$WHEEL"

echo "validate-release: headless consumer smoke against the installed artifact"
OUT="$("$WORK/venv/bin/python" - "$VERSION" "$REPO_ROOT" <<'PY'
import asyncio
import sys
import tempfile
import time
from pathlib import Path

version, repo_root = sys.argv[1], Path(sys.argv[2])

import legio
from legio import logging as legio_logging

legio_logging.configure(level="INFO")

if legio.__version__ != version:
    sys.exit(f"installed version {legio.__version__} != pinned {version}")
print(f"validate-release: import ok __version__={legio.__version__}", flush=True)

from legio.cli import _shutdown_pumps, bring_catalog_up, executor_loop, executor_pump_count
from legio.config import load
from legio.materializer import boot_node
from legio.runtime import TaskState


async def main() -> None:
    node = repo_root / "examples" / "transform"
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "smoke.db"
    config = tmp / "legio.yaml"
    config.write_text(
        "node:\n"
        f'  id: "validate-release@example"\n'
        f'database:\n  db_path: "{db_path}"\n'
        f'patterns:\n  tool: "{node / "patterns" / "tool"}"\n'
        f'  linguistic: "{node / "patterns" / "linguistic"}"\n'
        f'  composite: "{node / "patterns" / "composite"}"\n'
        f'tools:\n  config: "{node / "tools.yaml"}"\n'
        "lifecycle:\n"
        "  default:\n"
        "    drain_timeout: 10.0\n"
        "    drain_interval: 0.02\n",
        encoding="utf-8",
    )
    loaded = load(config)
    booted = await boot_node(loaded)
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(executor_pump_count(booted))
    ]
    try:
        await bring_catalog_up(booted)
        task_id = await booted.runtime.submit(
            "smoke", (("transform", "transform"),), {"text": "hello", "factor": 2}
        )
        deadline = time.monotonic() + 15
        entry = None
        while time.monotonic() < deadline:
            entry = await booted.runtime.status(task_id, "smoke")
            if entry.state == TaskState.COMPLETED:
                break
            await asyncio.sleep(0.02)
        assert entry is not None and entry.state == TaskState.COMPLETED, entry
        assert entry.output == {"transform": {"transformed": "HELLOHELLO"}}, entry
        print(
            f"validate-release: round-trip ok task={task_id} output={entry.output}",
            flush=True,
        )
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()


asyncio.run(main())
print("validate-release: artifact smoke passed", flush=True)
PY
)"
echo "$OUT"

RECORD="docs/VALIDATIONS/release-artifact-${VERSION}.md"
cat > "$RECORD" <<EOF
# VALIDATIONS — release artifact ${VERSION} (LEG-102, in-repo harness)

Executable proof that the built artifact installs and runs as a consumer would
see it: built via \`uv build\`, installed into a throwaway virtualenv (deps from
the index), headless domain-free smoke against the **installed** package over
the \`examples/\` single source. The gate: \`make validate-release\`.

- Pinned version: \`${VERSION}\`
- Artifact: \`${WHEEL##*/}\`
- Smoke: $(printf '%s' "$OUT" | tr '\n' '; ')
- Run at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

The real external-consumer repository (an own repo pinning the release) is the
maintainer's follow-up; this record is the in-repo proof of the wheel.
EOF

echo "validate-release: record written $RECORD"
echo "validate-release: ok pinned=$VERSION"