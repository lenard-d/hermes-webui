import { state } from "./state.js";
import { loadKanbanBoards,loadKanbanTask,updateKanbanTask } from "./kanban-boards.js";
import { _kanbanBoardQuery,_scheduleKanbanRefresh } from "./kanban-tasks.js";

// Panels domain: kanban state and board rendering

// ── Kanban panel (read-only) ──
export function _kanbanColumnLabel(name){ return t('kanban_status_' + name) || name; }
export function _kanbanTaskTitle(task){ return task.title || task.summary || task.id || t('kanban_task'); }
export function _kanbanTaskBody(task){ return task.body || task.description || task.prompt || ''; }
export function _kanbanTaskMeta(task){
  const bits = [];
  bits.push(task.assignee ? task.assignee : t('kanban_unassigned'));
  if (task.tenant) bits.push(task.tenant);
  if (task.priority !== undefined && task.priority !== null) bits.push('P' + task.priority);
  if (task.comment_count) bits.push('💬 ' + task.comment_count);
  if (task.link_counts && task.link_counts.children) bits.push('↳ ' + task.link_counts.children);
  return bits;
}

export function _kanbanCurrentFilters(){
  const q = $('kanbanSearch') ? $('kanbanSearch').value.trim().toLowerCase() : '';
  const assigneeEl = $('kanbanAssigneeFilter');
  const tenantEl = $('kanbanTenantFilter');
  const assignee = assigneeEl ? (assigneeEl.value || assigneeEl.dataset.defaultValue || '') : '';
  const tenant = tenantEl ? (tenantEl.value || tenantEl.dataset.defaultValue || '') : '';
  const includeArchived = !!($('kanbanIncludeArchived') && $('kanbanIncludeArchived').checked);
  const onlyMine = !!($('kanbanOnlyMine') && $('kanbanOnlyMine').checked);
  return {q, assignee, tenant, includeArchived, onlyMine};
}

export function _kanbanApplyConfigDefaults(config){
  if (!config) return;
  state._kanbanLanesByProfile = config.lane_by_profile === true;
  syncKanbanViewToggle();
  if (state._kanbanConfigApplied) return;
  if ($('kanbanTenantFilter') && config.default_tenant) $('kanbanTenantFilter').dataset.defaultValue = config.default_tenant;
  if ($('kanbanIncludeArchived') && config.include_archived_by_default === true) $('kanbanIncludeArchived').checked = true;
  state._kanbanConfigApplied = true;
}

export function syncKanbanViewToggle(){
  const btn = $('btnKanbanViewToggle');
  if (!btn) return;
  const consolidated = !state._kanbanLanesByProfile;
  const label = t('kanban_view_consolidated');
  btn.setAttribute('aria-pressed', consolidated ? 'true' : 'false');
  btn.setAttribute('aria-label', label);
  btn.setAttribute('data-i18n-title', 'kanban_view_consolidated');
  btn.setAttribute('data-i18n-aria-label', 'kanban_view_consolidated');
  if (typeof _setButtonTooltip === 'function') _setButtonTooltip(btn, label);
  else btn.setAttribute('data-tooltip', label);
}

export async function toggleKanbanViewMode(){
  const btn = $('btnKanbanViewToggle');
  const nextLaneByProfile = !state._kanbanLanesByProfile;
  if (btn) btn.disabled = true;
  try {
    const saved = await api('/api/kanban/config', {method: 'PATCH', body: JSON.stringify({lane_by_profile: nextLaneByProfile})});
    state._kanbanLanesByProfile = saved.lane_by_profile === true;
    syncKanbanViewToggle();
    _kanbanRenderBoard();
    showToast(t(state._kanbanLanesByProfile ? 'kanban_view_lanes_saved' : 'kanban_view_consolidated_saved'));
  } catch(e) {
    showToast(t('kanban_view_update_failed') + (e.message || e), 4000, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export function _kanbanSetSelectOptions(el, values, allLabelKey){
  if (!el) return;
  const current = el.value || el.dataset.defaultValue || '';
  const opts = [`<option value="">${esc(t(allLabelKey))}</option>`]
    .concat((values || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`));
  el.innerHTML = opts.join('');
  if ([...el.options].some(o => o.value === current)) el.value = current;
}

export function _kanbanVisibleTasks(){
  const filters = _kanbanCurrentFilters();
  const columns = (state._kanbanBoard && state._kanbanBoard.columns) || [];
  return columns.map(col => {
    const tasks = (col.tasks || []).filter(task => {
      if (!filters.q) return true;
      const haystack = [task.id, _kanbanTaskTitle(task), _kanbanTaskBody(task), task.assignee, task.tenant]
        .filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(filters.q);
    });
    return {...col, tasks};
  });
}

export function _kanbanRenderSidebar(columns){
  const list = $('kanbanList');
  if (!list) return;
  const tasks = columns.flatMap(col => (col.tasks || []).map(task => ({...task, status: task.status || col.name})));
  if (!tasks.length) {
    list.innerHTML = `<div class="kanban-empty" data-i18n="kanban_no_matching_tasks">${esc(t('kanban_no_matching_tasks'))}</div>`;
    return;
  }
  list.innerHTML = tasks.map(task => {
    const meta = _kanbanTaskMeta(task);
    return `<button class="kanban-list-item" onclick="loadKanbanTask('${esc(task.id)}')">
      <span class="kanban-list-status">${esc(_kanbanColumnLabel(task.status))}</span>
      <span class="kanban-list-title">${esc(_kanbanTaskTitle(task))}</span>
      ${meta.length ? `<span class="kanban-meta">${esc(meta.join(' · '))}</span>` : ''}
    </button>`;
  }).join('');
}


/**
 * Render inline markdown (bold, italic, code, links, strikethrough).
 * Input is already HTML-escaped.
 */
export function _kanbanRenderMarkdownInline(escaped){
  return String(escaped || '')
    .replace(/~~([^~\n]+)~~/g, (_m, text) => `<del>${text}</del>`)
    .replace(/`([^`\n]+)`/g, (_m, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, (_m, text) => `<strong>${text}</strong>`)
    .replace(/(^|[^*a-zA-Z0-9])\*([^*\n]+)\*/g, (_m, prefix, text) => `${prefix}<em>${text}</em>`)
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_m, text, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`);
}

/**
 * Render full markdown block content: headings, code blocks, lists, tables,
 * task lists, blockquotes, horizontal rules, paragraphs + inline formatting.
 */
export function _kanbanRenderMarkdown(source){
  if (!source) return '';
  const lines = esc(source).split(/\r?\n/);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // ── Code block ──
    if (/^```/.test(trimmed)) {
      const lang = trimmed.slice(3).trim();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip closing ```
      const codeHtml = codeLines.join('\n');
      out.push(lang
        ? `<pre class="hermes-kanban-code"><code class="language-${_kanbanRenderMarkdownInline(lang)}">${codeHtml}</code></pre>`
        : `<pre class="hermes-kanban-code"><code>${codeHtml}</code></pre>`);
      continue;
    }

    // ── Horizontal rule ──
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(trimmed)) {
      out.push('<hr>');
      i++;
      continue;
    }

    // ── Heading ──
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      out.push(`<h${level}>${_kanbanRenderMarkdownInline(headingMatch[2])}</h${level}>`);
      i++;
      continue;
    }

    // ── Blockquote ──
    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${_kanbanRenderMarkdownInline(quoteLines.join('<br>'))}</blockquote>`);
      continue;
    }

    // ── Table row ──
    if (/^\|.+\|$/.test(trimmed)) {
      const tableRows = [];
      const tableAligns = [];
      while (i < lines.length && /^\|.+\|$/.test(lines[i].trim())) {
        const row = lines[i].trim();
        // Detect alignment separator row
        if (/^\|[\s:]*-{3,}[\s:]*\|/.test(row)) {
          const cells = row.split('|').filter(c => c.trim().length > 0);
          cells.forEach(c => {
            const t = c.trim();
            if (t.startsWith(':') && t.endsWith(':')) tableAligns.push('center');
            else if (t.endsWith(':')) tableAligns.push('right');
            else tableAligns.push('left');
          });
        } else {
          const cells = row.split('|').filter(c => c.trim().length > 0);
          tableRows.push(cells.map((c, ci) => {
            const align = tableAligns[ci] ? ` style="text-align:${tableAligns[ci]}"` : '';
            return `<td${align}>${_kanbanRenderMarkdownInline(c.trim())}</td>`;
          }).join(''));
        }
        i++;
      }
      if (tableRows.length) {
        out.push(`<table><tbody>${tableRows.map(r => `<tr>${r}</tr>`).join('')}</tbody></table>`);
      }
      continue;
    }

    // ── Task list item ──
    const taskMatch = trimmed.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
    if (taskMatch) {
      const checked = taskMatch[1] !== ' ';
      const text = taskMatch[2];
      const items = [];
      items.push(`<li class="hermes-kanban-task${checked ? ' checked' : ''}"><input type="checkbox"${checked ? ' checked' : ''} disabled> ${_kanbanRenderMarkdownInline(text)}</li>`);
      i++;
      // Collect continuation items
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextTask = next.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
        const nextLi = next.match(/^[-*+]\s+(.+)$/);
        if (nextTask) {
          const c = nextTask[1] !== ' ';
          items.push(`<li class="hermes-kanban-task${c ? ' checked' : ''}"><input type="checkbox"${c ? ' checked' : ''} disabled> ${_kanbanRenderMarkdownInline(nextTask[2])}</li>`);
          i++;
        } else if (nextLi) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextLi[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // ── Unordered list item ──
    const ulMatch = trimmed.match(/^[-*+]\s+(.+)$/);
    if (ulMatch) {
      const items = [];
      items.push(`<li>${_kanbanRenderMarkdownInline(ulMatch[1])}</li>`);
      i++;
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextUl = next.match(/^[-*+]\s+(.+)$/);
        const nextTask = next.match(/^[-*+]\s+\[( |x|X)\]\s+(.+)$/);
        if (nextTask) break; // let task list handler get it
        if (nextUl) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextUl[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // ── Ordered list item ──
    const olMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (olMatch) {
      const items = [];
      items.push(`<li>${_kanbanRenderMarkdownInline(olMatch[1])}</li>`);
      i++;
      while (i < lines.length) {
        const next = lines[i].trim();
        const nextOl = next.match(/^\d+\.\s+(.+)$/);
        if (nextOl) {
          items.push(`<li>${_kanbanRenderMarkdownInline(nextOl[1])}</li>`);
          i++;
        } else {
          break;
        }
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    // ── Empty line ──
    if (!trimmed) {
      out.push('');
      i++;
      continue;
    }

    // ── Paragraph ──
    out.push(`<p>${_kanbanRenderMarkdownInline(trimmed)}</p>`);
    i++;
  }
  return `<div class="hermes-kanban-md">${out.join('\n')}</div>`;
}

export function _kanbanFormatDuration(seconds){
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n < 60) return Math.round(n) + 's';
  if (n < 3600) return Math.round(n / 60) + 'm';
  if (n < 86400) return Math.round(n / 3600) + 'h';
  return Math.round(n / 86400) + 'd';
}

export function _kanbanTaskAge(task){
  const age = task && (task.age_seconds || task.age);
  if (Number.isFinite(Number(age))) return _kanbanFormatDuration(age);
  return '';
}

export function _kanbanCardStalenessClass(task){
  const age = Number(task && (task.age_seconds || task.age));
  const status = task && task.status;
  if (!Number.isFinite(age)) return '';
  if ((status === 'running' && age > 3600) || (status === 'blocked' && age > 86400)) return 'kanban-card-stale-red';
  if ((status === 'running' && age > 600) || (status === 'ready' && age > 3600) || (status === 'blocked' && age > 3600)) return 'kanban-card-stale-amber';
  return '';
}

export function _kanbanCardQuickActions(task){
  const id = esc(task.id || '');
  const status = task.status || '';
  const complete = status !== 'done' && status !== 'archived' ? `<button type="button" class="kanban-card-action" onclick="quickKanbanCardAction(event,'${id}','done')">${esc(t('kanban_card_complete'))}</button>` : '';
  const archive = status !== 'archived' ? `<button type="button" class="kanban-card-action danger" onclick="quickKanbanCardAction(event,'${id}','archived')">${esc(t('kanban_card_archive'))}</button>` : '';
  return `<div class="kanban-card-actions" onclick="event.stopPropagation()">${complete}${archive}</div>`;
}

export async function quickKanbanCardAction(event, taskId, status){
  if (event) event.stopPropagation();
  return updateKanbanTask(taskId, {status});
}

export function _kanbanSuppressNextCardClick(){
  state._kanbanSuppressCardClickUntil = Date.now() + 700;
}

export function dragKanbanTask(event, taskId){
  _kanbanSuppressNextCardClick();
  if (!event.dataTransfer) return;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', taskId);
}

export function finishKanbanDrag(event){
  if (event) _kanbanSuppressNextCardClick();
}

export function openKanbanCard(event, taskId){
  if (Date.now() < state._kanbanSuppressCardClickUntil) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    return false;
  }
  loadKanbanTask(taskId);
  return false;
}

export function allowKanbanDrop(event){
  // Don't accept drops into the 'running' column. Entering 'running' is owned
  // by the dispatcher/claim_task path (sets claim_lock + claim_expires +
  // started_at + worker_pid). A drag-drop would bypass that contract and the
  // bridge would reject the resulting PATCH with HTTP 400 anyway. Refuse the
  // drop visually so users see immediate feedback.
  const target = event.currentTarget;
  if (target && target.dataset && target.dataset.kanbanStatus === 'running') {
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
    return;
  }
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
}

export function clearKanbanDrop(event){
  if (event && event.currentTarget) event.currentTarget.classList.remove('drop-target');
}

export async function dropKanbanTask(event, status){
  _kanbanSuppressNextCardClick();
  event.preventDefault();
  event.stopPropagation();
  clearKanbanDrop(event);
  const taskId = event.dataTransfer ? event.dataTransfer.getData('text/plain') : '';
  if (taskId && status) await updateKanbanTask(taskId, {status}, {openDetail: false});
  _kanbanSuppressNextCardClick();
}
const KANBAN_UNASSIGNED_LANE = '__unassigned__';
export function _kanbanLaneKey(task){ return task && task.assignee ? String(task.assignee) : KANBAN_UNASSIGNED_LANE; }
export function _kanbanLaneLabel(lane){ return lane === KANBAN_UNASSIGNED_LANE ? t('kanban_unassigned') : lane; }

export function _kanbanLaneNames(columns){
  const names = new Set();
  columns.forEach(col => (col.tasks || []).forEach(task => names.add(_kanbanLaneKey(task))));
  const assigned = Array.from(names).filter(n => n !== KANBAN_UNASSIGNED_LANE).sort((a, b) => {
    if (a === 'default') return -1;
    if (b === 'default') return 1;
    return String(a).localeCompare(String(b));
  });
  if (names.has(KANBAN_UNASSIGNED_LANE)) assigned.push(KANBAN_UNASSIGNED_LANE);
  return assigned;
}

export function _kanbanRenderColumn(col){
  const tasks = col.tasks || [];
  return `<section class="kanban-column" data-status="${esc(col.name)}" data-kanban-status="${esc(col.name)}" ondragover="allowKanbanDrop(event)" ondragenter="event.currentTarget.classList.add('drop-target')" ondragleave="clearKanbanDrop(event)" ondrop="dropKanbanTask(event, '${esc(col.name)}')">
      <div class="kanban-column-head">
        <span>${esc(_kanbanColumnLabel(col.name))}</span>
        <span class="kanban-count">${tasks.length}</span>
      </div>
      <div class="kanban-column-body">
        ${tasks.length ? tasks.map(task => _kanbanCard(task, col.name)).join('') : `<div class="kanban-empty">${esc(t('kanban_empty'))}</div>`}
      </div>
    </section>`;
}

export function _kanbanRenderProfileLanes(columns){
  const lanes = _kanbanLaneNames(columns);
  if (!lanes.length) return columns.map(_kanbanRenderColumn).join('');
  return `<div class="kanban-profile-lanes">${lanes.map(lane => {
    const laneCols = columns.map(col => ({...col, tasks: (col.tasks || []).filter(task => _kanbanLaneKey(task) === lane)}));
    const count = laneCols.reduce((sum, col) => sum + (col.tasks || []).length, 0);
    const laneClass = lane === KANBAN_UNASSIGNED_LANE ? ' kanban-profile-lane-unassigned' : '';
    return `<section class="kanban-profile-lane${laneClass}" data-kanban-lane="${esc(lane)}"><header class="kanban-profile-lane-head"><span>${esc(_kanbanLaneLabel(lane))}</span><span class="kanban-count">${count}</span></header><div class="kanban-board kanban-board-in-lane">${laneCols.map(_kanbanRenderColumn).join('')}</div></section>`;
  }).join('')}</div>`;
}

export function _kanbanEmptyBoardHtml(){
  return `<div class="main-view-empty"><div class="main-view-empty-title">${esc(t('kanban_no_data'))}</div><div class="main-view-empty-sub">${esc(t('kanban_work_queue_hint'))}</div></div>`;
}

export function _kanbanHiddenByFiltersHtml(){
  return `<div class="main-view-empty"><div class="main-view-empty-title">${esc(t('kanban_tasks_hidden_by_filters'))}</div><div class="main-view-empty-sub"><button class="btn-link" onclick="clearKanbanFilters()">${esc(t('kanban_clear_filters'))}</button></div></div>`;
}

export function clearKanbanFilters(){
  const s = $('kanbanSearch'); if (s) s.value = '';
  const a = $('kanbanAssigneeFilter'); if (a) { a.value = ''; a.dataset.defaultValue = ''; }
  const te = $('kanbanTenantFilter'); if (te) { te.value = ''; te.dataset.defaultValue = ''; }
  const ai = $('kanbanIncludeArchived'); if (ai) ai.checked = false;
  const om = $('kanbanOnlyMine'); if (om) om.checked = false;
  loadKanban(true);
}

export function _kanbanRenderBoard(){
  const board = $('kanbanBoard');
  if (!board) return;
  if (!state._kanbanBoard || !state._kanbanBoard.columns) {
    board.innerHTML = _kanbanEmptyBoardHtml();
    return;
  }
  const columns = _kanbanVisibleTasks();
  const total = columns.reduce((n, col) => n + (col.tasks || []).length, 0);
  if ($('kanbanSummary')) $('kanbanSummary').textContent = String(t('kanban_visible_tasks')).replace('{0}', total);
  _kanbanRenderSidebar(columns);
  if (total === 0) {
    const unfilteredTotal = (state._kanbanBoard.columns || []).reduce((n, col) => n + (col.tasks || []).length, 0);
    board.innerHTML = unfilteredTotal > 0 ? _kanbanHiddenByFiltersHtml() : _kanbanEmptyBoardHtml();
    return;
  }
  board.innerHTML = state._kanbanLanesByProfile ? _kanbanRenderProfileLanes(columns) : columns.map(_kanbanRenderColumn).join('');
}

export function _kanbanCard(task, status){
  const priority = Number(task.priority || 0);
  const links = task.link_counts || {};
  const linkTotal = Number(links.parents || 0) + Number(links.children || 0);
  const comments = Number(task.comment_count || 0);
  const age = _kanbanTaskAge(task);
  const stale = _kanbanCardStalenessClass(task);
  const body = _kanbanTaskBody(task);
  const assignee = task.assignee ? `<span class="kanban-card-assignee">@${esc(task.assignee)}</span>` : `<span class="kanban-card-unassigned">${esc(t('kanban_unassigned'))}</span>`;
  return `<article class="kanban-card ${esc(stale)}" data-kanban-task-id="${esc(task.id)}" draggable="true" ondragstart="dragKanbanTask(event, '${esc(task.id)}')" ondragend="finishKanbanDrag(event)" onclick="return openKanbanCard(event, '${esc(task.id)}')" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();loadKanbanTask('${esc(task.id)}')}">
    <div class="kanban-card-topline"><span class="kanban-card-id">${esc(task.id || '')}</span>${priority ? `<span class="kanban-badge priority">P${priority}</span>` : ''}${task.tenant ? `<span class="kanban-badge tenant">${esc(task.tenant)}</span>` : ''}</div>
    <div class="kanban-card-title">${esc(_kanbanTaskTitle(task))}</div>
    ${body ? `<div class="kanban-card-body">${_kanbanRenderMarkdown(body)}</div>` : ''}
    <div class="kanban-card-meta">${assignee}${comments ? `<span class="kanban-card-metric">💬 ${comments}</span>` : ''}${linkTotal ? `<span class="kanban-card-metric">↔ ${linkTotal}</span>` : ''}${age ? `<span class="kanban-card-age">${esc(age)}</span>` : ''}</div>
    ${_kanbanCardQuickActions(task)}
  </article>`;
}

export async function hardRefreshWebUIClient(){
  try {
    if (navigator.serviceWorker) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
  } catch(_) {}
  try {
    if (window.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
  } catch(_) {}
  window.location.reload();
}

export function _normalizeWebUIVersion(value){
  if(!value) return '';
  const s=String(value).trim();
  if(!s) return '';
  // Suppress placeholder / non-version sentinels (case-insensitive) so a real
  // client version never "mismatches" against a server that couldn't detect its
  // own version. api/updates.py can emit 'unknown' (git describe failure in a
  // Docker/CI image); comparing a real version against 'unknown' would FALSELY
  // fire the stale-client banner. (Codex #5480 gate)
  const lower=s.toLowerCase();
  if(lower==='__webui_version__'||lower==='not detected'||lower==='unknown') return '';
  return s;
}

export function _currentWebUIBundleVersion(){
  try{
    const raw=window.__HERMES_WEBUI_BUNDLE_VERSION__;
    if(!raw) return '';
    let s=String(raw);
    try{ s=decodeURIComponent(s.replace(/\+/g,' ')); }catch(_){}
    return _normalizeWebUIVersion(s);
  }catch(_){ return ''; }
}

export function _showStaleWebUIClientBanner(clientVersion,serverVersion){
  const banner=document.getElementById('staleClientBanner');
  if(!banner) return;
  const msg=document.getElementById('staleClientMessage');
  const versions=document.getElementById('staleClientVersions');
  if(msg) msg.textContent='This tab is running a different WebUI version. Hard refresh to restore full functionality.';
  if(versions) versions.textContent='Running: '+clientVersion+' → Server: '+serverVersion;
  banner.style.display='flex';
}

export function checkWebUIVersionSkew(settings){
  try{
    if(!settings) return;
    const client=_currentWebUIBundleVersion();
    const server=_normalizeWebUIVersion(settings.webui_version);
    if(!client||!server) return;
    if(client===server) return;
    _showStaleWebUIClientBanner(client,server);
  }catch(_){}
}

export function _startWebUIVersionSkewMonitor(){
  let _pollTimer=null;
  function _isBannerVisible(){
    const banner=document.getElementById('staleClientBanner');
    return !!(banner&&banner.style.display==='flex');
  }
  function _check(){
    if(_isBannerVisible()) return;
    Promise.resolve().then(function(){ return api('/api/settings'); }).then(function(s){ checkWebUIVersionSkew(s); }).catch(function(){});
  }
  function _startPoll(){
    if(_pollTimer||document.hidden) return;
    _pollTimer=setInterval(function(){
      if(document.hidden){ clearInterval(_pollTimer); _pollTimer=null; return; }
      if(_isBannerVisible()){ clearInterval(_pollTimer); _pollTimer=null; return; }
      _check();
    },60000);
  }
  _check();
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden){ _check(); _startPoll(); }
    else if(_pollTimer){ clearInterval(_pollTimer); _pollTimer=null; }
  });
  window.addEventListener('focus',function(){ _check(); });
  _startPoll();
}
_startWebUIVersionSkewMonitor();

export function _kanbanLooksLikeStaleClientError(err){
  const msg = String((err && err.message) || err || '').toLowerCase();
  return !!(err && err.status === 404 && (
    msg === 'not found' ||
    msg.includes('unknown kanban endpoint') ||
    msg.includes('stale cached bundle')
  ));
}

export function _kanbanUnavailableHtml(err){
  const raw = String((err && err.message) || err || '');
  if (_kanbanLooksLikeStaleClientError(err)) {
    return `<div class="main-view-empty"><div class="main-view-empty-title">Kanban needs a hard refresh</div><div class="main-view-empty-subtitle">The server rejected an obsolete Kanban endpoint. This usually means the browser or Mac app is still running a stale cached WebUI bundle after an update.</div><button class="btn primary" type="button" onclick="hardRefreshWebUIClient()">${esc(t('update_hard_refresh_now')||'Hard refresh now')}</button><div class="main-view-empty-subtitle">Original error: ${esc(raw || 'not found')}</div></div>`;
  }
  const msg = `${esc(t('kanban_unavailable'))}: ${esc(raw)}`;
  return `<div class="main-view-empty"><div class="main-view-empty-title">${msg}</div></div>`;
}

export async function loadKanban(animate){
  const board = $('kanbanBoard');
  const list = $('kanbanList');
  try {
    if (animate && board) board.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:13px">${esc(t('loading'))}</div>`;
    // Resolve the active board before board-scoped requests. If another CLI or
    // tab archived the previous board, /boards can fall back to default instead
    // of leaving config/board pinned to a ghost slug.
    await loadKanbanBoards();
    const config = await api('/api/kanban/config' + _kanbanBoardQuery());
    let assignees = null;
    try { assignees = await api('/api/kanban/assignees' + _kanbanBoardQuery()); } catch(e) { assignees = null; }
    _kanbanApplyConfigDefaults(config);
    const filters = _kanbanCurrentFilters();
    const params = new URLSearchParams();
    if (filters.assignee) params.set('assignee', filters.assignee);
    if (filters.tenant) params.set('tenant', filters.tenant);
    if (filters.includeArchived) params.set('include_archived', '1');
    if (filters.onlyMine) params.set('only_mine', '1');
    if (state._kanbanCurrentBoard) params.set('board', state._kanbanCurrentBoard);
    const path = '/api/kanban/board' + (params.toString() ? '?' + params.toString() : '');
    const data = await api(path);
    if (data && data.changed === false && state._kanbanBoard) { _kanbanRenderBoard(); return; }
    state._kanbanBoard = data || {columns: []};
    if ((!state._kanbanBoard.columns || !state._kanbanBoard.columns.length) && config && config.columns) {
      state._kanbanBoard.columns = config.columns.map(name => ({name, tasks: []}));
    }
    state._kanbanLatestEventId = Number(state._kanbanBoard.latest_event_id || 0);
    // Toggle the "Read-only view" banner based on the bridge's read_only flag.
    // Bridge sets read_only=true only when the kanban_db connection cannot accept
    // writes (e.g. dispatcher contention or library missing). Hide otherwise.
    try {
      const ro = document.querySelector('.kanban-readonly');
      if (ro) ro.style.display = state._kanbanBoard.read_only ? '' : 'none';
    } catch(_) {}
    _kanbanSetSelectOptions($('kanbanAssigneeFilter'), state._kanbanBoard.assignees || (assignees && assignees.assignees) || (config && config.assignees), 'kanban_all_assignees');
    _kanbanSetSelectOptions($('kanbanTenantFilter'), state._kanbanBoard.tenants, 'kanban_all_tenants');
    await loadKanbanStats();
    // Note: PR #1828 (v0.51.20) moved the boards refresh to the start of
    // loadKanban() so the active board is resolved BEFORE board-scoped
    // requests fire. The previous tail-of-function refresh has been removed
    // to avoid doubling /api/kanban/boards traffic during SSE-driven
    // refreshes (debounced at 250ms via _scheduleKanbanRefresh). The
    // 30-second poll started by _kanbanStartPolling() picks up any board
    // state changes that arrive after this render.
    _kanbanStartPolling();
    _kanbanRenderBoard();
  } catch(e) {
    const html = _kanbanUnavailableHtml(e);
    if (board) board.innerHTML = html;
    if (list) list.innerHTML = html;
  }
}

export function filterKanban(){ _kanbanRenderBoard(); }

export async function loadKanbanStats(){
  try {
    const stats = await api('/api/kanban/stats' + _kanbanBoardQuery());
    const el = $('kanbanStats');
    if (!el) return;
    const byStatus = (stats && stats.by_status) || {};
    const total = Object.values(byStatus).reduce((a, b) => a + Number(b || 0), 0);
    const cells = Object.entries(byStatus).sort(([a], [b]) => a.localeCompare(b)).map(([status, count]) =>
      `<span class="kanban-stat-cell"><strong>${esc(String(count))}</strong> ${esc(_kanbanColumnLabel(status))}</span>`
    ).join('');
    el.innerHTML = `<div class="kanban-stats-grid"><span class="kanban-stat-cell total"><strong>${esc(String(total))}</strong> ${esc(t('kanban_stats'))}</span>${cells}</div>`;
  } catch(e) { /* stats are best-effort */ }
}

export async function refreshKanbanEvents(){
  if (state._currentPanel !== 'kanban' || !state._kanbanLatestEventId) return;
  try {
    const eventsEndpoint = '/api/kanban/events';
    const events = await api(eventsEndpoint + _kanbanBoardQuery({since: state._kanbanLatestEventId}));
    if (events && Array.isArray(events.events) && events.events.length) {
      state._kanbanLatestEventId = Number(events.latest_event_id || events.cursor || state._kanbanLatestEventId);
      await loadKanban(true);
      if (state._kanbanCurrentTaskId && events.events.some(ev => ev.task_id === state._kanbanCurrentTaskId)) await loadKanbanTask(state._kanbanCurrentTaskId);
    }
  } catch(e) { /* polling should not spam toasts */ }
}

export function _kanbanStartPolling(){
  // Prefer SSE for low-latency live updates. Fall back to polling on
  // browsers without EventSource or after repeated stream failures.
  if (typeof EventSource === 'undefined' || state._kanbanEventSourceFailures >= 3) {
    if (state._kanbanPollTimer) return;
    state._kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    return;
  }
  _kanbanStartEventStream();
}

export function _kanbanStopPolling(){
  if (state._kanbanPollTimer) { clearInterval(state._kanbanPollTimer); state._kanbanPollTimer = null; }
  if (state._kanbanEventSource) { try { if(state._kanbanEventSource.readyState!==2)state._kanbanEventSource.close(); } catch(_) {} state._kanbanEventSource = null; }
}

export function _kanbanStartEventStream(){
  // Tear down any prior stream before opening a new one (board switch,
  // login change, etc.).
  if (state._kanbanEventSource) { try { if(state._kanbanEventSource.readyState!==2)state._kanbanEventSource.close(); } catch(_) {} state._kanbanEventSource = null; }
  const since = Number(state._kanbanLatestEventId || 0);
  let url = '/api/kanban/events/stream' + _kanbanBoardQuery({since: since});
  let es;
  try {
    es = new EventSource(url);
  } catch(e) {
    state._kanbanEventSourceFailures += 1;
    if (state._kanbanEventSourceFailures < 3 && !state._kanbanPollTimer) {
      state._kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    }
    return;
  }
  state._kanbanEventSource = es;
  es.addEventListener('hello', (ev) => {
    // Reset the failure counter on a successful handshake.
    state._kanbanEventSourceFailures = 0;
  });
  es.addEventListener('events', async (ev) => {
    if (state._currentPanel !== 'kanban') return;  // ignore while user is on another panel
    let data;
    try { data = JSON.parse(ev.data); } catch(_) { return; }
    if (!data || !Array.isArray(data.events) || !data.events.length) return;
    state._kanbanLatestEventId = Number(data.cursor || state._kanbanLatestEventId);
    // Re-fetch the board so the visual state reflects the new events.
    // Throttle: if events are arriving faster than ~1/sec we coalesce.
    _scheduleKanbanRefresh(data.events);
  });
  es.onerror = () => {
    state._kanbanEventSourceFailures += 1;
    if (state._kanbanEventSourceFailures >= 3) {
      // Give up on SSE for this session — fall back to HTTP polling.
      try { es.close(); } catch(_) {}
      state._kanbanEventSource = null;
      if (!state._kanbanPollTimer) state._kanbanPollTimer = setInterval(refreshKanbanEvents, 30000);
    }
    // EventSource auto-reconnects under the hood; nothing more to do here
    // until we hit the failure limit.
  };
}
