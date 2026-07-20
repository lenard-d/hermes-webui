"""Kanban display configuration reads and persistence."""

from __future__ import annotations

from .integration import _conn, _kb
from .validation import BOARD_COLUMNS


def _config_payload(*, board=None):
    """Return kanban configuration: column names, known assignees, and lane/display settings from hermes_cli.config."""
    kb = _kb()
    try:
        with _conn(board=board) as conn:
            try:
                assignees = list(kb.known_assignees(conn))
            except Exception:
                assignees = []
    except Exception:
        assignees = []
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    k_cfg = (cfg.get("dashboard") or {}).get("kanban") or {}
    return {
        "columns": BOARD_COLUMNS,
        "assignees": assignees,
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(
            k_cfg.get("include_archived_by_default", False)
        ),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
        "read_only": False,
    }


def _update_config_payload(body):
    if not isinstance(body, dict):
        raise ValueError("JSON object body required")
    if "lane_by_profile" not in body:
        raise ValueError("lane_by_profile is required")
    if not isinstance(body.get("lane_by_profile"), bool):
        raise ValueError("lane_by_profile must be boolean")

    from api import config

    config_path = config._get_config_path()
    with config._cfg_lock:
        config_data = config._load_yaml_config_file(config_path)
        dashboard_cfg = config_data.get("dashboard")
        if not isinstance(dashboard_cfg, dict):
            dashboard_cfg = {}
        kanban_cfg = dashboard_cfg.get("kanban")
        if not isinstance(kanban_cfg, dict):
            kanban_cfg = {}
        kanban_cfg["lane_by_profile"] = body["lane_by_profile"]
        dashboard_cfg["kanban"] = kanban_cfg
        config_data["dashboard"] = dashboard_cfg
        config._save_yaml_config_file(config_path, config_data)
    config.reload_config()
    payload = _config_payload()
    payload["lane_by_profile"] = body["lane_by_profile"]
    return payload
