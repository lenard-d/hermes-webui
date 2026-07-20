"""Source-lock for the #5177 keep-stale-until-loaded path.

Symptom this guards against: after a hidden interval during which new messages
were persisted to the active session (post-turn bg-review writes, sibling-tab
writes, the just-finished main turn writing), switching the tab back caused the
ENTIRE transcript to visibly blank for the round-trip and then reappear —
"对话突然消失，重刷才回来". (nesquena/hermes-webui#5177)

Root cause: ``refreshActiveSessionIfExternallyUpdated('visible' | 'focus')`` hit
``remoteCount !== localCount`` and called ``loadSession(sid, {force:true})``,
which synchronously did ``S.messages = []`` before awaiting the metadata +
messages fetches. The inline comment at sessions.js itself warns about exactly
this "disappear/reappear" tradeoff but only short-circuits the metadata-only
(``remoteCount === localCount``) branch (the #5061 fix). #5122 covers a
different path (SSE error mid-stream with ``ready_state=2``) and does not apply
when the SSE error arrives while ``visibility_state='hidden'`` and bottoms out
through ``_deferStreamErrorIfPageHidden``.

Fix: ``refreshActiveSessionIfExternallyUpdated`` passes a new
``keepStaleUntilLoaded`` option to ``loadSession`` for the visibility/focus
recovery reasons. ``loadSession`` honors it by skipping the synchronous
``S.messages = []`` block when ``sameSessionForceReload`` is true, and forces
``_ensureMessagesLoaded`` to bypass its "messages already populated"
early-return so the new transcript is fetched and SWAPPED into ``S.messages``
in a single render frame. The visible result is old DOM → new DOM in one frame
with no intervening empty render.

These are static source assertions (whitespace-stripped substring + simple
brace-matching) so the keep-stale shape and the
recovery-reason → keepStaleUntilLoaded plumbing cannot silently regress.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "static" / "modules" / "sessions"


def _owner_source(name: str) -> str:
    return (SESSIONS_DIR / name).read_text(encoding="utf-8")


LOAD_SESSION_JS = _owner_source("existing-session-load.js")
LOAD_RECOVERY_JS = _owner_source("session-load-recovery.js")
TRANSCRIPT_LOADING_JS = _owner_source("transcript-loading.js")
SESSION_LIST_REFRESH_JS = _owner_source("session-list-refresh.js")


def _compact(text: str) -> str:
    return "".join(text.split())


def _function_block(source: str, marker: str, label: str) -> str:
    compact = _compact(source)
    start = compact.find(marker)
    assert start != -1, f"expected the {label} definition"
    i = compact.find("{", start)
    assert i != -1, f"{label} opening brace not found"
    depth = 0
    for j in range(i, len(compact)):
        c = compact[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return compact[start : j + 1]
    raise AssertionError(f"{label} braces did not balance")


def _load_session_block() -> str:
    """Slice from the start of ``async function loadSession`` to the matching
    close brace, using brace counting so future code growth in the function
    cannot push assertions out of a fixed window."""
    return _function_block(
        LOAD_SESSION_JS,
        "asyncfunctionloadSession(sid){",
        "loadSession",
    )


def _refresh_block() -> str:
    """Slice from the start of ``async function refreshActiveSessionIfExternallyUpdated``
    to its matching close brace."""
    return _function_block(
        SESSION_LIST_REFRESH_JS,
        "asyncfunctionrefreshActiveSessionIfExternallyUpdated(reason){",
        "refreshActiveSessionIfExternallyUpdated",
    )


def _restore_loaded_session_block() -> str:
    return _function_block(
        LOAD_RECOVERY_JS,
        "asyncfunction_restoreLoadedSession(ctx){",
        "_restoreLoadedSession",
    )


def _ensure_messages_loaded_block() -> str:
    compact = _compact(TRANSCRIPT_LOADING_JS)
    marker = "asyncfunction_ensureMessagesLoaded(sid,opts){"
    if marker not in compact:
        marker = "asyncfunction_ensureMessagesLoaded(sid){"
    return _function_block(TRANSCRIPT_LOADING_JS, marker, "_ensureMessagesLoaded")


def test_keep_stale_until_loaded_flag_computed_in_loadsession():
    # The flag must be computed by AND-ing the caller's opts with
    # sameSessionForceReload — cross-session switches MUST keep clearing
    # synchronously, otherwise a stale prior-session transcript stays on
    # screen during the navigation.
    block = _load_session_block()
    assert "const_keepStaleUntilLoaded=!!opts.keepStaleUntilLoaded&&sameSessionForceReload;" in block, (
        "loadSession must AND opts.keepStaleUntilLoaded with sameSessionForceReload"
    )


def test_loadsession_skips_synchronous_clear_when_keep_stale_until_loaded():
    # Inside the (currentSid !== sid || forceReload) block, the four-line clear
    # (S.messages=[], S.toolCalls=[], plus transcript-window state reset)
    # must sit inside `if (!_keepStaleUntilLoaded) { ... }`.
    block = _load_session_block()
    guard_idx = block.find("if(!_keepStaleUntilLoaded){")
    assert guard_idx != -1, (
        "expected the keep-stale guard wrapping the synchronous clear"
    )
    # The four clear lines must appear inside that guarded scope (between the
    # guard's opening { and a `}` of matching depth).
    body = block[guard_idx:]
    # Limit the slice to the first matching close-brace by depth-counting.
    depth = 0
    guard_body_end = -1
    for j, c in enumerate(body):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                guard_body_end = j + 1
                break
    assert guard_body_end != -1, "guard scope did not close"
    guard_body = body[:guard_body_end]
    assert "S.messages=[];" in guard_body
    assert "S.toolCalls=[];" in guard_body
    assert "transcriptWindowState.messagesTruncated=false;" in guard_body
    assert "transcriptWindowState.oldestIdx=0;" in guard_body


def test_only_one_synchronous_messages_clear_in_loadsession_force_block():
    # Guard against a re-introduced unguarded clear sneaking back in. There
    # must be exactly ONE `S.messages=[];` site in loadSession, and it must be
    # the one nested inside the if(!_keepStaleUntilLoaded) guard above.
    block = _load_session_block()
    assert block.count("S.messages=[];") == 1, (
        "loadSession should clear S.messages exactly once, under the keep-stale guard"
    )


def test_ensure_messages_loaded_called_with_keep_stale_flag():
    # Both _ensureMessagesLoaded call sites inside the extracted restore owner must forward
    # the keep-stale flag so the early-return inside _ensureMessagesLoaded
    # cannot skip the swap when stale messages are still in place.
    block = _restore_loaded_session_block()
    # Both INFLIGHT and idle paths.
    assert block.count("await_ensureMessagesLoaded(sid,{force:_keepStaleUntilLoaded,loadGeneration:_loadGeneration})") == 2


def test_ensure_messages_loaded_supports_force_override():
    # The receiving end: _ensureMessagesLoaded must look at opts.force in its
    # "messages already populated" early-return so the keep-stale flag
    # actually does what it says.
    # Accept the function signature with a named `opts` parameter
    # (preferred — self-documenting and strict-mode-optimization-friendly,
    # greptile P2 r3393… on #5189) and the historical `arguments[1]` shape
    # (for callers preserving an existing public signature). The required
    # invariant is the EARLY-RETURN being gated on opts.force.
    region = _ensure_messages_loaded_block()
    # Either explicit named param `opts` (preferred), or arguments[1] fallback,
    # both must coerce to an object so opts.force is safe to read.
    assert (
        "opts=opts||{};" in region
        or "constopts=arguments[1]||{};" in region
    ), "_ensureMessagesLoaded must coerce opts to an object before reading opts.force"
    # The early-return MUST be GATED on !opts.force.
    assert "if(!opts.force&&S.messages&&S.messages.length>0" in region, (
        "_ensureMessagesLoaded's early-return must short-circuit on opts.force"
    )


def test_refresh_visibility_path_requests_keep_stale_until_loaded():
    # The visibility-recovery callers must opt INTO keepStaleUntilLoaded; the
    # post-stream idle reconcile and the poll path stay on the original
    # destructive reload behaviour (per the design note in the patch comment).
    block = _refresh_block()
    # The recovery-reason map MUST include 'visible' and 'focus' and EXCLUDE
    # 'poll' / 'idle-reconcile'. Strip JS-side whitespace via _compact above.
    assert "const_recoveryReasons={visible:true,focus:true};" in block, (
        "expected the visibility/focus recovery-reason map"
    )
    assert "const_keepStaleUntilLoaded=!!_recoveryReasons[String(reason||'')];" in block
    # The reloaded-path loadSession call must forward the flag — there is
    # exactly one loadSession call in this block (the reloaded branch) and it
    # must pass keepStaleUntilLoaded.
    assert (
        "awaitloadSession(sid,{force:true,externalRefreshReason:reason||'poll',keepStaleUntilLoaded:_keepStaleUntilLoaded});"
        in block
    )


def test_poll_and_idle_reconcile_do_not_enable_keep_stale():
    # Belt-and-suspenders: the recovery-reason map must not list 'poll' or
    # 'idle-reconcile'. We assert on the LITERAL set definition so any
    # accidental widening is caught by the source lock.
    block = _refresh_block()
    # Just the two keys, exact set.
    assert "const_recoveryReasons={visible:true,focus:true};" in block
    # Sanity: those exact reason strings must still be the ones the
    # visibility/focus listeners use elsewhere in this file. (Failure here
    # means a renaming broke our routing.)
    compact = _compact(SESSION_LIST_REFRESH_JS)
    assert "refreshActiveSessionIfExternallyUpdated('visible')" in compact
    assert "refreshActiveSessionIfExternallyUpdated('focus')" in compact
