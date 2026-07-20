import { state } from "./state.js";
import { switchPanel } from "./core.js";
import { _kanbanRenderMarkdown,_kanbanTaskTitle,loadKanban } from "./kanban-board.js";
import { addKanbanDependency,loadKanbanTask,openKanbanCreateBoard,removeKanbanDependency,submitKanbanTaskModal } from "./kanban-boards.js";

// Panels domain: kanban events and task editing

export function _scheduleKanbanRefresh(events){
  for (const ev of events) {
    if (ev && ev.task_id) state._kanbanRefreshPendingTaskIds.add(ev.task_id);
  }
  if (state._kanbanRefreshScheduled) return;
  state._kanbanRefreshScheduled = true;
  // 250ms debounce — keeps a burst of N events from triggering N reloads.
  setTimeout(async () => {
    state._kanbanRefreshScheduled = false;
    const taskIds = Array.from(state._kanbanRefreshPendingTaskIds);
    state._kanbanRefreshPendingTaskIds.clear();
    if (state._currentPanel !== 'kanban') return;
    try {
      await loadKanban(true);
      if (state._kanbanCurrentTaskId && taskIds.includes(state._kanbanCurrentTaskId)) {
        await loadKanbanTask(state._kanbanCurrentTaskId);
      }
    } catch(_) { /* swallow — SSE refresh shouldn't toast */ }
  }, 250);
}

// Build a "?board=<slug>" or "?since=N&board=<slug>" query string fragment
// based on the active board. Empty when the user is on the default board
// AND nobody has explicitly switched (so we don't pin to "default" and
// override a hypothetical server-side switch).
export function _kanbanBoardQuery(extra){
  const params = new URLSearchParams();
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v !== null && v !== undefined && v !== '') params.set(k, String(v));
    }
  }
  if (state._kanbanCurrentBoard) params.set('board', state._kanbanCurrentBoard);
  const s = params.toString();
  return s ? '?' + s : '';
}

export async function nudgeKanbanDispatcher(){
  if (state._kanbanIsDispatching) return;
  // Dry-run dispatch: show what WOULD be spawned, without actually spawning
  // workers.  Uses ?dry_run=1 so the dispatcher reports its plan without
  // mutating the board.  The result shape includes spawned/skipped_unassigned/
  // skipped_nonspawnable/promoted/auto_blocked so users can diagnose why a
  // Ready task isn't being picked up before they commit to a real run.
  state._kanbanIsDispatching = true;
  _setKanbanDispatcherButtonsDisabled(true);
  try {
    const dispatchEndpoint = '/api/kanban/dispatch';
    const result = await api(
      dispatchEndpoint + '?dry_run=1&max=8' + (state._kanbanCurrentBoard ? '&board=' + encodeURIComponent(state._kanbanCurrentBoard) : ''),
      {method: 'POST'},
    );
    showToast(_kanbanFormatDispatchResult(result, true), 'info', 6000);
    await loadKanban(true);
  } catch(e) {
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  } finally {
    state._kanbanIsDispatching = false;
    _setKanbanDispatcherButtonsDisabled(false);
  }
}

export async function runKanbanDispatcher(){
  if (state._kanbanIsDispatching) return;
  // Real dispatch: claims Ready tasks and spawns worker subprocesses
  // (one `hermes -p <assignee>` per claimed row, up to max=8 per call).
  // Confirmation dialog first because this actually consumes API budget on
  // each spawned worker.  Result toast surfaces what happened so users see
  // the dispatcher actually doing work.

  state._kanbanIsDispatching = true;
  _setKanbanDispatcherButtonsDisabled(true);
  try {
    const ok = await showConfirmDialog({
      title: t('kanban_run_dispatcher') || 'Run dispatcher',
      message: t('kanban_run_dispatcher_confirm')
        || 'This will claim Ready tasks on this board and spawn worker subprocesses (one per task, up to 8 per click). Continue?',
      confirmLabel: t('kanban_run_dispatcher') || 'Run dispatcher',
    });
    if (!ok) return;
    const dispatchEndpoint = '/api/kanban/dispatch';
    const result = await api(
      dispatchEndpoint + '?max=8' + (state._kanbanCurrentBoard ? '&board=' + encodeURIComponent(state._kanbanCurrentBoard) : ''),
      {method: 'POST'},
    );
    showToast(_kanbanFormatDispatchResult(result, false), 'info', 8000);
    await loadKanban(true);
  } catch(e) {
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  } finally {
    state._kanbanIsDispatching = false;
    _setKanbanDispatcherButtonsDisabled(false);
  }
}

export function _setKanbanDispatcherButtonsDisabled(disabled){
  document.querySelectorAll('.kanban-run-dispatch-btn, .kanban-nudge-dispatch-btn').forEach((btn) => {
    btn.disabled = !!disabled;
    btn.classList.toggle('disabled', !!disabled);
  });
}

export function _kanbanFormatDispatchResult(result, dryRun){
  // Produce a human-readable one-line summary of dispatch_once's output so
  // users can see exactly what happened rather than a generic "OK" toast.
  const r = result || {};
  const spawned = (r.spawned || []).length;
  const promoted = r.promoted || 0;
  const reclaimed = r.reclaimed || 0;
  const skippedUnassigned = (r.skipped_unassigned || []).length;
  const skippedNonspawnable = (r.skipped_nonspawnable || []).length;
  const autoBlocked = (r.auto_blocked || []).length;
  const timedOut = (r.timed_out || []).length;
  const crashed = (r.crashed || []).length;
  const verb = dryRun ? (t('kanban_dispatch_preview_prefix') || 'Preview:') : (t('kanban_dispatch_run_prefix') || 'Dispatched:');
  const parts = [];
  parts.push(spawned + ' ' + (t('kanban_dispatch_spawned') || 'spawned'));
  if (promoted) parts.push(promoted + ' ' + (t('kanban_dispatch_promoted') || 'promoted'));
  if (reclaimed) parts.push(reclaimed + ' ' + (t('kanban_dispatch_reclaimed') || 'reclaimed'));
  if (skippedUnassigned) parts.push(skippedUnassigned + ' ' + (t('kanban_dispatch_skipped_unassigned') || 'skipped (no assignee)'));
  if (skippedNonspawnable) parts.push(skippedNonspawnable + ' ' + (t('kanban_dispatch_skipped_nonspawnable') || 'skipped (unknown profile)'));
  if (autoBlocked) parts.push(autoBlocked + ' ' + (t('kanban_dispatch_auto_blocked') || 'auto-blocked'));
  if (timedOut) parts.push(timedOut + ' ' + (t('kanban_dispatch_timed_out') || 'timed out'));
  if (crashed) parts.push(crashed + ' ' + (t('kanban_dispatch_crashed') || 'crashed'));
  return verb + ' ' + parts.join(', ');
}

export function _kanbanSelectedTaskIds(){
  const selected = Array.from(document.querySelectorAll('.kanban-card.selected')).map(card => card.dataset.kanbanTaskId).filter(Boolean);
  return selected.length ? selected : (state._kanbanCurrentTaskId ? [state._kanbanCurrentTaskId] : []);
}

export async function bulkUpdateKanban(){
  const ids = _kanbanSelectedTaskIds();
  const status = $('kanbanBulkStatus') ? $('kanbanBulkStatus').value : '';
  if (!ids.length || !status) return;
  try {
    await api('/api/kanban/tasks/bulk' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({ids, status})});
    showToast(t('kanban_bulk_action'));
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export async function blockKanbanTask(taskId){
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/block' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({reason: 'blocked from WebUI'})});
    await loadKanbanTask(taskId);
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export async function unblockKanbanTask(taskId){
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/unblock' + _kanbanBoardQuery(), {method: 'POST', body: JSON.stringify({})});
    await loadKanbanTask(taskId);
    await loadKanban(true);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export function closeKanbanTaskDetail(){
  state._kanbanCurrentTaskId = null;
  const preview = $('kanbanTaskPreview');
  if (preview) {
    preview.style.display = 'none';
    preview.innerHTML = '';
  }
  const board = $('kanbanBoard');
  if (board) board.querySelectorAll('.kanban-card').forEach(card => card.classList.remove('selected'));
}

export function _kanbanFormatTimestamp(value){
  if (value === undefined || value === null || value === '') return '';
  let date = null;
  if (typeof value === 'number') date = new Date(value > 100000000000 ? value : value * 1000);
  else if (/^\d+(?:\.\d+)?$/.test(String(value).trim())) {
    const n = Number(value);
    date = new Date(n > 100000000000 ? n : n * 1000);
  } else {
    date = new Date(value);
  }
  if (!date || Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleString(); } catch(e) { return date.toISOString(); }
}

export function _kanbanEventSummary(event){
  const kind = event.kind || event.type || 'event';
  const payload = event.payload || event.data || {};
  if (payload && typeof payload === 'object') {
    const parts = [];
    if (payload.status) parts.push(String(payload.status));
    if (payload.reason) parts.push(String(payload.reason));
    if (payload.summary) parts.push(String(payload.summary));
    if (payload.fields && Array.isArray(payload.fields)) parts.push(payload.fields.join(', '));
    if (parts.length) return `${kind}: ${parts.join(' · ')}`;
  }
  return String(kind);
}

export function _kanbanFormatDetailValue(value){
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value, null, 2); } catch(e) { return String(value); }
  }
  return String(value);
}

export function _kanbanDetailSection(cls, title, inner, emptyKey){
  const content = inner || `<div class="kanban-detail-empty">${esc(t(emptyKey))}</div>`;
  return `<section class="kanban-detail-section ${cls}">
    <h3>${esc(title)}</h3>
    ${content}
  </section>`;
}

export function _kanbanCommentHtml(comment){
  const body = comment.body || comment.text || comment.content || '';
  const by = comment.author || comment.created_by || comment.actor || '';
  const at = _kanbanFormatTimestamp(comment.created_at || comment.ts || '');
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${_kanbanRenderMarkdown(body)}</div>
    <div class="kanban-detail-row-meta">${esc([by, at].filter(Boolean).join(' · '))}</div>
  </div>`;
}

export function _kanbanEventHtml(event){
  const at = _kanbanFormatTimestamp(event.created_at || event.ts || '');
  const payload = _kanbanFormatDetailValue(event.payload || event.data || '');
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${esc(_kanbanEventSummary(event))}</div>
    ${payload ? `<pre class="kanban-detail-pre">${esc(payload)}</pre>` : ''}
    <div class="kanban-detail-row-meta">${esc(at)}</div>
  </div>`;
}

export function _kanbanRunHtml(run){
  const status = run.status || run.state || run.result || '';
  const label = run.run_id || run.id || run.worker || t('kanban_task');
  const started = _kanbanFormatTimestamp(run.started_at || run.created_at || '');
  const finished = _kanbanFormatTimestamp(run.finished_at || run.completed_at || '');
  const detail = run.error || run.summary || run.log_tail || '';
  return `<div class="kanban-detail-row">
    <div class="kanban-detail-row-main">${esc(label)}${status ? ` · ${esc(status)}` : ''}</div>
    ${detail ? `<pre class="kanban-detail-pre">${esc(_kanbanFormatDetailValue(detail))}</pre>` : ''}
    <div class="kanban-detail-row-meta">${esc([started, finished].filter(Boolean).join(' → '))}</div>
  </div>`;
}

export function _kanbanJsArg(s){
  // Encode a value for safe interpolation inside an inline on* handler's JS
  // string literal. JSON.stringify quotes/escapes for JS context; esc() then
  // makes it safe inside the HTML attribute. Without this, a task id containing
  // a quote breaks out of the handler (esc() alone is HTML-escaping, which the
  // browser decodes BEFORE executing the inline handler). (#3797)
  return esc(JSON.stringify(String(s == null ? '' : s)));
}
export function _kanbanLinkableTaskOptions(excludeId){
  // Datalist of existing task ids (with title as the option label) so the
  // dependency field is a pick-from-real-tasks autocomplete rather than a blind
  // free-text opaque-id box. Mirrors the tenant datalist pattern.
  const cols = (state._kanbanBoard && state._kanbanBoard.columns) || [];
  const seen = new Set();
  const opts = [];
  for (const col of cols) {
    for (const task of (col.tasks || [])) {
      const id = task && task.id;
      if (!id || id === excludeId || seen.has(id)) continue;
      seen.add(id);
      opts.push(`<option value="${esc(id)}">${esc(_kanbanTaskTitle(task))}</option>`);
    }
  }
  return opts.join('');
}
export function _kanbanLinksHtml(links){
  const parents = (links && links.parents) || [];
  const children = (links && links.children) || [];
  const taskId = state._kanbanCurrentTaskId;
  const item = (id, isParent) => {
    const parentId = isParent ? id : taskId;
    const childId = isParent ? taskId : id;
    return `<code>${esc(id)} <button class="btn mini" onclick="removeKanbanDependency(${_kanbanJsArg(parentId)},${_kanbanJsArg(childId)})" data-i18n="kanban_remove_dependency" title="${esc(t('kanban_remove_dependency') || 'Remove')}">✕</button></code>`;
  };
  const hasLinks = parents.length || children.length;
  return `<div class="kanban-detail-links-section">
    ${hasLinks ? `<div class="kanban-detail-links-grid">
      <div><strong>${esc(t('kanban_parents'))}</strong><div>${parents.length ? parents.map(id => item(id, true)).join(' ') : esc(t('kanban_empty'))}</div></div>
      <div><strong>${esc(t('kanban_children'))}</strong><div>${children.length ? children.map(id => item(id, false)).join(' ') : esc(t('kanban_empty'))}</div></div>
    </div>` : ''}
    <div class="kanban-detail-links-controls">
      <input type="text" id="kanbanDependencyInput" class="kanban-detail-links-input" list="kanbanDependencyOptions" maxlength="255" autocomplete="off" data-i18n-placeholder="kanban_dependency_placeholder" placeholder="Task ID to link">
      <datalist id="kanbanDependencyOptions">${_kanbanLinkableTaskOptions(taskId)}</datalist>
      <button class="btn secondary" onclick="addKanbanDependency(${_kanbanJsArg(taskId)})" data-i18n="kanban_add_dependency">Add dependency</button>
    </div>
  </div>`;
}

export async function createKanbanTask(){
  const input = document.getElementById('kanbanNewTaskTitle');
  const title = input ? input.value.trim() : '';
  if (!title) {
    // Empty inline input (or a click on the panel-head "+" via openKanbanCreate)
    // — open the full create-task modal so the user has somewhere obvious to
    // type and configure the task. Mirrors the cron / skills pattern of routing
    // header "+" clicks through to a clearly-modal create surface.
    openKanbanCreate();
    return;
  }
  try {
    const created = await api('/api/kanban/tasks' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({title}),
    });
    if (input) input.value = '';
    await loadKanban(true);
    if (created && created.task && created.task.id) await loadKanbanTask(created.task.id);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}
// ────────────────────────────────────────────────────────────────────────────
// Kanban: create-task modal (panel-head "+" button entry point).
//
// Same `.kanban-modal-overlay` shell as openKanbanCreateBoard() so the two
// flows look and behave identically (centered card, dim backdrop, ESC closes,
// click-on-backdrop closes). The modal markup lives in static/index.html as
// #kanbanTaskModal — see the section just above </body>. Submit hits the
// existing /api/kanban/tasks POST endpoint (which already accepts title, body,
// assignee, tenant, priority, status — see api/kanban_bridge.py:306).
// ────────────────────────────────────────────────────────────────────────────

// ────────────────────────────────────────────────────────────────────────────
// Kanban: create-task / edit-task modal (panel-head "+" + task-detail Edit
// button entry points).
//
// Single modal serves both flows.  Title + submit-button labels and the
// underlying submit verb (POST vs PATCH) flip based on `state._kanbanTaskModalMode`.
//
// Same `.kanban-modal-overlay` shell as openKanbanCreateBoard() so the two
// flows look and behave identically (centered card, dim backdrop, ESC closes,
// click-on-backdrop closes). The modal markup lives in static/index.html as
// #kanbanTaskModal — see the section just above </body>.
//
// The assignee field auto-completes against the union of (a) live Hermes
// profile names from /api/profiles and (b) historical assignees on the
// active board, with an inline hint that explains the dispatcher claim
// contract — most users will pick a profile name from the dropdown rather
// than type one.
// ────────────────────────────────────────────────────────────────────────────

const _KANBAN_PROFILE_NAMES_CACHE_TTL_MS = 30000;
export function _invalidateKanbanProfileCache() {
  state._kanbanProfileNamesCache = null;
  state._kanbanProfileNamesCacheAt = 0;
}
// Status the modal *displayed* on edit-mode open.  If the user doesn't touch
// the dropdown, we must NOT send `status` in the PATCH payload — otherwise
// editing a task whose real status is non-editable in this dropdown
// (running/blocked/done/archived → mapped to 'triage' for display) would
// silently demote the task on save.  See the regression caught during PR
// review: editing a 'running' task without touching status was reclaiming
// the worker and moving the task back to triage.

export async function _kanbanLoadProfileNames(){
  // Hit /api/profiles once per session and cache for a short TTL.
  // Returns an array of profile names (sorted, default first if present).
  const hasFreshCache = (
    Array.isArray(state._kanbanProfileNamesCache) &&
    (Date.now() - state._kanbanProfileNamesCacheAt) < _KANBAN_PROFILE_NAMES_CACHE_TTL_MS
  );
  if (hasFreshCache) return state._kanbanProfileNamesCache;
  try {
    const data = await api('/api/profiles');
    const profiles = Array.isArray(data && data.profiles) ? data.profiles : [];
    const names = profiles.map(p => p && p.name).filter(Boolean);
    // Stable order: default first, then alphabetical.
    names.sort((a, b) => {
      if (a === 'default') return -1;
      if (b === 'default') return 1;
      return a.localeCompare(b);
    });
    state._kanbanProfileNamesCache = names;
    state._kanbanProfileNamesCacheAt = Date.now();
    return names;
  } catch(_) {
    state._kanbanProfileNamesCache = [];
    state._kanbanProfileNamesCacheAt = Date.now();
    return [];
  }
}

export async function _kanbanPopulateAssigneeSelect(currentValue){
  const sel = document.getElementById('kanbanTaskModalAssignee');
  if (!sel) return;
  // Profile names: the canonical set the dispatcher can claim.
  const profileNames = await _kanbanLoadProfileNames();
  // Historical assignees from the active board: include them so users who
  // assigned to a CLI lane (e.g. orion-cc) before still see those values.
  const historicalAssignees = (state._kanbanBoard && Array.isArray(state._kanbanBoard.assignees))
    ? state._kanbanBoard.assignees
    : [];
  // Build a final ordered list, deduping.  Profiles come first, then any
  // historical assignees that aren't profiles (rare but keeps round-tripping
  // correct for tasks created via CLI).
  const seen = new Set();
  const profiles = [];
  for (const name of profileNames) {
    if (!seen.has(name)) { profiles.push(name); seen.add(name); }
  }
  const extras = [];
  for (const name of historicalAssignees) {
    if (name && !seen.has(name)) { extras.push(name); seen.add(name); }
  }
  // If the current value isn't in either bucket (e.g. an old CLI-created
  // assignee that's since been deleted), preserve it as a final option so
  // editing the task doesn't silently change its assignee.
  if (currentValue && !seen.has(currentValue)) {
    extras.push(currentValue);
    seen.add(currentValue);
  }
  // The empty value maps to null on submit (intentionally unassigned).  Keep
  // it last so the default-selected option is the first profile, not "no one".
  let html = '';
  if (profiles.length) {
    html += `<optgroup label="${esc(t('kanban_assignee_profiles_label') || 'Hermes profiles')}">`;
    html += profiles.map(v => `<option value="${esc(v)}"${v === currentValue ? ' selected' : ''}>${esc(v)}</option>`).join('');
    html += '</optgroup>';
  }
  if (extras.length) {
    html += `<optgroup label="${esc(t('kanban_assignee_other_label') || 'Other (CLI lanes / removed profiles)')}">`;
    html += extras.map(v => `<option value="${esc(v)}"${v === currentValue ? ' selected' : ''}>${esc(v)}</option>`).join('');
    html += '</optgroup>';
  }
  // Final "no assignee" fallthrough — explicit so users know what they're choosing.
  html += `<option value=""${(!currentValue) ? ' selected' : ''}>${esc(t('kanban_assignee_unassigned') || '— Unassigned (won\u2019t auto-run) —')}</option>`;
  sel.innerHTML = html;
}

export function openKanbanCreate(){
  // Make sure the user is on the kanban panel so the resulting board reload is
  // visible behind the modal.
  if (typeof switchPanel === 'function' && state._currentPanel !== 'kanban') switchPanel('kanban');
  const modal = document.getElementById('kanbanTaskModal');
  if (!modal) return;
  state._kanbanTaskModalMode = 'create';
  state._kanbanTaskModalEditingId = null;
  state._kanbanTaskModalInitialDisplayedStatus = null;  // create mode: always send status
  // Default new tasks to "ready" so they're immediately claimable by the
  // dispatcher (assuming the user picks an assignee).  Triage is for staging
  // tasks that need human review before being marked actionable; users who
  // want it can still pick it from the status dropdown.
  _kanbanResetTaskModalFields({status: 'ready'});
  _kanbanSetTaskModalStatusHint(null);
  _kanbanSetTaskModalLabels('create');
  _kanbanPopulateAssigneeSelect('').then(() => {
    // After the dropdown is populated, default-select the first profile (not
    // the "Unassigned" fallthrough).  This is the right hint: most users want
    // to assign to *something* — they can pick "Unassigned" deliberately.
    const sel = document.getElementById('kanbanTaskModalAssignee');
    if (sel && sel.options.length > 0 && sel.value === '') {
      const firstProfile = Array.from(sel.options).find(opt => opt.value !== '');
      if (firstProfile) sel.value = firstProfile.value;
    }
  });
  _kanbanPopulateTenantDatalist();
  _kanbanPopulateWorkspacePathDatalist();
  _kanbanPopulateParentsDatalist();
  modal.hidden = false;
  if (state._kanbanTaskModalFocusCleanup) {
    state._kanbanTaskModalFocusCleanup();
    state._kanbanTaskModalFocusCleanup = null;
  }
  state._kanbanTaskModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => {
    const titleEl = document.getElementById('kanbanTaskModalTitleInput');
    if (titleEl) titleEl.focus();
  }, 50);
  document.addEventListener('keydown', _kanbanTaskModalKey);
}

export async function openKanbanEdit(taskId){
  // Triggered by the Edit button on the task detail view.  Fetches the task
  // (rather than relying on whatever's cached locally) so the modal always
  // reflects authoritative server state.
  if (!taskId) return;
  if (typeof switchPanel === 'function' && state._currentPanel !== 'kanban') switchPanel('kanban');
  const modal = document.getElementById('kanbanTaskModal');
  if (!modal) return;
  let task = null;
  try {
    const data = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery());
    task = data && data.task;
  } catch(e) {
    showToast((t('kanban_unavailable') || 'Kanban unavailable') + ': ' + (e.message || e), 'error');
    return;
  }
  if (!task) return;
  state._kanbanTaskModalMode = 'edit';
  state._kanbanTaskModalEditingId = task.id;
  // Track the displayed status so submitKanbanTaskModal can detect whether
  // the user actually picked a new value vs. the dropdown's mapped default.
  // Without this, editing a 'running'/'blocked'/'done'/'archived' task whose
  // real status maps to 'triage' for display would silently demote the task
  // (the mapped 'triage' would land in the PATCH payload, and _patch_task
  // would call _set_status_direct → reclaim worker → move to triage).
  const initialDisplayedStatus = _kanbanEditableStatusFor(task.status);
  const originalStatus = task.status || initialDisplayedStatus;
  state._kanbanTaskModalInitialDisplayedStatus = initialDisplayedStatus;
  _kanbanResetTaskModalFields({
    title: task.title || '',
    body: task.body || '',
    status: initialDisplayedStatus,
    tenant: task.tenant || '',
    priority: typeof task.priority === 'number' ? task.priority : 0,
  });
  // Populate the assignee select AFTER reset so the option exists when we
  // call sel.value = currentAssignee.
  await _kanbanPopulateAssigneeSelect(task.assignee || '');
  _kanbanSetTaskModalStatusHint(originalStatus, initialDisplayedStatus);
  _kanbanSetTaskModalLabels('edit');
  _kanbanPopulateTenantDatalist();
  modal.hidden = false;
  if (state._kanbanTaskModalFocusCleanup) {
    state._kanbanTaskModalFocusCleanup();
    state._kanbanTaskModalFocusCleanup = null;
  }
  state._kanbanTaskModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => {
    const titleEl = document.getElementById('kanbanTaskModalTitleInput');
    if (titleEl) { titleEl.focus(); titleEl.select(); }
  }, 50);
  document.addEventListener('keydown', _kanbanTaskModalKey);
}

export function _kanbanEditableStatusFor(status){
  // The modal's status select only offers triage/todo/ready (the user-writable
  // states).  blocked/running/done/archived are reached via the detail-view
  // status buttons or the dispatcher.  Map non-editable states to a sensible
  // default so the user can still change them via the buttons after saving.
  const editable = new Set(['triage', 'todo', 'ready']);
  return editable.has(status) ? status : 'triage';
}

export function _kanbanResetTaskModalFields(values){
  const v = values || {};
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.value = (val == null ? '' : String(val));
  };
  set('kanbanTaskModalTitleInput', v.title || '');
  set('kanbanTaskModalBody', v.body || '');
  set('kanbanTaskModalStatus', v.status || 'triage');
  // Assignee handled separately by _kanbanPopulateAssigneeSelect() because
  // it's a <select> populated from /api/profiles + board history; setting
  // .value before the options exist would silently fail.
  set('kanbanTaskModalTenant', v.tenant || '');
  set('kanbanTaskModalPriority', v.priority != null ? v.priority : 0);
  set('kanbanTaskModalSkills', Array.isArray(v.skills) ? v.skills.join(', ') : (v.skills || ''));
  set('kanbanTaskModalMaxRuntimeSeconds', v.max_runtime_seconds != null ? v.max_runtime_seconds : '');
  set('kanbanTaskModalParents', '');
  const errEl = document.getElementById('kanbanTaskModalError');
  if (errEl) { errEl.textContent = ''; delete errEl.dataset.warningShown; }
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  if (submitBtn) submitBtn.disabled = false;
}

export function _kanbanSetTaskModalLabels(mode){
  const titleH = document.getElementById('kanbanTaskModalTitle');
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  if (mode === 'edit') {
    if (titleH) titleH.textContent = t('kanban_edit_task') || 'Edit task';
    if (submitBtn) submitBtn.textContent = t('save') || 'Save';
  } else {
    if (titleH) titleH.textContent = t('kanban_new_task') || 'New task';
    if (submitBtn) submitBtn.textContent = t('create') || 'Create';
  }
  // Workspace and new backend fields are create-only; backend patch doesn't handle them.
  const createOnlyIds = [
    'kanbanTaskModalWorkspaceKind',
    'kanbanTaskModalWorkspacePath',
    'kanbanTaskModalSkills',
    'kanbanTaskModalMaxRuntimeSeconds',
    'kanbanTaskModalParents',
  ];
  const disabled = mode === 'edit';
  for (const id of createOnlyIds) {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  }
}

export function _kanbanSetTaskModalStatusHint(realStatus, editableStatus){
  const hintEl = document.getElementById('kanbanTaskModalStatusOriginalHint');
  if (!hintEl) return;
  if (!realStatus || realStatus === editableStatus) {
    hintEl.hidden = true;
    hintEl.textContent = '';
    return;
  }
  const statusLabel = t(`kanban_status_${realStatus}`) || realStatus;
  hintEl.textContent = String(t('kanban_status_original_hint')).replace('{0}', statusLabel);
  hintEl.hidden = false;
}

export function _kanbanPopulateTenantDatalist(){
  const tenants = (state._kanbanBoard && Array.isArray(state._kanbanBoard.tenants)) ? state._kanbanBoard.tenants : [];
  const tList = document.getElementById('kanbanTaskModalTenantList');
  if (tList) tList.innerHTML = tenants.map(v => `<option value="${esc(v)}"></option>`).join('');
}

export function _kanbanPopulateWorkspacePathDatalist(){
  const cols = (state._kanbanBoard && state._kanbanBoard.columns) || [];
  const seen = new Set();
  const opts = [];
  for (const col of cols) {
    for (const task of (col.tasks || [])) {
      const path = task && task.workspace_path;
      if (!path || seen.has(path)) continue;
      seen.add(path);
      opts.push(`<option value="${esc(path)}"></option>`);
    }
  }
  const list = document.getElementById('kanbanTaskModalWorkspacePathList');
  if (list) list.innerHTML = opts.join('');
}

export function _kanbanPopulateParentsDatalist(){
  const list = document.getElementById('kanbanTaskModalParentsList');
  if (list) list.innerHTML = _kanbanLinkableTaskOptions(null);
}

export function _trapModalFocus(modalEl){
  if (!modalEl) return () => {};
  const selector = 'a[href], button, textarea, input, select, summary, [tabindex]:not([tabindex="-1"])';
  const collect = () => {
    const candidates = Array.from(modalEl.querySelectorAll(selector));
    return candidates.filter((el) => {
      if (el.disabled || el.hidden) return false;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      return el.tabIndex >= 0;
    });
  };
  let focusableEls = collect();
  const onKeyDown = (ev) => {
    if (ev.key !== 'Tab') return;
    if (!focusableEls.length) {
      ev.preventDefault();
      return;
    }
    const current = document.activeElement;
    let idx = focusableEls.indexOf(current);
    if (idx === -1) {
      ev.preventDefault();
      focusableEls[0].focus();
      return;
    }
    if (ev.shiftKey) idx -= 1;
    else idx += 1;
    idx = (idx + focusableEls.length) % focusableEls.length;
    ev.preventDefault();
    focusableEls[idx].focus();
  };
  modalEl.addEventListener('keydown', onKeyDown);
  return () => {
    modalEl.removeEventListener('keydown', onKeyDown);
  };
}

export function closeKanbanTaskModal(){
  const modal = document.getElementById('kanbanTaskModal');
  if (modal) modal.hidden = true;
  state._kanbanTaskModalMode = 'create';
  state._kanbanTaskModalEditingId = null;
  state._kanbanTaskModalInitialDisplayedStatus = null;
  _kanbanSetTaskModalStatusHint(null, null);
  if (state._kanbanTaskModalFocusCleanup) {
    state._kanbanTaskModalFocusCleanup();
    state._kanbanTaskModalFocusCleanup = null;
  }
  document.removeEventListener('keydown', _kanbanTaskModalKey);
}

export function _kanbanTaskModalKey(ev){
  if (ev.key === 'Escape') {
    ev.preventDefault();
    closeKanbanTaskModal();
    return;
  }
  if (ev.key === 'Enter' && !ev.shiftKey) {
    // Enter submits except when the focus is in the description textarea
    // (where Enter should insert a newline).
    const target = ev.target;
    if (target && target.tagName === 'TEXTAREA') return;
    const modal = document.getElementById('kanbanTaskModal');
    if (modal && !modal.hidden) {
      ev.preventDefault();
      submitKanbanTaskModal();
    }
  }
}

export function _kanbanOnWorkspaceKindChange(){
  const kindEl = document.getElementById('kanbanTaskModalWorkspaceKind');
  const pathRowEl = document.getElementById('kanbanTaskModalWorkspacePathRow');
  if (!kindEl || !pathRowEl) return;
  const kind = kindEl.value;
  pathRowEl.style.display = (kind === 'scratch') ? 'none' : 'block';
}
