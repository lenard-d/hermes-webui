"""Behavioral contract for the repository coverage entry point."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_coverage_entrypoint_writes_a_machine_readable_report(tmp_path):
    sample_test = tmp_path / "test_coverage_sample.py"
    sample_test.write_text(
        "from api.helpers import require\n\n"
        "def test_sample():\n"
        "    assert require({'safe': True}, 'safe') is None\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "coverage.json"
    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    env["HERMES_COVERAGE_JSON"] = str(report_path)

    result = subprocess.run(
        [str(ROOT / "scripts" / "coverage.sh"), str(sample_test), "-q"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["branch_coverage"] is True
    assert report["totals"]["num_statements"] > 0
