"""Read-only Project OS dashboard projection for the HTTP interface."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import j
from api.workspace import (
    get_last_workspace,
    git_info_for_workspace,
    read_file_content,
)


def workspace_read(repo_root: Path, relative_path: str) -> dict | None:
    try:
        return read_file_content(repo_root, relative_path)
    except Exception:
        return None


def workspace_json(repo_root: Path, relative_path: str) -> dict | None:
    payload = workspace_read(repo_root, relative_path)
    if not payload or not isinstance(payload.get("content"), str):
        return None
    try:
        parsed = json.loads(payload.get("content") or "")
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def truth_board_slugs(repo_root: Path) -> set[str]:
    slugs: set[str] = set()

    def add(value) -> None:
        text = str(value or "").strip()
        if text:
            slugs.add(text)

    for relative_path in (
        ".ax/handoff/current.json",
        ".ax/status/active.json",
        ".ax/status/heartbeat.json",
    ):
        truth = workspace_json(repo_root, relative_path)
        if not isinstance(truth, dict):
            continue
        raw_board = truth.get("board")
        if isinstance(raw_board, dict):
            add(raw_board.get("slug"))
            add(raw_board.get("id"))
            add(raw_board.get("name"))
            add(raw_board.get("display_name"))
        else:
            add(raw_board)
        for key in (
            "selected_board_slug",
            "canonical_backlog_board_id",
            "current_browser_board_id",
            "active_proof_board_id",
            "recover_board_id",
        ):
            add(truth.get(key))
    return slugs


def repo_matches_board(repo_root: Path, board_slug: str | None) -> bool:
    slug = str(board_slug or "").strip()
    return bool(slug and slug in truth_board_slugs(repo_root))


def candidate_repo_roots(workspace_root: Path | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return
        if not resolved.is_dir():
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    add(workspace_root)
    scan_root = None
    if workspace_root is not None:
        try:
            scan_root = workspace_root.expanduser().resolve()
        except Exception:
            scan_root = None
    if not scan_root or not scan_root.is_dir():
        return candidates

    skip_names = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
        "vendor",
        "dist",
        "build",
    }
    queue_dirs: list[tuple[Path, int]] = [(scan_root, 0)]
    inspected = 0
    while queue_dirs and inspected < 300:
        current, depth = queue_dirs.pop(0)
        inspected += 1
        if (current / ".ax").is_dir() or (current / "docs" / "project-os").is_dir():
            add(current)
        if depth >= 3:
            continue
        try:
            children = sorted(
                (child for child in current.iterdir() if child.is_dir()),
                key=lambda child: child.name,
            )
        except Exception:
            continue
        for child in children:
            name = child.name
            if name in skip_names or (name.startswith(".") and name != ".ax"):
                continue
            queue_dirs.append((child, depth + 1))
    add(Path.cwd())
    return candidates


def resolve_repo_root_for_board(
    repo_root: Path | None, board_slug: str | None
) -> Path | None:
    slug = str(board_slug or "").strip()
    if not slug:
        return repo_root if repo_root and repo_root.exists() else None
    if repo_root and repo_root.exists() and repo_matches_board(repo_root, slug):
        return repo_root
    for candidate in candidate_repo_roots(repo_root):
        if repo_matches_board(candidate, slug):
            return candidate
    return repo_root if repo_root and repo_root.exists() else None


def goal_summary(
    project_md: dict | None,
    handoff: dict | None,
    status_md: dict | None,
    board_name: str | None = None,
    board_desc: str | None = None,
) -> str:
    project_text = str((project_md or {}).get("content") or "")
    for line in project_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if text and not text.startswith("#"):
            return text[:220]
    handoff_summary = str((handoff or {}).get("goal_summary") or "").strip()
    if handoff_summary:
        return handoff_summary[:220]
    description = str(board_desc or "").strip()
    if description:
        return description[:220]
    status_text = str((status_md or {}).get("content") or "")
    for line in status_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if text and not text.startswith("#"):
            return text[:220]
    return str(board_name or "Project OS").strip()[:220]


def onboarding_context(
    repo_root: Path,
    project_md: dict | None,
    plan_md: dict | None,
    status_md: dict | None,
) -> dict:
    project_text = str((project_md or {}).get("content") or "")
    plan_text = str((plan_md or {}).get("content") or "")
    status_text = str((status_md or {}).get("content") or "")
    merged = "\n".join([project_text, plan_text, status_text])
    is_non_git_workspace = not (repo_root / ".git").exists()
    has_boundary_hold = "TO_BE_VALIDATED_BY_HERMES" in merged
    child_repo_blocked = (
        "auto-promoted" in merged
        or "auto-adopted" in merged
        or "auto-adoption | `금지`" in merged
        or "자동 승격 금지" in merged
        or "canonical repo continuity로 승격하지 않습니다" in merged
    )
    workspace_root_confirmed = str(repo_root) in merged
    if not (is_non_git_workspace and (project_text or plan_text or status_text)):
        return {"active": False, "doc_source": "project-os"}
    status_label = "보류(안전)" if has_boundary_hold else "확인됨"
    summary = (
        "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 "
        "자동 승격은 금지됩니다."
    )
    if has_boundary_hold:
        summary = (
            "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 "
            "TO_BE_VALIDATED_BY_HERMES 상태를 유지합니다."
        )
    return {
        "active": True,
        "doc_source": "root",
        "status_label": status_label,
        "summary": summary,
        "next_safe_action": "workspace-root 기준으로 경계만 좁게 검증",
        "workspace_root_confirmed": workspace_root_confirmed,
        "repo_boundary_status": (
            "TO_BE_VALIDATED_BY_HERMES" if has_boundary_hold else "confirmed"
        ),
        "child_repo_auto_promotion_blocked": bool(child_repo_blocked),
        "guardrails": [
            (
                "workspace root 확인됨"
                if workspace_root_confirmed
                else "workspace root 확인 필요"
            ),
            (
                "child repo 자동 승격 금지 유지"
                if child_repo_blocked
                else "child repo guardrail 확인 필요"
            ),
            (
                "repo boundary 미확정 유지"
                if has_boundary_hold
                else "repo boundary confirmed"
            ),
        ],
    }


def _dashboard_documents(repo_root: Path) -> dict:
    return {
        "handoff": workspace_json(repo_root, ".ax/handoff/current.json"),
        "active": workspace_json(repo_root, ".ax/status/active.json"),
        "heartbeat": workspace_json(repo_root, ".ax/status/heartbeat.json"),
        "project": workspace_read(repo_root, "docs/project-os/PROJECT.md"),
        "plan": workspace_read(repo_root, "docs/project-os/PLAN.md"),
        "status": workspace_read(repo_root, "docs/project-os/STATUS.md"),
        "blocker": workspace_read(repo_root, "docs/project-os/BLOCKER-RESOLVER.md"),
        "root_project": workspace_read(repo_root, "PROJECT.md"),
        "root_plan": workspace_read(repo_root, "PLAN.md"),
        "root_status": workspace_read(repo_root, "STATUS.md"),
    }


def handle_dashboard(handler, parsed) -> bool:
    query = parse_qs(parsed.query or "")
    requested_board = str((query.get("board") or [""])[0] or "").strip()
    workspace_raw = str(get_last_workspace() or "").strip()
    repo_root = Path(workspace_raw).expanduser() if workspace_raw else None
    selected_board_meta = None
    if requested_board:
        try:
            from api.kanban import get_backend, serialize_board_metadata

            backend = get_backend()
            for metadata in backend.list_boards(include_archived=True) or []:
                board = serialize_board_metadata(metadata)
                if str(board.get("slug") or "") == requested_board:
                    selected_board_meta = board
                    workdir = str(board.get("default_workdir") or "").strip()
                    if workdir:
                        candidate = Path(workdir).expanduser()
                        if candidate.exists():
                            repo_root = candidate
                    break
        except Exception:
            selected_board_meta = None
    repo_root = resolve_repo_root_for_board(repo_root, requested_board)
    if not repo_root or not repo_root.exists():
        return (
            j(
                handler,
                {
                    "workspace": None,
                    "repo_root": None,
                    "git": None,
                    "docs": {},
                    "handoff": None,
                    "active": None,
                    "heartbeat": None,
                    "goal_summary": "",
                },
            )
            or True
        )

    documents = _dashboard_documents(repo_root)
    onboarding = onboarding_context(
        repo_root,
        documents["root_project"],
        documents["root_plan"],
        documents["root_status"],
    )
    if onboarding.get("active"):
        documents["project"] = documents["root_project"] or documents["project"]
        documents["plan"] = documents["root_plan"] or documents["plan"]
        documents["status"] = documents["root_status"] or documents["status"]

    active = documents["active"]
    if isinstance(active, dict):
        active_repo_root = str(active.get("repo_root") or "").strip()
        if active_repo_root:
            candidate = Path(active_repo_root).expanduser()
            if candidate.exists():
                try:
                    candidate = candidate.resolve()
                except Exception:
                    pass
                if candidate != repo_root:
                    repo_root = candidate
                    documents = _dashboard_documents(repo_root)
                    onboarding = onboarding_context(
                        repo_root,
                        documents["root_project"],
                        documents["root_plan"],
                        documents["root_status"],
                    )
                    if onboarding.get("active"):
                        documents["project"] = (
                            documents["root_project"] or documents["project"]
                        )
                        documents["plan"] = documents["root_plan"] or documents["plan"]
                        documents["status"] = (
                            documents["root_status"] or documents["status"]
                        )

    try:
        git = git_info_for_workspace(repo_root)
    except Exception:
        git = None

    board_name = None
    board_desc = None
    handoff = documents["handoff"]
    if isinstance(handoff, dict):
        raw_board = handoff.get("board")
        board = raw_board if isinstance(raw_board, dict) else {}
        board_name = board.get("display_name") or board.get("name") or board.get("slug")
        board_desc = handoff.get("goal_summary") or board.get("repo_corroboration")
    if selected_board_meta:
        board_name = (
            board_name
            or selected_board_meta.get("name")
            or selected_board_meta.get("slug")
        )
        board_desc = board_desc or selected_board_meta.get("description")

    return (
        j(
            handler,
            {
                "workspace": str(repo_root),
                "repo_root": str(repo_root),
                "selected_board_slug": requested_board
                or (selected_board_meta or {}).get("slug"),
                "git": git,
                "docs": {
                    "project": documents["project"],
                    "plan": documents["plan"],
                    "status": documents["status"],
                    "blocker_resolver": documents["blocker"],
                },
                "handoff": handoff,
                "active": documents["active"],
                "heartbeat": documents["heartbeat"],
                "onboarding": onboarding,
                "goal_summary": goal_summary(
                    documents["project"],
                    handoff,
                    documents["status"],
                    board_name,
                    board_desc,
                ),
            },
        )
        or True
    )


__all__ = (
    "candidate_repo_roots",
    "goal_summary",
    "handle_dashboard",
    "onboarding_context",
    "repo_matches_board",
    "resolve_repo_root_for_board",
    "truth_board_slugs",
    "workspace_json",
    "workspace_read",
)
