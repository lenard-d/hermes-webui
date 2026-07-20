"""Human-readable summaries for available Hermes updates.

This module owns the presentation-facing interpretation of an update payload:
commit-subject truncation, prompting, bullet normalization, stable sections,
deduplication, and the bounded summary cache.  Git discovery and update
application deliberately remain outside this module.
"""

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Callable


_SUMMARY_CACHE_MAX = 16
_summary_cache: OrderedDict = OrderedDict()
_summary_cache_lock = threading.Lock()


def _summary_cache_key(details: list[dict]) -> str:
    """Return a stable key for the exact update range being summarized."""
    payload = []
    for item in details:
        payload.append({
            'name': item.get('name'),
            'behind': item.get('behind'),
            'current_sha': item.get('current_sha'),
            'latest_sha': item.get('latest_sha'),
            'compare_url': item.get('compare_url'),
        })
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _clean_summary_bullet(line: str) -> str:
    line = re.sub(r'^\s*(?:[-*•]+|\d+[.)])\s*', '', str(line or '')).strip()
    line = re.sub(r'\s+', ' ', line)
    if not line:
        return ''
    if line[-1] not in '.!?':
        line += '.'
    return line[:240]


def _split_summary_category(line: str) -> tuple[str | None, str]:
    raw = str(line or '').strip()
    match = re.match(r'^\s*(?:[-*•]+|\d+[.)])?\s*(notice|what you(?:ll|\'ll| will) notice|user(?:s)? will notice|worth knowing|worth|note)\s*:\s*(.+)$', raw, re.I)
    if not match:
        return None, raw
    label = match.group(1).lower()
    category = 'worth' if label in {'worth knowing', 'worth', 'note'} else 'notice'
    return category, match.group(2)


def _unique_summary_bullets(items: list[str]) -> list[str]:
    seen = set()
    bullets = []
    for item in items:
        cleaned = _clean_summary_bullet(item)
        key = cleaned.lower()
        if cleaned and key not in seen:
            bullets.append(cleaned)
            seen.add(key)
    return bullets


def _summary_bullets_from_text(text: str, *, fallback_items: list[str]) -> list[str]:
    raw = str(text or '').strip()
    candidates = []
    for line in raw.splitlines():
        _category, body = _split_summary_category(line)
        cleaned = _clean_summary_bullet(body)
        if cleaned:
            candidates.append(cleaned)
    if len(candidates) <= 1 and raw:
        candidates = [_clean_summary_bullet(part) for part in re.split(r'(?<=[.!?])\s+', raw)]
        candidates = [item for item in candidates if item]
    if not candidates:
        candidates = [_clean_summary_bullet(item) for item in fallback_items]
    bullets = _unique_summary_bullets(candidates)
    return bullets or ['Updates are available.']


def _categorized_summary_bullets_from_text(text: str) -> tuple[list[str], list[str]]:
    notice_items: list[str] = []
    worth_items: list[str] = []
    for line in str(text or '').splitlines():
        category, body = _split_summary_category(line)
        if category == 'notice':
            notice_items.append(body)
        elif category == 'worth':
            worth_items.append(body)
        elif re.match(r'^\s*(?:[-*•]+|\d+[.)])?\s*[A-Za-z][A-Za-z ]{1,32}\s*:', str(line or '')):
            notice_items.append(body)
    return _unique_summary_bullets(notice_items), _unique_summary_bullets(worth_items)


def _fallback_update_bullets(details: list[dict]) -> list[str]:
    bullets = []
    for item in details:
        label = item.get('label') or item.get('name') or 'Hermes'
        behind = item.get('behind') or 0
        commits = item.get('commits') or []
        if commits:
            highlights = '; '.join(commits[:3])
            qualifier = 'recent updates' if item.get('commits_truncated') else 'updates'
            bullets.append(f"{label} has {behind} update(s), including {qualifier}: {highlights}.")
        else:
            bullets.append(f"{label} has {behind} update(s) available.")
    return bullets or ['Updates are available.']


def _worth_knowing_bullets(details: list[dict]) -> list[str]:
    items = []
    truncated = [item for item in details if item.get('commits_truncated') and item.get('commits_limit')]
    for item in truncated[:2]:
        label = item.get('label') or item.get('name') or 'Hermes'
        behind = item.get('behind') or 0
        limit = item.get('commits_limit') or len(item.get('commits') or [])
        items.append(
            f"{label} has {behind} updates; this summary uses the latest {limit} commit subjects, with the full comparison still available in the diff link."
        )
    if items:
        return items
    targets = [
        f"{item.get('label') or item.get('name') or 'Hermes'} ({item.get('behind') or 0} update{'s' if (item.get('behind') or 0) != 1 else ''})"
        for item in details
        if item.get('behind')
    ]
    if len(targets) > 1:
        return ['This summary combines updates from ' + ' and '.join(targets) + '.']
    return []


def _format_update_summary_sections(summary_text: str, details: list[dict]) -> tuple[list[dict], str]:
    notice_items, worth_items = _categorized_summary_bullets_from_text(summary_text)
    if not notice_items:
        notice_items = _summary_bullets_from_text(summary_text, fallback_items=_fallback_update_bullets(details))
    notice_keys = {item.lower() for item in notice_items}
    worth_items = [item for item in worth_items if item.lower() not in notice_keys]
    worth_items.extend(
        item for item in _worth_knowing_bullets(details)
        if item.lower() not in notice_keys and item.lower() not in {existing.lower() for existing in worth_items}
    )
    sections = [
        {
            'title': "What you'll notice",
            'items': notice_items,
        },
    ]
    if worth_items:
        sections.append(
            {
                'title': 'Worth knowing',
                'items': worth_items,
            }
        )
    lines = []
    for section in sections:
        lines.append(section['title'])
        lines.extend(f"- {item}" for item in section['items'])
        lines.append('')
    return sections, '\n'.join(lines).strip()


def _update_summary_prompt(details: list[dict]) -> tuple[str, str]:
    system = (
        "You write human-readable release summaries for Hermes users. "
        "Focus on what the user will notice in the product. Keep it simple, specific, and short. "
        "avoid technical jargon, implementation details, SHA names, branch names, and file paths unless necessary. "
        "Return only bullets. Do not include headings, markdown tables, intro paragraphs, or closing notes."
    )
    user_lines = [
        "Summarize these available updates as concise bullets.",
        "Prefix each bullet with `Notice:` for user-visible behavior changes or `Worth knowing:` for useful context.",
        "Put user-visible Notice bullets first and include every meaningful user-facing change from the available commit subjects.",
        "Use Worth knowing only for helpful context that is not a duplicate of a Notice bullet.",
        "Use everyday language and explain visible behavior changes, not code mechanics.",
        "Return only prefixed bullets; the WebUI will add the fixed section headings separately.",
        "",
    ]
    for item in details:
        user_lines.append(f"{item['label']}: {item['behind']} commit(s) behind")
        commits = item.get('commits') or []
        if commits:
            if item.get('commits_truncated'):
                user_lines.append(
                    f"- Showing latest {len(commits)} of {item['behind']} commit subjects; summarize trends, not every commit."
                )
            user_lines.extend(f"- {subject}" for subject in commits)
        else:
            user_lines.append("- No local commit subjects available; summarize only the update count.")
        user_lines.append("")
    return system, '\n'.join(user_lines)


def summarize_update_payload(
    updates: dict,
    commit_subjects_for_update: Callable[..., tuple[list[str], bool]],
    llm_callback=None,
    *,
    target: str | None = None,
    use_cache: bool = True,
) -> dict:
    """Build a human-readable What's New summary and keep regular diff links.

    ``commit_subjects_for_update`` resolves local git metadata at the caller's
    update-checking seam.  This module interprets only the resulting subjects
    and update payload; it never fetches or mutates a repository.
    """
    if not isinstance(updates, dict):
        updates = {}
    requested_target = target if target in ('webui', 'agent') else None
    details = []
    for key, label in (('webui', 'WebUI'), ('agent', 'Agent')):
        if requested_target and key != requested_target:
            continue
        info = updates.get(key)
        if not isinstance(info, dict) or int(info.get('behind') or 0) <= 0:
            continue
        commit_limit = 24
        commits, commits_truncated = commit_subjects_for_update({'name': key, **info}, limit=commit_limit)
        behind = int(info.get('behind') or 0)
        details.append({
            'name': key,
            'label': label,
            'behind': behind,
            'current_sha': info.get('current_sha'),
            'latest_sha': info.get('latest_sha'),
            'compare_url': info.get('compare_url'),
            'commits': commits,
            'commits_limit': commit_limit,
            'commits_truncated': bool(commits_truncated or (commits and behind > len(commits))),
        })

    cache_key = _summary_cache_key(details)
    if use_cache:
        with _summary_cache_lock:
            cached = _summary_cache.get(cache_key)
            if cached:
                _summary_cache.move_to_end(cache_key)
        if cached:
            result = dict(cached)
            result['cached'] = True
            return result

    generated_by = 'fallback'
    candidate = ''
    if details and callable(llm_callback):
        system, prompt = _update_summary_prompt(details)
        try:
            candidate = (llm_callback(system, prompt) or '').strip()
            if candidate:
                generated_by = 'llm'
        except Exception:
            candidate = ''
    sections, summary = _format_update_summary_sections(candidate, details)
    result = {
        'ok': True,
        'summary': summary,
        'summary_sections': sections,
        'generated_by': generated_by,
        'cached': False,
        'cache_key': cache_key,
        'target': requested_target,
        'targets': details,
    }
    if use_cache:
        with _summary_cache_lock:
            if len(_summary_cache) >= _SUMMARY_CACHE_MAX and cache_key not in _summary_cache:
                _summary_cache.popitem(last=False)
            _summary_cache[cache_key] = dict(result)
    return result
