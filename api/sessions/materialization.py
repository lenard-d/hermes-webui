"""Foreign-session resolution and safe WebUI materialization."""

from __future__ import annotations

import json
import logging
import sqlite3 as _sqlite
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from api.profiles import _profiles_match
from api.workspace import get_last_workspace
from .cache import cache_full_session, get_session
from .external_sidebar import get_cli_sessions
from .gateway_identity import gateway_session_identity
from .reconciliation import get_cli_session_messages
from .records import (
    DEFAULT_WORKSPACE,
    SESSION_INDEX_FILE,
    Session,
    _load_webui_deleted_session_tombstone,
    _write_session_index,
    is_safe_session_id,
)
from .sources import is_messaging_session_record as _is_messaging_session_record
from .state_db import _active_state_db_path

logger = logging.getLogger(__name__)

def _lookup_gateway_session_identity(session_id: str) -> dict:
    return gateway_session_identity(session_id)


def _lookup_cli_session_metadata(session_id: str, *, all_profiles: bool = False) -> dict:
    if not session_id:
        return {}
    try:
        for row in get_cli_sessions(all_profiles=all_profiles):
            if row.get("session_id") == session_id:
                return row
    except Exception:
        return {}
    return {}


def _session_index_marks_was_webui(sid: str) -> bool:
    """Return True iff ``sid`` is in the WebUI session index as a WebUI- or
    fork-origin row whose sidecar is now gone.

    The WebUI session index (``SESSION_INDEX_FILE``) is the canonical registry
    of sessions the WebUI ever owned. A row there tagged with ``webui`` or
    ``fork`` means the session once had a sidecar that has since been deleted
    (or never materialised on this profile). Returning 404 to the client on
    these ids is what lets the browser self-heal: strip the stale
    ``/session/<id>`` URL and clear localStorage instead of silently
    re-attaching to a now-empty session (#2782).

    Foreign-origin rows (CLI, TUI, Desktop, claude_code, gateway, telegram,
    etc.) — those with explicit non-webui source tags, OR blank sources with
    ``is_cli_session``/``read_only`` markers — are NOT treated as deleted
    WebUI sessions, even when the sidecar is absent.
    """
    if not SESSION_INDEX_FILE.exists():
        return False
    try:
        entries = json.loads(SESSION_INDEX_FILE.read_bytes())
    except Exception:
        return False
    for entry in entries if isinstance(entries, list) else []:
        if entry.get("session_id") != sid:
            continue
        # Classify per source field, not on a collapsed `a or b or c` — a
        # legacy CLI/imported row can carry is_cli_session:true with BLANK
        # source fields, and collapsing-then-defaulting-to-WebUI would wrongly
        # 404 it (it should keep its read-only CLI stub).
        srcs = [
            str(entry.get("source_tag") or "").strip().lower(),
            str(entry.get("raw_source") or "").strip().lower(),
            str(entry.get("session_source") or "").strip().lower(),
        ]
        explicit = [s for s in srcs if s]
        if any(s in ("webui", "fork") for s in explicit):
            # Explicit WebUI-origin (incl. forks, which /api/session/branch
            # stamps session_source="fork") — a deleted sidecar bricks
            # identically. 404.
            return True
        if explicit:
            # Explicit non-WebUI source (cli, telegram, claude_code, ...) —
            # genuine foreign session, keep the existing CLI/read-only stub.
            return False
        # All source fields blank: WebUI-origin UNLESS the row is a legacy
        # CLI/imported session marked only by is_cli_session / read_only.
        is_cli = entry.get("is_cli_session") is True
        is_read_only = bool(entry.get("read_only") or entry.get("is_read_only"))
        return not (is_cli or is_read_only)
    return False


def _session_deleted_tombstone_marks_was_webui(sid: str) -> bool:
    try:
        return sid in _load_webui_deleted_session_tombstone()
    except Exception:
        return False


def _state_db_session_source(sid: str) -> str:
    """Return the lowercased ``sessions.source`` for ``sid`` from state.db.

    Cheap single-row lookup used to distinguish delegated ``subagent`` children
    (which have a recoverable state.db transcript) from genuinely-deleted WebUI
    sessions.  Returns "" on any error / missing row so callers fall back to
    their existing behaviour.
    """
    if not sid or not is_safe_session_id(sid):
        return ""
    try:
        db_path = _active_state_db_path()
        if not db_path or not Path(db_path).exists():
            return ""
        with closing(_sqlite.connect(str(db_path))) as _conn:
            row = _conn.execute(
                "SELECT source FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    return str(row[0] or "").strip().lower()


def _is_subagent_child_session_id(sid: str) -> bool:
    """Return True when ``sid`` is a delegated subagent child in state.db.

    Delegated ``delegate_task`` children are recorded in Hermes state.db with
    ``source='subagent'`` and a ``parent_session_id``. They frequently have no
    WebUI sidecar (they ran server-side), so opening one from the sidebar must
    recover the transcript from state.db rather than 404 as a deleted WebUI
    session (#5307).
    """
    return _state_db_session_source(sid) == "subagent"


def _session_is_subagent_view_only(sid: str) -> bool:
    """Return True when ``sid`` is a delegated subagent child by ANY signal —
    state.db source OR a persisted WebUI sidecar tagged subagent.

    Delegated children are view-only and owned by the delegate runner. Direct
    transcript/metadata mutation routes (delete / truncate / clear / pin /
    rename / move) must refuse them so a stray WebUI action can't delete or
    fork the child's state.db transcript (#5307). This is the shared
    defense-in-depth guard for routes that bypass
    ``_get_or_materialize_session()``.
    """
    if _is_subagent_child_session_id(sid):
        return True
    try:
        s = get_session(sid)
    except Exception:
        return False
    src = (
        str(getattr(s, "source_tag", "") or getattr(s, "raw_source", "")
            or getattr(s, "session_source", "") or "").strip().lower()
    )
    return src == "subagent"


def _is_claimable_cli_source(cli_meta: dict, state_db_source: str = "") -> tuple[bool, str]:
    """Decide whether a foreign-origin session is safe to claim writeable
    in WebUI. Returns ``(claimable, reason_if_not)``.

    Policy mirrors ``/api/session/import_cli``:
    sessions explicitly marked ``read_only`` in their foreign store are
    surfaced as read-only stubs but never materialised as writable
    WebUI sidecars. We extend that with a denylist of foreign-source
    families whose ownership belongs to a non-WebUI process
    (messaging channels, external agents, Claude Code, scheduled
    cron runs, and platformless gateway fallbacks), so a WebUI POST
    cannot accidentally turn them into writable sidecars and violate
    their ownership boundary (#4911 review + the residual
    gateway/unknown gap flagged in the follow-up + the cron-claim
    policy flagged in the Greptile 4/5 review).

    The check is denylist-based: if a source is in any of the
    refused families below, it is non-claimable. Everything else
    (CLI, TUI, Desktop, plus future local agent sources) is allowed.
    TUI/Desktop sessions whose cli_meta is empty (they don't appear
    in ``get_cli_sessions()`` due to the CLI cap) fall through to
    ``state_db_source``; state.db has a ``source`` column with values
    like ``tui``, ``desktop``, ``cli``, ``cron``, ``claude_code``,
    ``messaging``, ``external_agent``, ``gateway`` (platform-tagged
    gateways land in ``_MESSAGING_RAW_SOURCES`` and are caught by
    the messaging check above; bare ``"gateway"`` / ``"unknown"``
    literals are caught here).
    """
    cm = cli_meta or {}
    if bool(cm.get("read_only")):
        return False, "explicit_readonly"
    session_source = (cm.get("session_source") or "").strip().lower()
    if session_source in {"messaging", "external_agent"}:
        return False, f"session_source={session_source}"
    # Track cli_meta-sourced and state.db-sourced source_tag values
    # separately so the diagnostic reason string never mislabels a
    # cli_meta-sourced denial as a state.db-sourced one (Greptile P2,
    # #4911 follow-up).  The reason is currently discarded by the
    # caller, but it is exported in the return tuple and may surface
    # in a future log / user-visible diagnostic.
    cli_meta_source_tag = (cm.get("source_tag") or cm.get("raw_source") or "").strip().lower()
    if cli_meta_source_tag in {"claude_code", "cron", "external_agent",
                                "gateway", "messaging", "subagent", "unknown"}:
        # gateway/unknown are the platformless gateway fallbacks
        # (gateway/run.py, gateway/slash_commands.py) — they own the
        # conversation in the gateway, not in WebUI.
        # cron sessions are scheduled and owned by the cron runner
        # process; claiming them into a writable WebUI sidecar would
        # let a stray POST break the next scheduled run.
        # messaging / external_agent can be supplied by the foreign
        # store directly in source_tag (in addition to the
        # session_source / messaging-record checks above), and they
        # need the same provenance-correct refusal.
        return False, f"cli_meta_source={cli_meta_source_tag}"
    if _is_messaging_session_record(cm):
        return False, "messaging_record"
    # Empty cli_meta is the common case for TUI/Desktop; fall through
    # to state.db's source column.  Refuse known-foreign state.db sources.
    if not cli_meta_source_tag and state_db_source:
        state_db_source_tag = state_db_source.strip().lower()
        if state_db_source_tag in {"claude_code", "cron", "messaging",
                                    "external_agent", "gateway", "subagent", "unknown"}:
            return False, f"state_db_source={state_db_source_tag}"
    return True, ""


def _claim_or_synthesize_cli_session(sid: str, cli_meta: dict = None):
    """Resolve a session_id that has no WebUI sidecar.

    Returns ``(session_or_None, reason)``. Reasons:

      ``'materialized'``
        A state.db row with messages exists AND the foreign source is
        claimable per :func:`_is_claimable_cli_source` (CLI / TUI /
        Desktop, no explicit read_only, not a messaging / claude_code
        session). ``session`` is a fully populated
        :class:`api.sessions.store.Session` ready for writeable use; the caller
        MUST call ``session.save()`` to persist a WebUI-owned sidecar
        before the first write.  The Session carries the source-tag
        metadata from the CLI/state.db lookup (``is_cli_session=True``,
        ``read_only=False``) so the sidebar still renders the original
        source badge.

      ``'not_claimable'``
        The sid has recoverable state.db messages but the foreign
        source is owned by a non-WebUI process (messaging channel,
        claude_code, external_agent, or explicit read_only). ``session``
        is still returned, but with ``read_only=True`` preserved and
        the foreign source tag intact, so the GET stub continues to
        render the original badge and the read-only banner. The POST
        path must return 403 (not 404) so the user sees a clear
        refusal instead of the empty-state self-heal that the 404
        handler triggers (#4911 review).

      ``'was_webui'``
        The sid is in the WebUI session index as a webui/fork origin row but
        its sidecar is gone.  Callers MUST return 404 so the browser clears
        its stale ``/session/<id>`` URL and localStorage instead of silently
        re-attaching to a now-empty session (#2782).

      ``'no_foreign_state'``
        The sid has no WebUI sidecar AND no recoverable messages in
        state.db.  Callers MUST return 404.

      ``'invalid_sid'``
        ``sid`` failed :func:`is_safe_session_id`.  Callers MUST return 404.

    ``cli_meta`` is an optional pass-through.  Callers that already
    computed ``_lookup_cli_session_metadata(sid)`` (e.g. the GET path
    building a sidebar dict) can pass it in to avoid the redundant
    lookup; callers without it (POST path, tests) pass nothing and the
    helper does the lookup itself.

    Closing the GET-vs-POST asymmetry for foreign-origin sessions: GET
    ``/api/session`` and POST ``/api/chat/start`` both call this helper
    on a missing-sidecar KeyError, so a TUI/Desktop/CLI session can be
    loaded read-only AND continued writeable from the WebUI.
    """
    def build_workspace(sid, cli_meta):
        """Coalesce workspace with sane fallbacks so _start_run doesn't
        trip on a missing field. state.db's cwd is the canonical workspace for
        agent sessions; CLI metadata is the fallback (handles Telegram/etc).
        """
        workspace = (cli_meta or {}).get("workspace") or (cli_meta or {}).get("cwd")
        if not workspace:
            try:
                workspace = get_last_workspace()
            except Exception:
                workspace = None
        if not workspace:
            workspace = DEFAULT_WORKSPACE or "/"
        return workspace

    def build_session(sid, cli_meta, msgs, read_only_flag, is_cli_flag=True):
        return Session(
            session_id=sid,
            title=(cli_meta or {}).get("title") or "CLI Session",
            workspace=build_workspace(sid, cli_meta),
            model=(cli_meta or {}).get("model") or "unknown",
            model_provider=(cli_meta or {}).get("model_provider"),
            messages=msgs,
            created_at=(cli_meta or {}).get("created_at") or 0,
            updated_at=(cli_meta or {}).get("updated_at") or 0,
            profile=(cli_meta or {}).get("profile"),
            # ``is_cli_flag`` is True for genuine CLI/TUI/Desktop sessions so the
            # sidebar renders the source badge and the client's external-session
            # gating applies. It is False for delegated subagent children (#5307):
            # they are recovered read-only and must NOT be CLI-classified, or they
            # would pass the frontend ``_isExternalSession`` poll-skip /
            # active-refresh gates that #3603 keeps narrow.
            is_cli_session=is_cli_flag,
            source_tag=(cli_meta or {}).get("source_tag"),
            raw_source=(cli_meta or {}).get("raw_source"),
            session_source=(cli_meta or {}).get("session_source"),
            source_label=(cli_meta or {}).get("source_label"),
            # ``read_only_flag`` is True for not_claimable sources (foreign
            # store marked them read-only / messaging / claude_code) and
            # False for genuine CLI / TUI / Desktop sessions.  Only the
            # POST claim path with a verified-claimable source can
            # actually write; the GET stub always reflects whatever the
            # helper returns so the read-only banner stays accurate.
            read_only=read_only_flag,
        )

    if not is_safe_session_id(sid):
        return None, "invalid_sid"
    if (
        (
            _session_index_marks_was_webui(sid)
            or (
                _session_deleted_tombstone_marks_was_webui(sid)
                and _state_db_session_source(sid) in ("", "webui", "fork")
            )
        )
        and not _is_subagent_child_session_id(sid)
    ):
        # A delegated subagent child (source='subagent' in state.db) can be
        # registered in the WebUI index as a webui/fork/blank-source row (it
        # shares the parent's lineage) yet have no WebUI sidecar of its own.
        # Those must recover their transcript from state.db below rather than
        # 404 as a genuinely-deleted WebUI session (#5307). Every other
        # index-marked-WebUI id keeps the #2782 self-heal 404 contract.
        #
        # The durable delete tombstone only 404s a row that is WebUI-owned
        # (source webui/fork, or blank = a stale URL with no surviving row).
        # A foreign-source row (messaging/cli/tui/desktop) that happens to
        # carry a tombstone — e.g. a WebUI delete of an imported session whose
        # state.db row the external writer later re-created — must still
        # materialize its transcript, never be self-healed to a 404 (#5504).
        return None, "was_webui"
    if cli_meta is None:
        cli_meta = _lookup_cli_session_metadata(sid) or {}
    msgs = get_cli_session_messages(sid)
    if not msgs:
        return None, "no_foreign_state"
    # TUI/Desktop sessions often have empty cli_meta (they don't appear in
    # get_cli_sessions() because of the cap).  Fall back to the state.db
    # ``source`` column to make the claim-eligibility check robust and to
    # populate the Session's source-tag metadata so the sidebar still
    # renders the correct badge for these sessions.
    state_db_source = ""
    state_db_row = None
    try:
        db_path = _active_state_db_path()
        if db_path and Path(db_path).exists():
            with closing(_sqlite.connect(str(db_path))) as _conn:
                _conn.row_factory = _sqlite.Row
                _row = _conn.execute(
                    "SELECT source, title, model, cwd, started_at, ended_at "
                    "FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
                if _row is not None:
                    state_db_row = dict(_row)
                    state_db_source = str(_row["source"] or "").strip().lower()
    except Exception:
        state_db_source = ""
    # Populate source metadata from state.db when cli_meta is empty so the
    # synthesized Session carries the right source_tag/source_label.  Only
    # fill fields that are actually missing from cli_meta; the foreign store
    # always wins when both are present.
    #
    # No-mutation contract (Greptile #4911 follow-up): the GET path passes
    # a pre-computed cli_meta dict and expects it to be unchanged after
    # this helper returns.  We use a single copy-on-write rebind at the
    # top of the block and then plain subscript assignment so the
    # caller's dict is never touched in place.
    if state_db_row:
        cli_meta = dict(cli_meta or {})
        if not cli_meta.get("source_tag") and state_db_source:
            cli_meta["source_tag"] = state_db_source
        if not cli_meta.get("raw_source") and state_db_source:
            cli_meta["raw_source"] = state_db_source
        if not cli_meta.get("title") and state_db_row.get("title"):
            cli_meta["title"] = state_db_row["title"]
        if not cli_meta.get("model") and state_db_row.get("model"):
            cli_meta["model"] = state_db_row["model"]
        if not cli_meta.get("workspace") and state_db_row.get("cwd"):
            cli_meta["workspace"] = state_db_row["cwd"]
        # Map state.db timestamps to created_at/updated_at on the
        # synthesized Session.  Without this, the first POST writes
        # epoch (0) timestamps into the permanent sidecar and the
        # sidebar sorts/dates the session as "Jan 1 1970" (Greptile
        # #4911 follow-up, P1).  created_at always comes from
        # started_at; Session.save() never touches created_at (it's
        # not in the metadata touch list), so the mapping is
        # load-bearing on both the GET stub and the POST claim path.
        # updated_at prefers ended_at (last activity) and falls back
        # to started_at — note that on the POST claim path
        # Session.save() defaults to touch_updated_at=True and stamps
        # updated_at to wall-clock now, so this value is only the
        # GET-stub display value; the claimed sidecar's updated_at
        # reflects the moment of claim (the desired "just now" UX).
        if not cli_meta.get("created_at") and state_db_row.get("started_at"):
            cli_meta["created_at"] = state_db_row["started_at"]
        if not cli_meta.get("updated_at"):
            _ended = state_db_row.get("ended_at")
            _started = state_db_row.get("started_at")
            if _ended or _started:
                cli_meta["updated_at"] = _ended or _started
    claimable, _reason = _is_claimable_cli_source(cli_meta, state_db_source)
    if not claimable:
        # The session is real and viewable, but the foreign source forbids
        # the WebUI from taking write ownership.  Build the Session with
        # readonly=True so the GET stub keeps rendering the original
        # read-only badge, and return 'not_claimable' so the POST path
        # 403s instead of bare-404ing.
        #
        # Delegated subagent children (#5307) additionally must NOT be
        # CLI-classified: they are recovered read-only for viewing, but
        # is_cli_session=True would let them pass the frontend
        # _isExternalSession poll-skip / active-refresh gates that #3603
        # keeps narrow. Every other non-claimable foreign source keeps the
        # CLI classification so its source badge renders.
        _sa_child = _is_subagent_child_session_id(sid)
        return (
            build_session(sid, cli_meta, msgs, read_only_flag=True,
                          is_cli_flag=not _sa_child),
            "not_claimable",
        )
    return build_session(sid, cli_meta, msgs, read_only_flag=False), "materialized"


def _resolve_cli_import_metadata(session_id: str, *, requested_profile=None, allow_all_profiles: bool = False) -> dict:
    cli_meta = _lookup_cli_session_metadata(session_id)
    if cli_meta and (not requested_profile or _profiles_match(cli_meta.get("profile"), requested_profile)):
        return cli_meta
    if not allow_all_profiles:
        return {}
    cli_meta = _lookup_cli_session_metadata(session_id, all_profiles=True)
    if cli_meta and requested_profile and not _profiles_match(cli_meta.get("profile"), requested_profile):
        return {}
    return cli_meta or {}


def _publish_materialized_session(session, *, persist: bool = True):
    """Persist a new session before making it reachable through the LRU cache.

    Empty branches intentionally remain memory-only until their first turn;
    callers preserve that contract with ``persist=False``. Every materialized
    session that requires durability must complete its first save before cache
    publication so a failed request cannot leave an in-memory ghost.
    """
    if persist:
        session.save(skip_index=True)
    cache_full_session(session.session_id, session)
    if persist:
        try:
            _write_session_index(updates=[session])
        except Exception:
            # The canonical sidecar and in-process cache are already committed.
            # The compact index is a repairable projection and must not turn a
            # durable endpoint success into a reported failure.
            logger.exception(
                "Failed to refresh session index after materializing %s",
                session.session_id,
            )
    return session


@dataclass(frozen=True)
class BranchSourceResolution:
    """Domain result for resolving a session that may be branched in WebUI."""

    session: Session | None
    refusal: str | None = None


def resolve_branch_source(session_id: str) -> BranchSourceResolution:
    """Resolve a branch source without leaking ownership policy to HTTP."""
    if _session_is_subagent_view_only(session_id):
        return BranchSourceResolution(None, "subagent_view_only")
    try:
        source = get_session(session_id)
    except KeyError:
        source, reason = _claim_or_synthesize_cli_session(session_id)
        if source is None:
            return BranchSourceResolution(None, "not_found")
        source_kind = str(
            getattr(source, "source_tag", None)
            or getattr(source, "raw_source", None)
            or getattr(source, "source", None)
            or ""
        ).strip().lower()
        if reason == "not_claimable" and source_kind != "cron":
            return BranchSourceResolution(None, "foreign_view_only")
        if reason == "not_claimable":
            source._branch_source_readonly = True
            return BranchSourceResolution(source)
    if getattr(source, "read_only", False):
        source_kind = str(
            getattr(source, "source_tag", None)
            or getattr(source, "raw_source", None)
            or getattr(source, "source", None)
            or ""
        ).strip().lower()
        if source_kind != "cron":
            return BranchSourceResolution(None, "foreign_view_only")
        source._branch_source_readonly = True
    return BranchSourceResolution(source)


class ForeignSessionAccess:
    """Deep interface for foreign-session ownership and materialization."""

    @staticmethod
    def metadata(session_id: str, *, all_profiles: bool = False) -> dict:
        if all_profiles:
            return _lookup_cli_session_metadata(session_id, all_profiles=True)
        return _lookup_cli_session_metadata(session_id)

    @staticmethod
    def resolve_import_metadata(
        session_id: str,
        *,
        requested_profile=None,
        allow_all_profiles: bool = False,
    ) -> dict:
        return _resolve_cli_import_metadata(
            session_id,
            requested_profile=requested_profile,
            allow_all_profiles=allow_all_profiles,
        )

    @staticmethod
    def claim(session_id: str, metadata: dict | None = None):
        return _claim_or_synthesize_cli_session(session_id, metadata)

    @staticmethod
    def is_subagent_child(session_id: str) -> bool:
        return _is_subagent_child_session_id(session_id)

    @staticmethod
    def is_view_only(session_id: str) -> bool:
        return _session_is_subagent_view_only(session_id)

    @staticmethod
    def publish(session: Session, *, persist: bool = True) -> Session:
        return _publish_materialized_session(session, persist=persist)

    @staticmethod
    def resolve_branch_source(session_id: str) -> BranchSourceResolution:
        return resolve_branch_source(session_id)


foreign_session_access = ForeignSessionAccess()
