import { _knownSessionProfileCount } from './session-unread.js';
import { sidebarStateBindings } from './sidebar-store.js';

function animateNextSessionListRefresh(options={}){
  sidebarStateBindings._sessionListRefreshAnimationPending = true;
  if(options&&options.enterAll) sidebarStateBindings._sessionListEnterAllAnimationPending = true;
}

// ── Loading skeletons (#4662 Phase 1) ───────────────────────────────────────
// Tracks whether the session list is currently showing a skeleton so a
// resolving render knows to replace it (and so we don't stack skeletons).
let _sessionListSkeletonActive = false;

// Skeleton structure mirrors a real sidebar: a couple of group headers
// (Pinned / Today / Last week) with single-line rows under each. Title widths
// vary so it reads as real conversations. `stamp:false` omits the timestamp bar
// on the occasional row (a real list mixes rows with/without a visible time).
const _SESSION_SKELETON_GROUPS = [
  {rows: [{title: 70}]},
  {rows: [{title: 84}, {title: 58}, {title: 76}]},
  {rows: [{title: 64}, {title: 90}, {title: 52}, {title: 72}]},
];

// Render a skeleton placeholder into #sessionList that mirrors the real row
// anatomy (group labels + single-line title bars with a short timestamp bar).
// Called the instant a profile switch begins so the user never sees the
// previous profile's conversations.
function showSessionListSkeleton(targetProfile){
  const list = $('sessionList');
  if(!list) return;
  // Tear down any active virtual-scroll state up front so a pending scroll-driven
  // render can't repaint the previous profile's cached rows over the skeleton
  // (#4662 Codex gate). Cancel the queued RAF and drop the data-session-virtual-*
  // window markers; the real render rebuilds them from the new payload. Done once
  // here so it applies to BOTH the content and empty-state skeleton branches.
  if(typeof sidebarStateBindings._sessionVirtualScrollRaf!=='undefined'&&sidebarStateBindings._sessionVirtualScrollRaf){
    cancelAnimationFrame(sidebarStateBindings._sessionVirtualScrollRaf);
    sidebarStateBindings._sessionVirtualScrollRaf=0;
  }
  delete list.dataset.sessionVirtualTotal;
  delete list.dataset.sessionVirtualStart;
  delete list.dataset.sessionVirtualEnd;
  delete list.dataset.sessionVirtualFilter;
  delete list.dataset.sessionVirtualActiveAnchor;
  // #4717: if we already know (from a prior render) the profile we're switching
  // INTO has zero conversations, a full content skeleton (group labels + 8 rows)
  // is misleading — it implies data that will never arrive, then resolves to an
  // empty list. Render a quiet empty-state placeholder instead. Only when the
  // count is KNOWN to be 0; an unknown profile (null) keeps the content skeleton
  // (safe default — never hide a skeleton for a profile that may have sessions).
  // Skip the empty branch while a project/source filter is active, since the
  // per-profile count is an unfiltered total and could be non-zero overall yet
  // empty under the filter (or vice-versa) — the content skeleton is the safe
  // choice there. typeof guards keep this safe if the helper isn't in scope.
  const knownCount = (typeof targetProfile === 'string' && targetProfile
      && typeof _knownSessionProfileCount === 'function')
    ? _knownSessionProfileCount(targetProfile) : null;
  const filterActive = (typeof sidebarStateBindings._activeProject !== 'undefined' && sidebarStateBindings._activeProject)
    || (typeof sidebarStateBindings._sessionSourceFilter !== 'undefined' && sidebarStateBindings._sessionSourceFilter === 'cli');
  const wrap = document.createElement('div');
  wrap.setAttribute('aria-hidden', 'true');
  if(knownCount === 0 && !filterActive){
    // A single faint placeholder bar rather than a "no conversations" text — the
    // real empty-state note paints the instant the (fast, empty) fetch resolves,
    // so we just hold a calm, content-free space in the meantime (no flash of a
    // fake list, no premature wording).
    wrap.className = 'skeleton-list skeleton-list-empty';
    const bar = document.createElement('div');
    bar.className = 'skeleton-empty-hint';
    wrap.appendChild(bar);
  } else {
    wrap.className = 'skeleton-list';
    let rowIndex = 0;
    for(const group of _SESSION_SKELETON_GROUPS){
      const label = document.createElement('div');
      label.className = 'skeleton-group-label';
      wrap.appendChild(label);
      for(const spec of group.rows){
        const row = document.createElement('div');
        row.className = 'skeleton-row';
        // Stagger the fade-in per row. Set inline (not via CSS :nth-child) because
        // group-label siblings are interleaved with rows, so a :nth-child stagger
        // would skip most rows. Cap so the longest list doesn't feel laggy.
        row.style.animationDelay = Math.min(rowIndex * 0.025, 0.2) + 's';
        rowIndex++;
        const title = document.createElement('div');
        title.className = 'skeleton-bar skeleton-title';
        title.style.width = spec.title + '%';
        const stamp = document.createElement('div');
        stamp.className = 'skeleton-bar skeleton-stamp';
        row.appendChild(title);
        row.appendChild(stamp);
        wrap.appendChild(row);
      }
    }
  }
  list.innerHTML = '';
  list.appendChild(wrap);
  list.scrollTop = 0;
  _sessionListSkeletonActive = true;
}


export { animateNextSessionListRefresh, showSessionListSkeleton };

export const sessionListViewBindings=Object.freeze({
  get _sessionListSkeletonActive(){ return _sessionListSkeletonActive; },
  set _sessionListSkeletonActive(value){ _sessionListSkeletonActive=value; },
});
