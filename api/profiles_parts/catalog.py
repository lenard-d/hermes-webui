"""Profile catalog projection, skill statistics, and cache policy.

The mutable caches stay on :mod:`api.profiles`, the compatibility facade that
historically exposed them to tests and integrations.  Every decision resolves
that facade at call time so existing monkeypatch seams remain effective.
"""

import os
import threading
import time
from pathlib import Path

import yaml

from api.profiles_parts.facade import profiles_api


def _skills_stats_lock_for(profile_dir: Path) -> threading.Lock:
    """Return (creating if needed) the per-profile compute lock."""
    api = profiles_api()
    with api._SKILLS_STATS_LOCKS_GUARD:
        lock = api._SKILLS_STATS_LOCKS.get(profile_dir)
        if lock is None:
            lock = threading.Lock()
            api._SKILLS_STATS_LOCKS[profile_dir] = lock
        return lock


def _skill_tree_max_mtime_ns(skills_dir: Path, config_path: Path) -> int:
    """Return the max mtime across config, skill dirs, and SKILL.md files."""
    max_ns = 0
    try:
        if config_path.exists():
            max_ns = max(max_ns, config_path.stat().st_mtime_ns)
    except OSError:
        pass
    if not skills_dir.is_dir():
        return max_ns
    try:
        from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS
    except Exception:
        EXCLUDED_SKILL_DIRS = frozenset()
        SKILL_SUPPORT_DIRS = frozenset()
    try:
        # Match iter_skill_index_files: follow symlinks and prune support trees.
        for root, dirnames, filenames in os.walk(skills_dir, followlinks=True):
            root_path = Path(root)
            has_skill_md = "SKILL.md" in filenames
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in EXCLUDED_SKILL_DIRS
                and not (has_skill_md and dirname in SKILL_SUPPORT_DIRS)
            ]
            try:
                max_ns = max(max_ns, root_path.stat().st_mtime_ns)
            except OSError:
                pass
            for dirname in dirnames:
                try:
                    max_ns = max(max_ns, (root_path / dirname).stat().st_mtime_ns)
                except OSError:
                    pass
            if "SKILL.md" in filenames:
                try:
                    max_ns = max(max_ns, (root_path / "SKILL.md").stat().st_mtime_ns)
                except OSError:
                    pass
    except Exception:
        pass
    return max_ns


def _compute_profile_skills_stats(profile_dir: Path) -> tuple[int, int]:
    """Compute enabled and compatible skill counts for one profile."""
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return (0, 0)

    disabled = set()
    config_path = profile_dir / "config.yaml"
    if config_path.exists():
        try:
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                skills_cfg = cfg.get("skills")
                if isinstance(skills_cfg, dict):
                    platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(
                        "webui"
                    )
                    disabled_val = (
                        platform_disabled
                        if platform_disabled is not None
                        else skills_cfg.get("disabled")
                    )
                    if disabled_val is not None:
                        if isinstance(disabled_val, str):
                            disabled_val = [disabled_val]
                        disabled = {
                            str(value).strip()
                            for value in disabled_val
                            if str(value).strip()
                        }
        except Exception:
            pass

    from agent.skill_utils import (
        iter_skill_index_files,
        parse_frontmatter,
        skill_matches_platform,
    )

    seen_names = set()
    enabled_count = 0
    compatible_count = 0
    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        try:
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, _ = parse_frontmatter(content)
            if not skill_matches_platform(frontmatter):
                continue
            name = frontmatter.get("name", skill_md.parent.name)[:64]
            if name in seen_names:
                continue
            seen_names.add(name)
            compatible_count += 1
            if name not in disabled:
                enabled_count += 1
        except Exception:
            pass
    return (enabled_count, compatible_count)


def _get_profile_skills_stats(profile_dir: Path) -> tuple[int, int]:
    """Return skill counts through the mtime-aware, single-flight cache."""
    api = profiles_api()
    profile_dir = Path(profile_dir).resolve()
    now = time.time()
    skills_dir = profile_dir / "skills"
    config_path = profile_dir / "config.yaml"
    current_mtime_ns = api._skill_tree_max_mtime_ns(skills_dir, config_path)

    cached = api._SKILLS_STATS_CACHE.get(profile_dir)
    if cached is not None:
        enabled, compat, cached_mtime_ns, expiry = cached
        if current_mtime_ns == cached_mtime_ns and now < expiry:
            return enabled, compat

    lock = api._skills_stats_lock_for(profile_dir)
    with lock:
        cached = api._SKILLS_STATS_CACHE.get(profile_dir)
        if cached is not None:
            enabled, compat, cached_mtime_ns, expiry = cached
            if current_mtime_ns == cached_mtime_ns and time.time() < expiry:
                return enabled, compat

        new_mtime_ns = api._skill_tree_max_mtime_ns(skills_dir, config_path)
        result = api._compute_profile_skills_stats(profile_dir)
        api._SKILLS_STATS_CACHE[profile_dir] = (
            result[0],
            result[1],
            new_mtime_ns,
            time.time() + api._SKILLS_STATS_CACHE_TTL,
        )
        return result


def _invalidate_list_profiles_cache() -> None:
    """Drop the cached profile list after a catalog mutation."""
    api = profiles_api()
    with api._LIST_PROFILES_CACHE_LOCK:
        api._LIST_PROFILES_CACHE = None


def _build_profile_rows_fast() -> list | None:
    """Build WebUI profile rows while avoiding the upstream alias scan."""
    api = profiles_api()
    try:
        from hermes_cli.profiles import (
            _PROFILE_ID_RE as _UPSTREAM_PROFILE_ID_RE,
            _check_gateway_running,
            _get_default_hermes_home,
            _get_profiles_root,
            _read_config_model,
        )
    except Exception:
        return None

    def _row(home: Path, name: str, is_default: bool) -> dict:
        try:
            model, provider = _read_config_model(home)
        except Exception:
            model, provider = None, None
        try:
            gateway_running = _check_gateway_running(home)
        except Exception:
            gateway_running = False
        enabled_count, total_count = api._get_profile_skills_stats(home)
        return {
            "name": name,
            "path": str(home),
            "is_default": is_default,
            "is_active": False,
            "gateway_running": gateway_running,
            "model": model,
            "provider": provider,
            "has_env": (home / ".env").exists(),
            "visible": api._profile_visible_from_meta(home),
            "skill_count": enabled_count,
            "enabled_skills": enabled_count,
            "total_skills": total_count,
        }

    rows: list = []
    default_home = _get_default_hermes_home()
    if default_home.is_dir():
        rows.append(_row(default_home, "default", True))

    profiles_root = _get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if entry.is_dir() and _UPSTREAM_PROFILE_ID_RE.match(entry.name):
                rows.append(_row(entry, entry.name, False))
    return rows


def list_profiles_api() -> list:
    """Project all profiles into the stable WebUI response shape."""
    api = profiles_api()
    now = time.time()

    if api._is_isolated_profile_mode():
        active = api._isolated_profile_name()
        hermes_home = Path(api._INITIAL_HERMES_HOME).expanduser()
        try:
            from hermes_cli.profiles import list_profiles

            infos = list_profiles()
            for profile in infos:
                try:
                    same_home = (
                        Path(profile.path).expanduser().resolve()
                        == hermes_home.resolve()
                    )
                except OSError:
                    same_home = False
                if profile.name == active and same_home:
                    enabled_count, total_count = api._get_profile_skills_stats(
                        profile.path
                    )
                    return [{
                        "name": profile.name,
                        "path": str(profile.path),
                        "is_default": profile.is_default,
                        "is_active": True,
                        "gateway_running": profile.gateway_running,
                        "model": profile.model,
                        "provider": profile.provider,
                        "has_env": profile.has_env,
                        "visible": api._profile_visible_from_meta(profile.path),
                        "skill_count": enabled_count,
                        "enabled_skills": enabled_count,
                        "total_skills": total_count,
                    }]
        except (ImportError, OSError, PermissionError):
            pass
        enabled_count, total_count = api._get_profile_skills_stats(hermes_home)
        return [{
            "name": active,
            "path": str(hermes_home),
            "is_default": active == "default",
            "is_active": True,
            "gateway_running": False,
            "model": None,
            "provider": None,
            "has_env": (hermes_home / ".env").exists(),
            "visible": api._profile_visible_from_meta(hermes_home),
            "skill_count": enabled_count,
            "enabled_skills": enabled_count,
            "total_skills": total_count,
        }]

    with api._LIST_PROFILES_CACHE_LOCK:
        cached = api._LIST_PROFILES_CACHE
        if cached is not None and now - cached[1] < api._LIST_PROFILES_CACHE_TTL:
            rows = cached[0]
        else:
            rows = api._build_profile_rows_fast()
            if rows is not None:
                api._LIST_PROFILES_CACHE = (rows, now)

    if rows is None:
        api.logger.debug(
            "list_profiles_api: fast path unavailable, falling back to "
            "upstream list_profiles() (slower)"
        )
        try:
            from hermes_cli.profiles import list_profiles

            infos = list_profiles()
        except ImportError:
            return [api._default_profile_dict()]

        active = api.get_active_profile_name()
        result = []
        for profile in infos:
            enabled_count, total_count = api._get_profile_skills_stats(profile.path)
            result.append({
                "name": profile.name,
                "path": str(profile.path),
                "is_default": profile.is_default,
                "is_active": profile.name == active,
                "gateway_running": profile.gateway_running,
                "model": profile.model,
                "provider": profile.provider,
                "has_env": profile.has_env,
                "visible": api._profile_visible_from_meta(profile.path),
                "skill_count": enabled_count,
                "enabled_skills": enabled_count,
                "total_skills": total_count,
            })
        return result

    active = api.get_active_profile_name()
    return [{**profile, "is_active": profile["name"] == active} for profile in rows]


def _profile_visible_from_meta(profile_path: Path) -> bool:
    """Return False only for explicit ``visible: false`` profile metadata."""
    try:
        meta_path = Path(profile_path) / "profile.yaml"
        if not meta_path.exists():
            return True
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    return data.get("visible") is not False


def _default_profile_dict() -> dict:
    """Return the fallback root-profile row without hermes_cli."""
    api = profiles_api()
    enabled_count, compatible_count = api._get_profile_skills_stats(
        api._DEFAULT_HERMES_HOME
    )
    return {
        "name": "default",
        "path": str(api._DEFAULT_HERMES_HOME),
        "is_default": True,
        "is_active": True,
        "gateway_running": False,
        "model": None,
        "provider": None,
        "has_env": (api._DEFAULT_HERMES_HOME / ".env").exists(),
        "visible": True,
        "skill_count": enabled_count,
        "enabled_skills": enabled_count,
        "total_skills": compatible_count,
    }
