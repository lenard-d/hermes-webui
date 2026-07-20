"""Version, release-channel, and update-eligibility policy."""

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from api.config import REPO_ROOT as _DEFAULT_REPO_ROOT, get_agent_source_dir

_DEFAULT_AGENT_DIR = get_agent_source_dir()
from . import repository as _repository

_build_compare_url = _repository._build_compare_url
_detect_default_branch = _repository._detect_default_branch
_normalize_remote_url = _repository._normalize_remote_url
_sanitize_git_diagnostic = _repository._sanitize_git_diagnostic


def _git(args, cwd, timeout=10):
    return _repository._run_git(args, cwd, timeout=timeout)


def _repo_root() -> Path:
    return _DEFAULT_REPO_ROOT


def _agent_dir():
    return _DEFAULT_AGENT_DIR


def _webui_version() -> str:
    return _RUNNING_WEBUI_VERSION


_RELEASE_TAG_RE = re.compile(r'^v[0-9][0-9A-Za-z.+-]*$')
_RUNNING_WEBUI_VERSION = "unknown"


def _dirty_suffix(path: Path, timeout=1) -> str:
    """Return a best-effort ``-dirty`` suffix without blocking version display."""
    out, ok = _git(['diff-index', '--quiet', 'HEAD', '--'], path, timeout=timeout)
    if ok:
        return ""
    # diff-index --quiet exits 1 with no stdout/stderr to *signal* a dirty tree
    # (not an error). _git() substitutes a synthetic "git exited with
    # status N" diagnostic when both streams are empty, which makes the naive
    # `if not out` guard always false on dirty trees — silently dropping the
    # suffix and defeating dev-build cache busting (static/foo.js?v=… stays
    # identical to the last-committed version). Treat the synthetic shape as
    # the dirty signal; real errors (timeouts, missing git) carry a different
    # diagnostic and correctly suppress the suffix.
    if not out or out.startswith('git exited with status '):
        diff, diff_ok = _git(['diff', '--binary', 'HEAD', '--'], path, timeout=timeout)
        if diff_ok and diff:
            digest = hashlib.sha1(diff.encode('utf-8', errors='replace')).hexdigest()[
                :8
            ]
            return f"-dirty-{digest}"
        return "-dirty"
    return ""


def _describe_git_version(path: Path, *, timeout=5, dirty_timeout=1) -> str | None:
    """Return a fast git version string for a checkout, if available."""
    out, ok = _git(['describe', '--tags', '--always'], path, timeout=timeout)
    if not (ok and out):
        return None
    return out + _dirty_suffix(path, timeout=dirty_timeout)


def _detect_webui_version() -> str:
    """Detect the running WebUI version from git or a baked-in fallback file.

    Resolution order:
      1. ``git describe --tags --always --dirty`` — works in any git checkout.
         Returns the exact tag on tagged commits (e.g. ``v0.50.124``), a
         post-tag descriptor between releases (e.g. ``v0.50.124-1-ge91325d``),
         or a bare SHA when no tags exist (shallow clones, fresh forks).
      2. ``api/_version.py`` — a fallback written by the Docker / CI release
         workflow when ``.git`` is not present in the image.  Expected to define
         ``__version__ = 'vX.Y.Z'``.
      3. ``'unknown'`` — last resort; displayed as-is in the settings badge.
    """
    # Timeout capped at 3s: git describe on a healthy local repo is <50ms;
    # a 10s stall on import (NFS-mounted .git, broken git binary) is unacceptable.
    out = _describe_git_version(_repo_root())
    if out:
        return out

    # Docker / baked-image fallback: api/_version.py written by CI at build time.
    # Parse with regex rather than exec() — the file holds exactly one assignment
    # and regex is sufficient; exec() on a build artifact is an unnecessary surface.
    version_file = _repo_root() / 'api' / '_version.py'
    if version_file.exists():
        try:
            import re as _re

            m = _re.search(
                r"""__version__\s*=\s*['"]([^'"]+)['"]""",
                version_file.read_text(encoding='utf-8'),
            )
            if m:
                return m.group(1)
        except Exception:
            pass

    return 'unknown'


def _read_agent_source_version(agent_dir: Path) -> str | None:
    """Read Hermes Agent's package version from a copied source tree."""
    init_file = agent_dir / 'hermes_cli' / '__init__.py'
    try:
        text = init_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


def _gateway_health_base_url() -> str:
    """Return the configured/default Hermes Agent gateway base URL."""
    raw = (
        os.environ.get('GATEWAY_HEALTH_URL')
        or os.environ.get('HERMES_GATEWAY_HEALTH_URL')
        or 'http://hermes-agent:8642'
    ).strip()
    if raw.endswith('/health/detailed'):
        raw = raw[: -len('/health/detailed')]
    elif raw.endswith('/health'):
        raw = raw[: -len('/health')]
    return raw.rstrip('/')


def _version_from_gateway_health_payload(payload: object) -> str | None:
    """Extract a version string from a Hermes Agent gateway health payload."""
    if not isinstance(payload, dict):
        return None
    for key in ('version', 'agent_version', 'hermes_version'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get('agent')
    if isinstance(nested, dict):
        value = nested.get('version')
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _detect_agent_version_from_gateway_health(timeout: float = 0.75) -> str | None:
    """Best-effort cross-container gateway API fallback for Agent version."""
    base = _gateway_health_base_url()
    if not base:
        return None
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    for path in ('/health', '/health/detailed'):
        try:
            with urllib.request.urlopen(f'{base}{path}', timeout=timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except (
            OSError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            continue
        version = _version_from_gateway_health_payload(
            payload,
        )
        if version:
            return version
    return None


def _detect_agent_version() -> str:
    """Detect the running Hermes Agent version for UI display."""
    agent_dir = Path(_agent_dir()) if _agent_dir() is not None else None

    if agent_dir is not None:
        version_file = agent_dir / "VERSION"
        try:
            if version_file.exists():
                text = version_file.read_text(encoding='utf-8').strip()
                if text:
                    return text
        except Exception:
            pass

        # Fallback: infer from git describe when the checkout exists but no VERSION
        # file is available (common in source checkouts and developer environments).
        if agent_dir.exists():
            # Symmetric with _detect_webui_version() above — `--dirty` flags a
            # locally-modified checkout so operators can see when their agent has
            # uncommitted changes vs a clean tag. Per Opus advisor on stage-293.
            out = _describe_git_version(agent_dir)
            if out:
                return out

            # Docker two-container deployments often mount a copied agent source
            # tree without .git metadata or a VERSION file.  The package version
            # still lives in hermes_cli/__init__.py, so prefer that before giving
            # up or relying on a live gateway probe.
            source_version = _read_agent_source_version(agent_dir)
            if source_version:
                return source_version

    gateway_version = _detect_agent_version_from_gateway_health()
    if gateway_version:
        return gateway_version

    return 'not detected'


# Resolved once at import time — tags cannot change without a process restart.
# ── Release channels ─────────────────────────────────────────────────────────
# The self-updater tracks ONE of several release channels, selected in Settings
# (``update_channel``). A channel is nothing more than *which glob of tags the
# updater reads* on the single linear master line — no branches, no divergence,
# so every hard-won ff-only guarantee (#2653/#2846/#3140) is preserved.
#
#   stable       -> 'v*'        promoted, soaked releases (the default). Same glob
#                                the updater has always used — every existing
#                                v0.51.N tag matches, so legacy installs and the
#                                full existing test suite keep working unchanged.
#   experimental -> 'exp-v*'    every release batch, tagged for testers who opt in.
#
# ``exp-v*`` deliberately does NOT match ``v*`` (exp tags start with 'e', not
# 'v'): the two channels never leak into each other's tag list, and a legacy
# install running the historical 'v*' glob never matches an exp tag, so it
# auto-lands on the stable stream with zero action.
DEFAULT_UPDATE_CHANNEL = 'stable'
_CHANNEL_TAG_GLOBS = {
    'stable': 'v*',
    'experimental': 'exp-v*',
}


def _normalize_channel(channel) -> str:
    """Return a known channel name, defaulting to stable for anything unknown."""
    if isinstance(channel, str) and channel in _CHANNEL_TAG_GLOBS:
        return channel
    return DEFAULT_UPDATE_CHANNEL


def _channel_tag_glob(channel) -> str:
    """Return the ``git tag --list`` glob for the given channel."""
    normalized = _normalize_channel(channel)
    return _CHANNEL_TAG_GLOBS[normalized]


def _read_update_channel() -> str:
    """Read the configured update channel from settings (stable fallback).

    Read lazily at request time — never baked at import — so a channel switch in
    Settings takes effect on the next update check without a process restart.
    """
    try:
        from api.config import load_settings

        return _normalize_channel(load_settings().get('update_channel'))
    except Exception:
        return DEFAULT_UPDATE_CHANNEL


def channel_version_badge(channel=None) -> str:
    """Return a channel-scoped version string for the Settings display badge ONLY.

    This is DELIBERATELY separate from ``_webui_version()``. ``_webui_version()`` is
    load-bearing in exact-string-equality systems — asset cache-busting URLs, the
    service-worker CACHE_NAME, the models-cache stamp, and the stale-client skew
    banner — so it must stay channel-neutral and stable for the process lifetime.
    Making it channel-dependent would falsely trip "hard refresh" banners and
    spurious cache rebuilds on every channel flip. This helper is read at request
    time purely to render ``WebUI: v0.52.47 · Experimental`` in Settings.

    Returns the channel-matched ``git describe`` (e.g. ``v0.52.47`` on stable,
    ``exp-v0.52.51`` on experimental), or falls back to ``_webui_version()`` when no
    channel tag is reachable (fresh clone, Docker image without channel tags).
    """
    if channel is None:
        channel = _read_update_channel()
    channel = _normalize_channel(channel)
    # NOTE: no ``--always`` here (deliberately different from _detect_webui_version).
    # The current version is channel-INDEPENDENT — it's just what's installed. The
    # channel only picks which tag family we compare AGAINST for updates. On a
    # stable-tagged install (e.g. HEAD == v0.52.0) that opts into Experimental, no
    # ``exp-v*`` tag is reachable BEHIND HEAD (the exp tags sit ahead on master), so
    # ``--always`` would fall through to a bare SHA and render "WebUI: d4e80b45 ·
    # Experimental" instead of the real installed version. Falling back to the
    # channel-neutral _webui_version() keeps the badge showing "v0.52.0 · Experimental".
    # (#5862)
    out, ok = _git(
        [
            'describe',
            '--tags',
            '--match',
            _channel_tag_glob(channel),
        ],
        _repo_root(),
    )
    if ok and out:
        return out + _dirty_suffix(_repo_root())
    return _webui_version()


def _release_tags(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Return the channel's release tags newest-first, in version-sort order."""
    glob = _channel_tag_glob(channel)
    out, ok = _git(['tag', '--list', glob, '--sort=-v:refname'], path)
    if not (ok and out):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _current_release_tag(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Return the latest channel release tag reachable from HEAD, if one exists.

    MUST filter by the channel glob (``--match``): a commit tagged BOTH
    ``v0.52.0`` and ``exp-v0.52.0`` describes as ``exp-v0.52.0`` (git prefers the
    lexically-later tag), so an unfiltered ``describe`` would make stable-channel
    math resolve to the experimental tag and fall through to the branch firehose.
    """
    out, ok = _git(
        [
            'describe',
            '--tags',
            '--abbrev=0',
            '--match',
            _channel_tag_glob(channel),
        ],
        path,
    )
    return out if ok and out else None


def _release_gap(tags, current, latest):
    """Count release tags between current and latest in a newest-first list."""
    if not latest or current == latest:
        return 0
    if current in tags:
        return tags.index(current)
    return 1


def _count_channel_tags_ahead(path, channel=DEFAULT_UPDATE_CHANNEL):
    """Count channel release tags strictly ahead of HEAD (fast-forwardable).

    Used only when NO channel tag is reachable behind HEAD — the channel-scoped
    ``describe`` returned None — e.g. a stable ``v0.52.0`` install opting into
    Experimental (all ``exp-v*`` tags sit ahead on master). ``_release_gap`` can't
    position HEAD in the tag list then and returns a bogus 1. ``git tag --contains
    HEAD`` lists tags whose history includes HEAD, i.e. tags that are ahead of (or
    on) HEAD; since HEAD carries no channel tag in this path, that count is exactly
    the number of channel releases the install can fast-forward to. (#5862)
    """
    out, ok = _git(
        [
            'tag',
            '--list',
            _channel_tag_glob(channel),
            '--contains',
            'HEAD',
        ],
        path,
    )
    if not (ok and out):
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _release_tag_sort_key(tag):
    """Return a version-sort key that keeps release tags newest-first."""
    raw = str(tag or '').strip()
    if raw.startswith('v'):
        raw = raw[1:]
    parts = []
    for chunk in re.split(r'(\d+)', raw):
        if not chunk:
            continue
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()))
    return tuple(parts)


def _is_stable_release_tag(tag):
    """Return True for stable release tags and False for prerelease tags."""
    raw = str(tag or '').strip()
    return bool(_RELEASE_TAG_RE.fullmatch(raw) and '-' not in raw[1:])


def _github_release_tags(
    url='https://api.github.com/repos/nesquena/hermes-webui/tags?per_page=100',
    *,
    timeout=3.0,
):
    """Return GitHub release tags newest-first, including commit SHAs when available."""
    request = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'hermes-webui',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode('utf-8'))
    if not isinstance(payload, list):
        return []
    tags = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not _is_stable_release_tag(name):
            continue
        commit = item.get('commit')
        sha = None
        if isinstance(commit, dict):
            commit_sha = commit.get('sha')
            if isinstance(commit_sha, str):
                commit_sha = commit_sha.strip()
                if commit_sha:
                    sha = commit_sha
        tags.append({'name': name, 'sha': sha})
    return sorted(
        tags,
        key=lambda item: _release_tag_sort_key(item['name']),
        reverse=True,
    )


def _check_webui_published_release_update():
    """Return a manual-update payload when the baked WebUI version trails GitHub tags."""
    current_version = str(_webui_version() or '').strip()
    if not _RELEASE_TAG_RE.fullmatch(current_version):
        return None
    try:
        tags = _github_release_tags()
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
    ):
        return None
    if not tags:
        return None

    tag_names = [item['name'] for item in tags]
    if current_version not in tag_names:
        return None

    latest = tags[0]
    latest_version = latest['name']
    behind = _release_gap(tag_names, current_version, latest_version)
    if behind <= 0:
        return None

    current = (
        next((item for item in tags if item['name'] == current_version), None) or {}
    )
    current_ref = current.get('sha') or current_version
    latest_ref = latest.get('sha') or latest_version
    repo_url = 'https://github.com/nesquena/hermes-webui'
    return {
        'name': 'webui',
        'behind': behind,
        'current_sha': current_ref,
        'latest_sha': latest_ref,
        'branch': latest_version,
        'repo_url': repo_url,
        'release_based': True,
        'current_version': current_version,
        'latest_version': latest_version,
        'compare_url': _build_compare_url(
            repo_url,
            current_ref,
            latest_ref,
        ),
        'manual_update': True,
    }


def _head_is_past_latest_tag(path, current_tag, channel=DEFAULT_UPDATE_CHANNEL):
    """Return True when HEAD has moved past the latest reachable channel tag.

    `git describe --tags --always --match <glob>` returns the bare tag name
    (e.g. ``v2026.5.16``) when HEAD is exactly on the tag, and a
    ``v2026.5.16-608-g1d22b9c2`` suffix when HEAD has moved 608 commits past it.
    Used by both the update check and the update apply path so they agree on
    which ref to advance to — see #2653 (check side) and #2846 (apply side).

    The ``--match`` filter is mandatory: without it, a HEAD sitting on a commit
    that carries the other channel's tag would describe against that tag and
    give a wrong past/at answer for THIS channel.
    """
    if not current_tag:
        return False
    full_desc, ok = _git(
        [
            'describe',
            '--tags',
            '--always',
            '--match',
            _channel_tag_glob(channel),
        ],
        path,
    )
    return bool(ok and full_desc and full_desc != current_tag)


def _head_contains_ref(path, ref):
    """Return True when ``ref`` is an ancestor of HEAD.

    Release-channel checks are tag-name based, but users tracking ``main`` can
    be on a commit that already contains the newest published tag. In that case
    a positive tag gap is not an available update; applying the tag would move
    backwards or fail fast-forward. Use the commit graph to detect that state.
    """
    if not ref:
        return False
    _, ok = _git(['merge-base', '--is-ancestor', ref, 'HEAD'], path)
    return bool(ok)


def _can_fast_forward_to(path, ref):
    """Return True when ``ref`` is a descendant of HEAD (``git pull --ff-only`` can reach it)."""
    if not ref:
        return False
    _, ok = _git(['merge-base', '--is-ancestor', 'HEAD', ref], path)
    return bool(ok)


def _select_apply_compare_ref(path, channel=DEFAULT_UPDATE_CHANNEL, target=None):
    """Return the same remote ref family that the update check reports.

    The update banner prefers published release tags when they exist. Applying
    an update must therefore advance to the latest release tag too; otherwise a
    checkout on a local/fork tracking branch can report release updates, pull a
    different branch that is already current, restart, and still remain behind.

    When HEAD is past the latest tag (the agent repo's day-to-day state between
    tagged releases), the check side falls through to the branch comparison via
    `_check_repo_release` returning None. The apply side must mirror that
    decision — otherwise we run `git pull --ff-only <latest-tag>` against a
    checkout that's already past the tag, no-op, restart, and the banner
    re-appears with the same N commits available. See #2846.

    CHANNEL SEMANTICS (webui only): the stable/experimental channels govern the
    WebUI repo. For ``target == 'webui'`` on the ``stable`` channel, stable tags
    are a *promoted subset* of master, so a stable install whose HEAD already
    contains the latest stable tag but sits behind master's tip must NOT fall
    through to the branch comparison (that would advance it to ``origin/master``
    — the full experimental firehose, defeating the channel). We return ``None``
    so the caller reports "no update". Every other case — the experimental
    channel, and the AGENT repo (which is a separate project that legitimately
    tracks master past its tags) — keeps the historical branch fallthrough
    unchanged. This mirrors ``_check_repo_release``.
    """
    channel = _normalize_channel(channel)
    suppress_stable_fallthrough = channel == 'stable' and target == 'webui'
    tags = _release_tags(path, channel)
    if tags:
        latest_tag = tags[0]
        current_tag = _current_release_tag(path, channel)
        behind = _release_gap(tags, current_tag, latest_tag)
        # Mirror the check side exactly: fall through to the branch comparison
        # whenever the checkout has already moved past the release tag that the
        # banner would otherwise advertise. The common case is behind == 0 and
        # HEAD is past its nearest tag, but main-tracking checkouts can also
        # have behind > 0 after fetching a newer tag that HEAD already contains
        # (#3140). In both cases applying the tag would no-op, move backwards,
        # or fail fast-forward; branch comparison is the truthful update path.
        # Short-circuit `or` preserves the original minimal git-call pattern.
        if (
            (
                behind == 0
                and _head_is_past_latest_tag(
                    path,
                    current_tag,
                    channel,
                )
            )
            or (behind > 0 and _head_contains_ref(path, latest_tag))
            or (behind > 0 and not _can_fast_forward_to(path, latest_tag))
        ):
            # WebUI stable: "HEAD past/contains the latest stable tag" means
            # up-to-date on the promoted subset — NOT a signal to advance to
            # master. Return None so the caller reports no update.
            if suppress_stable_fallthrough:
                return None
            # Experimental / agent: preserve the historical branch fallthrough.
            pass
        else:
            return latest_tag

    upstream, ok = _git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if ok and upstream:
        return upstream

    branch = _detect_default_branch(path)
    return f'origin/{branch}'


def _channel_up_to_date_info(path, name, channel, current_tag):
    """Return an 'up to date' payload for a channel that must NOT branch-compare.

    Used by the stable channel: stable tags are a promoted subset of master, so
    when HEAD already contains the latest stable tag we report up-to-date
    (behind == 0) rather than falling through to the branch comparison, which
    would advance the user onto the experimental firehose.
    """
    remote_url, _ = _git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)
    return {
        'name': name,
        'behind': 0,
        'current_sha': current_tag,
        'latest_sha': current_tag,
        'branch': current_tag,
        'repo_url': remote_url,
        'release_based': True,
        'current_version': current_tag,
        'latest_version': current_tag,
        'channel': channel,
    }


def _check_repo_release(path, name, channel=DEFAULT_UPDATE_CHANNEL):
    """Check if a git repo is behind its latest published channel release tag."""
    channel = _normalize_channel(channel)
    tags = _release_tags(path, channel)
    if not tags:
        return None

    latest_tag = tags[0]
    current_tag = _current_release_tag(path, channel)
    behind = _release_gap(tags, current_tag, latest_tag)

    # When NO channel tag is reachable behind HEAD, _current_release_tag returns
    # None (channel-scoped `describe --abbrev=0` fatals with "No tags can describe").
    # This is the normal state of a stable-tagged install (HEAD == v0.52.0) opting
    # into Experimental: every exp-v* tag sits AHEAD on master. _release_gap can't
    # position None in the tag list and returns a bogus 1, and the display fields
    # would carry current_version=None (rendered as "unknown"). Recover the real
    # ahead-count and show the channel-neutral installed version as the current
    # version — the channel only chooses the comparison tag family, not what's
    # installed. (#5862)
    current_version_display = current_tag
    # A git-verified ref for the compare link (defaults to the resolved channel
    # tag; may be refined below in the no-channel-tag-behind-HEAD fallback).
    current_sha_ref = current_tag
    if current_tag is None:
        ahead = _count_channel_tags_ahead(path, channel)
        if ahead > 0:
            behind = ahead
        # Scope the installed-version fallback to the WebUI repo only.
        # _check_repo_release() is shared with the Agent repo, and _webui_version()
        # (e.g. v0.52.0) is not a valid ref/tag in the Agent repository — injecting
        # it there would display the WebUI version as the Agent's installed version
        # and produce a broken Agent compare link. (#5864)
        if name == "webui":
            current_version_display = _webui_version()
            # For the compare link, derive a git-VERIFIED installed tag rather than
            # reusing _webui_version() (which can be `vX.Y.Z-dirty-<hash>`, `-N-g<sha>`,
            # a bare SHA, or `unknown` — none guaranteed refs). Prefer the exact tag
            # on HEAD across ALL release families (channel-neutral), so a stable-
            # pinned Experimental install still gets a resolvable /compare/<tag>...
            # link; fall back to None (no link) when HEAD is not exactly on a tag. (#5864)
            exact_tag, ok = _git(['describe', '--tags', '--exact-match', 'HEAD'], path)
            exact_tag = (exact_tag or '').strip()
            current_sha_ref = exact_tag if ok and exact_tag else None

    # If behind == 0 but HEAD has moved past the tag (e.g. the agent repo
    # keeps committing to master between tagged releases), the release check
    # would report "Up to date" even though hundreds of commits are missing.
    # Fall through to _check_repo_branch so the real commit count is reported
    # instead. The same predicate is used by _select_apply_compare_ref so the
    # check and apply sides cannot drift again. See #2653 (check), #2846 (apply).
    #
    # CHANNEL (webui only): for the WebUI repo on stable, stable tags are a
    # promoted SUBSET of master, so "HEAD past the latest stable tag" means
    # up-to-date on the promoted subset, NOT a signal to branch-compare against
    # origin/master (the firehose). Report up-to-date. The AGENT repo and the
    # experimental channel keep the historical fall-through.
    suppress_stable_fallthrough = channel == 'stable' and name == 'webui'
    if behind == 0 and _head_is_past_latest_tag(
        path,
        current_tag,
        channel,
    ):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(
                path,
                name,
                channel,
                current_tag,
            )
        return None

    # Users tracking main can already contain the newest fetched release tag
    # while their nearest reachable tag is older. A positive tag gap then means
    # only "there is a newer tag name", not "HEAD is behind that tag" (#3140).
    # Fall through to the branch check so the banner compares against the
    # configured upstream instead of advertising a tag that cannot fast-forward.
    if behind > 0 and _head_contains_ref(path, latest_tag):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(
                path,
                name,
                channel,
                current_tag,
            )
        return None

    # Patch releases can land on a side branch while day-to-day installs track
    # main past an older tag. A positive tag-name gap then advertises an update
    # that `git pull --ff-only <latest-tag>` cannot reach.
    if behind > 0 and not _can_fast_forward_to(path, latest_tag):
        if suppress_stable_fallthrough:
            return _channel_up_to_date_info(
                path,
                name,
                channel,
                current_tag,
            )
        return None

    remote_url, _ = _git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)

    return {
        'name': name,
        'behind': behind,
        # GitHub compare URLs accept tag names, and tag-to-tag links are the
        # clearest "what changed in this release?" view for operators. Use a
        # git-VERIFIED ref for the compare link: the resolved channel tag when
        # one is reachable behind HEAD, else None. _webui_version() is NOT safe here
        # — it can be `v0.52.0-dirty-<hash>`, `v0.52.0-N-g<sha>`, a bare SHA, or
        # `unknown`, none of which are guaranteed refs, so reusing it would emit
        # a broken /compare link (ui.js) and lose update-summary commit subjects. (#5864)
        'current_sha': current_sha_ref,
        'latest_sha': latest_tag,
        'branch': latest_tag,
        'repo_url': remote_url,
        'release_based': True,
        'current_version': current_version_display,
        'latest_version': latest_tag,
        'channel': channel,
    }


def _check_repo_branch(path, name, *, fetch=True):
    """Fallback: check if a git repo is behind its upstream branch."""

    # Fetch latest from origin (network call, cached by TTL)
    if fetch:
        _, fetch_ok = _git(['fetch', 'origin', '--quiet'], path, timeout=15)
        if not fetch_ok:
            return {'name': name, 'behind': 0, 'error': 'fetch failed'}

    # Use the current branch's upstream tracking branch, not the repo default.
    # This avoids false "N updates behind" alerts when the user is on a feature
    # branch and master/main has moved forward with unrelated commits.
    # If no upstream is set (brand-new local branch), fall back to the default branch.
    upstream, ok = _git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if ok and upstream:
        # upstream is like "origin/feat/foo" — use it directly in rev-list
        compare_ref = upstream
    else:
        branch = _detect_default_branch(path)
        compare_ref = f'origin/{branch}'

    # Count commits behind
    out, ok = _git(['rev-list', '--count', f'HEAD..{compare_ref}'], path)
    behind = int(out) if ok and out.isdigit() else 0

    # Get short SHAs for display.
    #
    # latest_sha = upstream tip (compare_ref). Always exists on github.com
    # because it is literally the commit `git fetch` just pulled.
    #
    # current_sha is trickier. The intuitive choice — local HEAD — breaks
    # the "What's new?" compare URL whenever HEAD is not a public commit:
    # unpushed work, dirty stage branches, forks, in-flight rebases, or
    # release-time merge commits whose SHA only lives in the maintainer's
    # checkout. We saw exactly this in #1579: a banner reporting "17 updates"
    # linked to /compare/<localHEAD>...<upstream> and 404'd because <localHEAD>
    # was never pushed to the canonical repo.
    #
    # The right base is the merge-base between HEAD and the upstream ref —
    # that's the most recent commit both sides agree on, and (because
    # `git fetch` succeeded above) it is guaranteed to be present upstream.
    # If a user is 17 commits behind with no local-only commits, merge-base
    # equals local HEAD and the URL is identical to what we shipped before;
    # if they ARE ahead with local-only commits, the URL still resolves to
    # the public history they share with upstream. If merge-base fails for
    # any reason (e.g. shallow clone where the bases diverge before the
    # cutoff), fall back to None so the JS link guard suppresses the link
    # rather than emitting a known-broken URL.
    mb_full, mb_ok = _git(['merge-base', 'HEAD', compare_ref], path)
    if mb_ok and mb_full:
        short, ok = _git(['rev-parse', '--short', mb_full], path)
        current = short if (ok and short) else None
    else:
        current = None
    latest, _ = _git(['rev-parse', '--short', compare_ref], path)

    # Get repo URL for "What's new?" link
    remote_url, _ = _git(['remote', 'get-url', 'origin'], path)
    remote_url = _normalize_remote_url(remote_url)

    return {
        'name': name,
        'behind': behind,
        'current_sha': current,
        'latest_sha': latest,
        'branch': compare_ref,
        'repo_url': remote_url,
        'compare_url': _build_compare_url(remote_url, current, latest),
    }


def _check_repo(path, name, channel=DEFAULT_UPDATE_CHANNEL):
    """Check if a git repo is behind its latest release. Returns dict or None.

    The returned dict (when not None) always carries a ``dirty: bool`` reflecting
    the working-tree state vs HEAD. A dirty install at-or-past the latest release
    tag used to silently report "Up to date" with no remediation affordance, so
    the Settings panel reads this flag to offer ``apply_force_update`` (issue
    #4085).

    When ``.git`` is absent (Docker images, pip installs), returns a minimal dict
    with ``no_git: True`` and ``behind: None`` so the frontend can distinguish
    "can't check" from "up to date" (issue #4356).
    """
    channel = _normalize_channel(channel)
    if path is None or not (path / '.git').exists():
        if name == 'webui':
            release_info = _check_webui_published_release_update()
            if release_info is not None:
                release_info = dict(release_info)
                release_info['no_git'] = True
                return release_info
        return {
            'name': name,
            'behind': None,
            'no_git': True,
        }

    # Fetch tags first so update prompts track published releases, not every
    # development commit that lands on master/main after the latest release.
    #
    # --force is required because the WebUI is a release-tracking consumer:
    # it never pushes tags, so it should always defer to whatever the remote
    # says a release tag points to. Without --force, a remote re-tag (e.g.
    # after a squash-merge that re-points a release tag at a new SHA) jams
    # the update path indefinitely with "would clobber existing tag" errors.
    # See #2756.
    fetch_out, fetch_ok = _git(
        ['fetch', 'origin', '--tags', '--force'], path, timeout=15
    )
    if not fetch_ok:
        release_info = _check_repo_release(path, name, channel)
        message = 'fetch failed'
        if fetch_out:
            detail = _sanitize_git_diagnostic(fetch_out)
            message = f'{message}: {detail}'
        if release_info is not None:
            release_info = dict(release_info)
            release_info['error'] = message
            release_info['stale_check'] = True
            release_info['dirty'] = _is_dirty(path)
            return release_info
        return {
            'name': name,
            'behind': None,
            'error': message,
            'stale_check': True,
            'dirty': _is_dirty(path),
        }

    release_info = _check_repo_release(path, name, channel)
    if release_info is not None:
        release_info = dict(release_info)
        release_info['dirty'] = _is_dirty(path)
        return release_info

    branch_info = _check_repo_branch(path, name, fetch=False)
    if branch_info is not None:
        branch_info = dict(branch_info)
        branch_info['dirty'] = _is_dirty(path)
        branch_info['channel'] = channel
        return branch_info
    return None


def _is_dirty(path: Path, timeout: int = 1) -> bool:
    """Return True when the working tree has uncommitted changes vs HEAD.

    Same primitive as ``_dirty_suffix`` (issue #4085): ``git diff-index
    --quiet HEAD --`` exits 0 on a clean tree and 1 on a dirty tree (not an
    error). Real errors (timeout, missing git, fatal) are conservatively
    reported as clean so a transient probe failure never produces a false-
    positive "local changes" alert.
    """
    out, ok = _git(['diff-index', '--quiet', 'HEAD', '--'], path, timeout=timeout)
    if ok:
        return False
    return not out or out.startswith('git exited with status ')
