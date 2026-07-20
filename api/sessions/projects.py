"""Session project metadata and imported-session materialization."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid

from api.config import PROJECTS_FILE, SESSION_INDEX_FILE
from api.workspace import get_last_workspace
from .sources import import_source_metadata
from .records import (
    Session,
    _clear_webui_deleted_session_tombstone,
    _clear_webui_zero_message_orphan_tombstone,
)

logger = logging.getLogger(__name__)

def _strip_attached_files_marker(text: str) -> str:
    return re.sub(r"\n\n\[Attached files: [^\]]+\]$", "", str(text or "")).strip()


def title_from(messages, fallback: str='Untitled'):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if c is None:
                continue
            if isinstance(c, list):
                c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
            text = _strip_attached_files_marker(str(c))
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

_PROJECTS_MIGRATION_LOCK = threading.Lock()


_projects_migrated = False


def _backfill_project_profiles_if_needed(projects: list) -> bool:
    """Tag any legacy untagged projects (`profile` missing) with a sensible default.

    Strategy:
      1. For each untagged project, look at the sessions assigned to it via
         the session index. If any session carries a profile, take that
         profile.  Most installs are single-profile so this picks up the
         right answer for everyone.
      2. Otherwise default to 'default'.

    Returns True if any project was mutated. Safe to call repeatedly — once
    every project is tagged, this is a no-op. Runs at most once per process
    (cached via the module-level _projects_migrated flag) but the result is
    persisted so it's a one-time write.
    """
    untagged = [p for p in projects if not p.get('profile')]
    if not untagged:
        return False

    # Build session_id -> profile map for the untagged project_ids.
    session_profile_by_project: dict[str, str] = {}
    if SESSION_INDEX_FILE.exists():
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
            untagged_ids = {p['project_id'] for p in untagged if p.get('project_id')}
            for e in entries:
                pid = e.get('project_id')
                if pid in untagged_ids and e.get('profile'):
                    # First session profile wins for the project.
                    session_profile_by_project.setdefault(pid, e['profile'])
        except Exception:
            logger.debug("Failed to read session index for project profile backfill")

    mutated = False
    for p in untagged:
        inferred = session_profile_by_project.get(p.get('project_id'), 'default')
        p['profile'] = inferred
        mutated = True
    return mutated


def load_projects(*, _migrate: bool = True) -> list:
    """Load project list from disk. Returns list of project dicts.

    On first call, runs a one-time migration to back-fill the `profile` field
    on legacy untagged projects (#1614). Disable via `_migrate=False` for
    callsites that want the raw on-disk shape (test fixtures, e.g.).
    """
    global _projects_migrated
    if not PROJECTS_FILE.exists():
        return []
    try:
        projects = json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    if _migrate and not _projects_migrated:
        with _PROJECTS_MIGRATION_LOCK:
            # Re-check inside the lock — another thread may have raced.
            if _projects_migrated:
                # Per Opus advisor on stage-293: another thread completed
                # migration and wrote new state to disk while we waited for
                # the lock. Our `projects` snapshot is the pre-migration
                # version; re-read so the caller doesn't see stale untagged
                # rows (which a mutation route could then write back,
                # silently overwriting the migration).
                try:
                    return json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
                except Exception:
                    return projects
            if _backfill_project_profiles_if_needed(projects):
                try:
                    save_projects(projects)
                    _projects_migrated = True
                except Exception:
                    logger.debug("Failed to persist project profile backfill")
                    # Leave _projects_migrated False so a future call retries.
            else:
                # Nothing to migrate — already tagged.
                _projects_migrated = True
    return projects

def save_projects(projects) -> None:
    """Write project list to disk."""
    PROJECTS_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding='utf-8')


CRON_PROJECT_NAME = 'Cron Jobs'
_CRON_PROJECT_LOCK = threading.Lock()


def ensure_cron_project(create: bool = True) -> str | None:
    """Return the project_id of the system "Cron Jobs" project for the active profile.

    Each profile gets its own "Cron Jobs" project so cron-spawned sessions in
    profile A don't surface under the cron chip of profile B (#1614). Lookup
    keys on (name, profile) — a legacy untagged "Cron Jobs" project (no
    `profile` field) is treated as belonging to whichever profile first calls
    this in a given install, then re-tagged.

    When `create` is False, only an EXISTING per-profile cron project is
    resolved (exact tag, renamed-root alias, or legacy-untagged back-tag);
    no new project is minted and None is returned instead. Callers gate
    `create` on `_profile_has_user_projects()` so cron sessions don't force
    a "Cron Jobs" chip onto installs that never opted into project
    organization (#5379). Direct callers that omit `create` keep today's
    unconditional-create behavior.

    Thread-safe and idempotent.  Returns a 12-char hex project_id string, or
    None if `create` is False and no existing cron project resolves.
    """
    from api.profiles import get_active_profile_name, is_root_profile

    active = get_active_profile_name() or 'default'
    with _CRON_PROJECT_LOCK:
        projects = load_projects()
        # Look for an existing per-profile cron project. Match either an exact
        # profile tag or the renamed-root alias (a 'default'-tagged project
        # under a renamed root, or a renamed-root-tagged project under
        # 'default'). _is_root_profile is the canonical alias check.
        for p in projects:
            if p.get('name') != CRON_PROJECT_NAME:
                continue
            row_profile = p.get('profile')
            if row_profile == active:
                return p['project_id']
            if is_root_profile(row_profile or 'default') and is_root_profile(active):
                return p['project_id']
        # Reuse a legacy untagged cron project — back-tag it to the active profile.
        for p in projects:
            if p.get('name') == CRON_PROJECT_NAME and not p.get('profile'):
                p['profile'] = active
                save_projects(projects)
                return p['project_id']
        if not create:
            return None
        # Otherwise create a new one tagged with the active profile.
        project_id = uuid.uuid4().hex[:12]
        projects.append({
            'project_id': project_id,
            'name': CRON_PROJECT_NAME,
            'color': '#6366f1',
            'profile': active,
            'created_at': time.time(),
        })
        save_projects(projects)
        return project_id


WEBHOOK_PROJECT_NAME = 'Webhooks'
_WEBHOOK_PROJECT_LOCK = threading.Lock()


def ensure_webhook_project() -> str:
    """Return the project_id of the system "Webhooks" project for the active profile."""
    from api.profiles import get_active_profile_name, is_root_profile

    active = get_active_profile_name() or 'default'
    with _WEBHOOK_PROJECT_LOCK:
        projects = load_projects()
        for p in projects:
            if p.get('name') != WEBHOOK_PROJECT_NAME:
                continue
            row_profile = p.get('profile')
            if row_profile == active:
                return p['project_id']
            if is_root_profile(row_profile or 'default') and is_root_profile(active):
                return p['project_id']
        for p in projects:
            if p.get('name') == WEBHOOK_PROJECT_NAME and not p.get('profile'):
                p['profile'] = active
                save_projects(projects)
                return p['project_id']
        project_id = uuid.uuid4().hex[:12]
        projects.append({
            'project_id': project_id,
            'name': WEBHOOK_PROJECT_NAME,
            'color': '#0ea5e9',
            'profile': active,
            'created_at': time.time(),
        })
        save_projects(projects)
        return project_id


def _profile_has_user_projects() -> bool:
    """True if the active profile already has at least one real (non-system) project.

    "Opted into project organization" means `load_projects()` contains a
    project whose name is not a reserved system name (`CRON_PROJECT_NAME`,
    `WEBHOOK_PROJECT_NAME`), tagged to the active profile or its renamed-root
    alias. Profile/alias matching mirrors `ensure_cron_project`'s own lookup
    so the two never disagree about which profile a project belongs to.

    Read-only: never mutates projects.json, safe to call as often as needed.
    """
    from api.profiles import get_active_profile_name, is_root_profile

    active = get_active_profile_name() or 'default'
    reserved = {CRON_PROJECT_NAME, WEBHOOK_PROJECT_NAME}
    for p in load_projects():
        if p.get('name') in reserved:
            continue
        row_profile = p.get('profile')
        if row_profile == active:
            return True
        if is_root_profile(row_profile or 'default') and is_root_profile(active):
            return True
    return False


def is_cron_session(session_id: str, source_tag: str | None = None) -> bool:
    """Return True if a session originates from a cron job."""
    if source_tag == 'cron':
        return True
    sid = str(session_id or '')
    return sid.startswith('cron_')


def is_webhook_session(session_id: str, source_tag: str | None = None) -> bool:
    """Return True if a session originates from a webhook route."""
    return str(source_tag or '').strip().lower() == 'webhook'



def import_cli_session(
    session_id: str,
    title: str,
    messages,
    model: str='unknown',
    profile=None,
    created_at=None,
    updated_at=None,
    parent_session_id=None,
    source_metadata: dict | None = None,
):
    """Create a new WebUI session populated with CLI/agent messages.

    Preserve parent_session_id from state.db so imported continuation segments
    keep their lineage in the WebUI store and sidebar instead of reappearing as
    detached orphan chats.
    """
    allowed_source_fields = import_source_metadata(source_metadata)
    s = Session(
        session_id=session_id,
        title=title,
        workspace=get_last_workspace(),
        model=model,
        messages=messages,
        profile=profile,
        created_at=created_at,
        updated_at=updated_at,
        parent_session_id=parent_session_id,
        **allowed_source_fields,
    )
    # #4985: import_cli_session uses an explicit sid (the CLI sidecar's id).
    # If that sid was previously tombstoned as a webui zero-message orphan,
    # clear the tombstone entry so the freshly-imported session is visible
    # on the next poll. Wrapped because a tombstone failure must never block
    # an import.
    try:
        _clear_webui_zero_message_orphan_tombstone(s.session_id)
        _clear_webui_deleted_session_tombstone(s.session_id)
    except Exception:
        logger.debug(
            "Failed to clear webui tombstone for %s",
            s.session_id,
            exc_info=True,
        )
    s.save(touch_updated_at=False)
    return s


# ── CLI session bridge ──────────────────────────────────────────────────────
