#!/usr/bin/env python3
"""Check Python package dependency direction without third-party tooling.

The checker parses ``api/**/*.py`` with the standard-library AST.  It discovers
top-level packages from the checkout, so a newly added ``api/<domain>/`` package
is covered without editing this script.

Default mode compares violations with ``scripts/architecture_baseline.json``.
Only exact, reviewed legacy findings are allowed; a new finding or a stale
baseline entry fails the check.  ``--inventory`` prints the discovered package
graph and all architecture debt.  ``--print-baseline`` emits a deterministic
baseline candidate for deliberate review -- it never rewrites the baseline.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = Path(__file__).with_name("architecture_baseline.json")

HTTP_ROOTS = frozenset({"http", "routes", "routes_parts"})
STREAM_ROOTS = frozenset({"streaming", "streaming_parts"})
NON_DOMAIN_PACKAGES = frozenset({"http", "routes_parts"})
CONFIG_SOURCE_ROOTS = frozenset({"config", "config_parts"})
CONFIG_UPWARD_ROOTS = frozenset(
    {"profiles", "profiles_parts", "providers", "provider_parts"}
)
PERSISTENCE_TOKEN = re.compile(
    r"(?:^|_)(?:repository|persistence|storage|store)(?:$|_)"
)
BINDER_NAME = re.compile(
    r"^(?:bind_.+_(?:api|function)|install_.+_part|_bind_(?:function|class)|facade_attr)$"
)


@dataclass(frozen=True, order=True)
class ImportRef:
    source: str
    target: str
    line: int


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    source: str
    target: str
    detail: str
    line: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.source, self.target)


@dataclass(frozen=True)
class Analysis:
    modules: tuple[str, ...]
    packages: tuple[str, ...]
    imports: tuple[ImportRef, ...]
    package_edges: tuple[tuple[str, str], ...]
    cyclic_edges: tuple[tuple[str, str], ...]
    findings: tuple[Finding, ...]


def _module_name(api_root: Path, path: Path) -> tuple[str, bool]:
    relative = path.relative_to(api_root.parent)
    parts = list(relative.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _relative_base(source: str, is_package: bool, level: int, module: str | None) -> str:
    package_parts = source.split(".") if is_package else source.split(".")[:-1]
    keep = len(package_parts) - (level - 1)
    if keep < 1:
        return ""
    parts = package_parts[:keep]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _is_api_name(name: str) -> bool:
    return name == "api" or name.startswith("api.")


def _import_refs(
    tree: ast.AST,
    source: str,
    is_package: bool,
    known_modules: set[str],
) -> list[ImportRef]:
    refs: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_api_name(alias.name):
                    refs.append(ImportRef(source, alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(source, is_package, node.level, node.module)
            else:
                base = node.module or ""
            if not _is_api_name(base):
                continue
            for alias in node.names:
                target = base
                if alias.name != "*":
                    candidate = f"{base}.{alias.name}"
                    if alias.name.startswith("_") or candidate in known_modules:
                        target = candidate
                refs.append(ImportRef(source, target, node.lineno))
    return refs


def _top_level_root(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "api" else None


def _package_for(module: str, packages: set[str]) -> str | None:
    root = _top_level_root(module)
    return root if root in packages else None


def _is_persistence_module(module: str) -> bool:
    return any(PERSISTENCE_TOKEN.search(part) for part in module.split(".")[1:])


def _binder_markers(tree: ast.AST) -> set[str]:
    markers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and BINDER_NAME.match(node.name):
            markers.add(node.name)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name and BINDER_NAME.match(name):
                markers.add(name)
        elif _is_sys_modules_self(node):
            markers.add("sys.modules[__name__]")
    return markers


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_sys_modules_self(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    return (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "sys"
        and value.attr == "modules"
        and isinstance(node.slice, ast.Name)
        and node.slice.id == "__name__"
    )


def _cyclic_edges(edges: set[tuple[str, str]]) -> set[tuple[str, str]]:
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)

    def reaches(start: str, goal: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            node = pending.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            pending.extend(sorted(adjacency.get(node, ()), reverse=True))
        return False

    return {(source, target) for source, target in edges if reaches(target, source)}


def analyze(root: Path) -> Analysis:
    root = root.resolve()
    api_root = root / "api"
    if not api_root.is_dir():
        finding = Finding("scan-error", "api", "missing", f"missing directory: {api_root}")
        return Analysis((), (), (), (), (), (finding,))

    paths = sorted(api_root.rglob("*.py"), key=lambda path: path.relative_to(root).as_posix())
    module_records = [(_module_name(api_root, path), path) for path in paths]
    known_modules = {module for (module, _), _path in module_records}
    packages = {
        path.parent.name
        for path in paths
        if path.name == "__init__.py" and path.parent.parent == api_root
    }
    imports: list[ImportRef] = []
    findings: list[Finding] = []

    for (module, is_package), path in module_records:
        relative_path = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            line = getattr(exc, "lineno", 0) or 0
            findings.append(
                Finding("parse-error", module, relative_path, str(exc), line)
            )
            continue
        imports.extend(_import_refs(tree, module, is_package, known_modules))
        for marker in sorted(_binder_markers(tree)):
            findings.append(
                Finding(
                    "facade-binder-debt",
                    module,
                    marker,
                    "temporary facade binding must be removed during package migration",
                )
            )

    for path in sorted(api_root.iterdir(), key=lambda item: item.name):
        if path.is_dir() and path.name.endswith("_parts"):
            findings.append(
                Finding(
                    "parts-package-debt",
                    f"api/{path.name}",
                    "package-pattern",
                    "replace foo.py + foo_parts/ with one real domain package",
                )
            )

    package_edges: set[tuple[str, str]] = set()
    package_set = set(packages)
    for ref in imports:
        source_root = _package_for(ref.source, package_set)
        target_root = _package_for(ref.target, package_set)
        if source_root and target_root and source_root != target_root:
            package_edges.add((source_root, target_root))

        target_top = _top_level_root(ref.target)
        if source_root and source_root not in NON_DOMAIN_PACKAGES and target_top in HTTP_ROOTS:
            findings.append(
                Finding(
                    "domain-depends-on-http",
                    ref.source,
                    ref.target,
                    "domain packages may not import HTTP/router transport",
                    ref.line,
                )
            )

        source_top = _top_level_root(ref.source)
        if source_top in CONFIG_SOURCE_ROOTS and target_top in CONFIG_UPWARD_ROOTS:
            findings.append(
                Finding(
                    "config-foundation-depends-upward",
                    ref.source,
                    ref.target,
                    "configuration foundations may not import profiles/providers",
                    ref.line,
                )
            )

        if _is_persistence_module(ref.source) and target_top in HTTP_ROOTS | STREAM_ROOTS:
            findings.append(
                Finding(
                    "persistence-depends-on-transport",
                    ref.source,
                    ref.target,
                    "persistence modules may not import HTTP or stream transport",
                    ref.line,
                )
            )

        if source_root and target_root and source_root != target_root:
            target_parts = ref.target.split(".")
            if len(target_parts) > 2:
                findings.append(
                    Finding(
                        "cross-package-private-import",
                        ref.source,
                        ref.target,
                        "cross-package imports must use the target package interface",
                        ref.line,
                    )
                )

    cyclic_edges = _cyclic_edges(package_edges)
    for source, target in cyclic_edges:
        findings.append(
            Finding(
                "package-cycle",
                f"api.{source}",
                f"api.{target}",
                "package dependency edge participates in a cycle",
            )
        )

    # Multiple imports of one module edge are one dependency finding.  Keep the
    # earliest line solely for a useful diagnostic; baseline identity excludes it.
    deduplicated: dict[tuple[str, str, str], Finding] = {}
    for finding in sorted(findings):
        current = deduplicated.get(finding.key)
        if current is None or (finding.line and finding.line < current.line):
            deduplicated[finding.key] = finding

    return Analysis(
        modules=tuple(sorted(known_modules)),
        packages=tuple(sorted(packages)),
        imports=tuple(sorted(set(imports))),
        package_edges=tuple(sorted(package_edges)),
        cyclic_edges=tuple(sorted(cyclic_edges)),
        findings=tuple(sorted(deduplicated.values())),
    )


def load_baseline(path: Path) -> dict[tuple[str, str, str], str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline {path}: {exc}") from exc
    if raw.get("version") != 1 or not isinstance(raw.get("allowed_violations"), list):
        raise ValueError("baseline must have version 1 and an allowed_violations list")
    entries: dict[tuple[str, str, str], str] = {}
    for index, item in enumerate(raw["allowed_violations"]):
        if not isinstance(item, dict):
            raise ValueError(f"baseline entry {index} must be an object")
        values = tuple(item.get(field) for field in ("rule", "source", "target"))
        reason = item.get("reason")
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"baseline entry {index} needs non-empty rule/source/target")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"baseline entry {index} needs a reviewable reason")
        if values in entries:
            raise ValueError(f"duplicate baseline entry: {' | '.join(values)}")
        entries[values] = reason
    return entries


def _finding_location(finding: Finding) -> str:
    return f"{finding.source}:{finding.line}" if finding.line else finding.source


def _print_inventory(analysis: Analysis) -> None:
    print("Packages:")
    for package in analysis.packages:
        print(f"  api.{package}")
    print("Package dependencies:")
    for source, target in analysis.package_edges:
        suffix = " [cyclic]" if (source, target) in analysis.cyclic_edges else ""
        print(f"  api.{source} -> api.{target}{suffix}")
    print("Internal module dependencies:")
    for source, target in sorted({(ref.source, ref.target) for ref in analysis.imports}):
        print(f"  {source} -> {target}")
    print("Architecture findings:")
    for finding in analysis.findings:
        print(f"  {finding.rule}: {_finding_location(finding)} -> {finding.target}")


def _baseline_document(findings: Iterable[Finding]) -> str:
    entries = [
        {
            "rule": finding.rule,
            "source": finding.source,
            "target": finding.target,
            "reason": _baseline_reason(finding),
        }
        for finding in sorted(findings)
        if finding.rule not in {"parse-error", "scan-error"}
    ]
    return json.dumps(
        {
            "version": 1,
            "description": "Exact legacy exceptions for the dependency architecture guard.",
            "allowed_violations": entries,
        },
        indent=2,
        sort_keys=False,
    ) + "\n"


def _baseline_reason(finding: Finding) -> str:
    if finding.rule == "config-foundation-depends-upward":
        return "Legacy config facade reads profile/provider state; remove in the config-stack migration."
    if finding.rule == "domain-depends-on-http":
        return "Legacy domain owner reaches the route facade; remove in the owning run/transport migration."
    if finding.rule == "persistence-depends-on-transport":
        return "Legacy persistence owner reaches streaming state; move that state to a lower-level owner."
    if finding.rule == "facade-binder-debt":
        return "Compatibility facade binding; remove when this owner exposes a direct package interface."
    if finding.rule == "parts-package-debt":
        return "Legacy foo.py plus foo_parts layout; replace in this domain's atomic package migration."
    if finding.rule == "cross-package-private-import":
        return "Legacy cross-package implementation import; route through the target package interface."
    if finding.rule == "package-cycle":
        return "Legacy cyclic package edge; remove during the owning package migration."
    return "Legacy architecture debt recorded 2026-07-20; remove with its owning migration."


def check(analysis: Analysis, baseline: dict[tuple[str, str, str], str]) -> int:
    current = {finding.key: finding for finding in analysis.findings}
    new_keys = sorted(current.keys() - baseline.keys())
    stale_keys = sorted(baseline.keys() - current.keys())
    allowed_count = len(current.keys() & baseline.keys())

    print(
        "architecture_check: "
        f"scanned {len(analysis.modules)} module(s), {len(analysis.packages)} package(s), "
        f"{len(analysis.package_edges)} package edge(s); "
        f"{allowed_count} reviewed legacy finding(s)."
    )
    for key in new_keys:
        finding = current[key]
        print(
            f"ERROR {finding.rule}: {_finding_location(finding)} -> {finding.target}\n"
            f"  {finding.detail}"
        )
    for rule, source, target in stale_keys:
        print(
            f"ERROR stale-baseline: {rule}: {source} -> {target}\n"
            "  the legacy finding is gone; remove this exception from the baseline"
        )
    if new_keys or stale_keys:
        print(
            "architecture_check: FAILED. Fix the dependency or make a narrow, reviewed "
            "baseline change with an explicit migration reason."
        )
        return 1
    print("architecture_check: OK -- no new architecture debt.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE, help="reviewed legacy baseline"
    )
    parser.add_argument("--inventory", action="store_true", help="print the dependency inventory")
    parser.add_argument(
        "--print-baseline",
        action="store_true",
        help="print a baseline candidate for review; do not run the gate",
    )
    args = parser.parse_args(argv)

    analysis = analyze(args.root)
    if args.inventory:
        _print_inventory(analysis)
    if args.print_baseline:
        print(_baseline_document(analysis.findings), end="")
        return 0
    try:
        baseline = load_baseline(args.baseline)
    except ValueError as exc:
        print(f"architecture_check: FAILED: {exc}", file=sys.stderr)
        return 2
    return check(analysis, baseline)


if __name__ == "__main__":
    raise SystemExit(main())
