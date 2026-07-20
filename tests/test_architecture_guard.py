from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import check_architecture


def _write(root: Path, relative: str, source: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "api/__init__.py")
    return root


def _finding_keys(analysis: check_architecture.Analysis) -> set[tuple[str, str, str]]:
    return {finding.key for finding in analysis.findings}


def test_planned_dependency_rules_apply_to_newly_discovered_packages(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for package in ("runs", "sessions", "config", "profiles", "providers"):
        _write(root, f"api/{package}/__init__.py")
    _write(root, "api/routes.py")
    _write(root, "api/streaming.py")
    _write(root, "api/providers/client.py")
    _write(root, "api/runs/execution.py", "import api.routes\n")
    _write(root, "api/config/io.py", "import api.providers\n")
    _write(root, "api/sessions/repository.py", "import api.streaming\n")
    _write(root, "api/sessions/service.py", "import api.providers.client\n")

    analysis = check_architecture.analyze(root)
    keys = _finding_keys(analysis)

    assert (
        "domain-depends-on-http",
        "api.runs.execution",
        "api.routes",
    ) in keys
    assert (
        "config-foundation-depends-upward",
        "api.config.io",
        "api.providers",
    ) in keys
    assert (
        "persistence-depends-on-transport",
        "api.sessions.repository",
        "api.streaming",
    ) in keys
    assert (
        "cross-package-private-import",
        "api.sessions.service",
        "api.providers.client",
    ) in keys
    assert analysis.packages == ("config", "profiles", "providers", "runs", "sessions")


def test_cross_package_import_through_public_interface_is_allowed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "api/runs/__init__.py")
    _write(root, "api/sessions/__init__.py")
    _write(root, "api/runs/execution.py", "from api import sessions\n")
    _write(root, "api/sessions/repository.py", "from . import lifecycle\n")
    _write(root, "api/sessions/lifecycle.py")

    analysis = check_architecture.analyze(root)

    assert not {
        finding.key
        for finding in analysis.findings
        if finding.rule == "cross-package-private-import"
    }
    assert analysis.package_edges == (("runs", "sessions"),)


def test_each_new_edge_that_participates_in_package_cycle_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "api/alpha/__init__.py", "import api.beta\n")
    _write(root, "api/beta/__init__.py", "import api.gamma\n")
    _write(root, "api/gamma/__init__.py", "import api.alpha\n")

    analysis = check_architecture.analyze(root)

    assert analysis.cyclic_edges == (
        ("alpha", "beta"),
        ("beta", "gamma"),
        ("gamma", "alpha"),
    )
    assert {
        (finding.source, finding.target)
        for finding in analysis.findings
        if finding.rule == "package-cycle"
    } == {
        ("api.alpha", "api.beta"),
        ("api.beta", "api.gamma"),
        ("api.gamma", "api.alpha"),
    }


def test_parts_packages_and_facade_binders_are_explicit_debt(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "api/legacy_parts/__init__.py")
    _write(
        root,
        "api/legacy.py",
        "import sys\n\n"
        "def bind_legacy_api(resolver):\n"
        "    return resolver\n\n"
        "bind_legacy_api(lambda: sys.modules[__name__])\n",
    )

    keys = _finding_keys(check_architecture.analyze(root))

    assert (
        "parts-package-debt",
        "api/legacy_parts",
        "package-pattern",
    ) in keys
    assert ("facade-binder-debt", "api.legacy", "bind_legacy_api") in keys
    assert (
        "facade-binder-debt",
        "api.legacy",
        "sys.modules[__name__]",
    ) in keys


def test_exact_baseline_allows_legacy_but_rejects_new_and_stale_entries(
    tmp_path: Path, capsys
) -> None:
    root = _repo(tmp_path)
    _write(root, "api/runs/__init__.py")
    _write(root, "api/routes.py")
    _write(root, "api/runs/execution.py", "import api.routes\n")
    analysis = check_architecture.analyze(root)
    key = next(
        finding.key
        for finding in analysis.findings
        if finding.rule == "domain-depends-on-http"
    )

    assert check_architecture.check(analysis, {key: "reviewed legacy edge"}) == 0
    assert "no new architecture debt" in capsys.readouterr().out

    assert check_architecture.check(analysis, {}) == 1
    output = capsys.readouterr().out
    assert "ERROR domain-depends-on-http: api.runs.execution:1 -> api.routes" in output

    stale_key = ("parts-package-debt", "api/gone_parts", "package-pattern")
    assert check_architecture.check(analysis, {key: "legacy", stale_key: "removed"}) == 1
    assert "ERROR stale-baseline: parts-package-debt: api/gone_parts" in capsys.readouterr().out


def test_invalid_python_fails_closed_with_parse_error(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "api/broken.py", "def nope(:\n")

    analysis = check_architecture.analyze(root)

    parse_errors = [finding for finding in analysis.findings if finding.rule == "parse-error"]
    assert len(parse_errors) == 1
    assert parse_errors[0].source == "api.broken"
    assert parse_errors[0].line == 1


def test_baseline_requires_exact_entries_and_review_reasons(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "version": 1,
                "allowed_violations": [
                    {
                        "rule": "package-cycle",
                        "source": "api.alpha",
                        "target": "api.beta",
                        "reason": "remove when alpha no longer imports beta",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert check_architecture.load_baseline(baseline) == {
        ("package-cycle", "api.alpha", "api.beta"): "remove when alpha no longer imports beta"
    }

    baseline.write_text(
        json.dumps(
            {
                "version": 1,
                "allowed_violations": [
                    {"rule": "package-cycle", "source": "api.alpha", "target": "api.beta"}
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        check_architecture.load_baseline(baseline)
    except ValueError as exc:
        assert "reviewable reason" in str(exc)
    else:
        raise AssertionError("a reasonless architecture exception must fail closed")


def test_cli_output_and_inventory_are_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "api/zeta/__init__.py", "import api.alpha\n")
    _write(root, "api/alpha/__init__.py")
    baseline = root / "baseline.json"
    baseline.write_text(
        json.dumps({"version": 1, "allowed_violations": []}), encoding="utf-8"
    )
    script = Path(check_architecture.__file__)
    command = [
        sys.executable,
        str(script),
        "--root",
        str(root),
        "--baseline",
        str(baseline),
        "--inventory",
    ]

    first = subprocess.run(command, check=False, capture_output=True, text=True)
    second = subprocess.run(command, check=False, capture_output=True, text=True)

    assert first.returncode == 0
    assert second.returncode == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ""
    assert first.stdout.index("  api.alpha") < first.stdout.index("  api.zeta")
    assert "  api.zeta -> api.alpha" in first.stdout
    assert "Internal module dependencies:\n  api.zeta -> api.alpha" in first.stdout


def test_repository_architecture_baseline_is_current() -> None:
    root = Path(__file__).resolve().parents[1]
    analysis = check_architecture.analyze(root)
    baseline = check_architecture.load_baseline(
        root / "scripts" / "architecture_baseline.json"
    )

    assert check_architecture.check(analysis, baseline) == 0
