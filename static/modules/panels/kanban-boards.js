import { state } from "./state.js";
import { _closeMobileSidebarAfterPanelSelection } from "./core.js";
import { _kanbanColumnLabel,_kanbanRenderMarkdown,_kanbanStartPolling,_kanbanStopPolling,_kanbanTaskBody,_kanbanTaskMeta,_kanbanTaskTitle,loadKanban } from "./kanban-board.js";
import { _kanbanBoardQuery,_kanbanCommentHtml,_kanbanDetailSection,_kanbanEventHtml,_kanbanLinksHtml,_kanbanRunHtml,_trapModalFocus,blockKanbanTask,closeKanbanTaskDetail,closeKanbanTaskModal,openKanbanEdit,unblockKanbanTask } from "./kanban-tasks.js";

// Panels domain: kanban task detail and board management

export async function submitKanbanTaskModal(){
  const titleEl = document.getElementById('kanbanTaskModalTitleInput');
  const bodyEl = document.getElementById('kanbanTaskModalBody');
  const statusEl = document.getElementById('kanbanTaskModalStatus');
  const assigneeEl = document.getElementById('kanbanTaskModalAssignee');
  const tenantEl = document.getElementById('kanbanTaskModalTenant');
  const priorityEl = document.getElementById('kanbanTaskModalPriority');
  const workspaceKindEl = document.getElementById('kanbanTaskModalWorkspaceKind');
  const workspacePathEl = document.getElementById('kanbanTaskModalWorkspacePath');
  const skillsEl = document.getElementById('kanbanTaskModalSkills');
  const maxRuntimeEl = document.getElementById('kanbanTaskModalMaxRuntimeSeconds');
  const parentsEl = document.getElementById('kanbanTaskModalParents');
  const errEl = document.getElementById('kanbanTaskModalError');
  const submitBtn = document.getElementById('kanbanTaskModalSubmit');
  const title = titleEl ? titleEl.value.trim() : '';
  if (!title) {
    if (errEl) errEl.textContent = t('kanban_title_required') || 'Title is required.';
    if (titleEl) titleEl.focus();
    return;
  }
  // Build payload — for create we omit defaulted fields so the backend chooses;
  // for edit we send every field so users can clear assignee/tenant/body.
  const isEdit = state._kanbanTaskModalMode === 'edit';
  // Validate workspace path for non-scratch workspace kinds (create mode only)
  const workspaceKind = workspaceKindEl ? workspaceKindEl.value : 'scratch';
  if (!isEdit && workspaceKind !== 'scratch') {
    const workspacePath = workspacePathEl ? workspacePathEl.value.trim() : '';
    if (!workspacePath) {
      if (errEl) errEl.textContent = t('kanban_workspace_path_required') || 'Workspace path is required for non-scratch workspaces.';
      if (workspacePathEl) workspacePathEl.focus();
      return;
    }
  }
  const payload = {title};
  const bodyVal = bodyEl ? bodyEl.value : '';
  const assigneeVal = assigneeEl ? assigneeEl.value.trim() : '';
  const tenantVal = tenantEl ? tenantEl.value.trim() : '';
  const statusVal = statusEl ? statusEl.value : '';
  const priorityRaw = priorityEl ? priorityEl.value : '';
  const workspacePathVal = workspacePathEl ? workspacePathEl.value.trim() : '';
  const skillsRaw = skillsEl ? skillsEl.value.trim() : '';
  const maxRuntimeRaw = maxRuntimeEl ? maxRuntimeEl.value.trim() : '';
  const parentsRaw = parentsEl ? parentsEl.value.trim() : '';
  if (isEdit) {
    payload.body = bodyVal;
    payload.assignee = assigneeVal || null;
    payload.tenant = tenantVal || null;
    // Only send status if the user actually changed the dropdown from the
    // value the modal opened with.  Otherwise editing a 'running'/'blocked'/
    // 'done'/'archived' task — whose real status maps to the dropdown's
    // 'triage' default — would silently demote the task on every save.
    if (statusVal && statusVal !== state._kanbanTaskModalInitialDisplayedStatus) {
      payload.status = statusVal;
    }
    const n = parseInt(priorityRaw, 10);
    payload.priority = Number.isNaN(n) ? 0 : n;
    // Note: workspace_kind and workspace_path are not sent on edit because
    // the backend _patch_task does not handle them (they are dropped).
  } else {
    if (bodyVal.trim()) payload.body = bodyVal;
    if (statusVal) payload.status = statusVal;
    if (assigneeVal) payload.assignee = assigneeVal;
    if (tenantVal) payload.tenant = tenantVal;
    if (priorityRaw !== '' && priorityRaw !== '0') {
      const n = parseInt(priorityRaw, 10);
      if (!Number.isNaN(n)) payload.priority = n;
    }
    payload.workspace_kind = workspaceKind;
    if (workspacePathVal) payload.workspace_path = workspacePathVal;
    if (skillsRaw) {
      payload.skills = skillsRaw.split(',').map(s => s.trim()).filter(Boolean);
    }
    if (maxRuntimeRaw) {
      if (!/^[1-9]\d*$/.test(maxRuntimeRaw)) {
        if (errEl) errEl.textContent = t('kanban_max_runtime_invalid')
          || 'Max runtime must be a whole number of seconds greater than 0.';
        if (maxRuntimeEl) maxRuntimeEl.focus();
        return;
      }
      payload.max_runtime_seconds = Number(maxRuntimeRaw);
    }
    if (parentsRaw) payload.parents = [parentsRaw];
  }
  // Soft warning: a Ready task with the explicit "Unassigned" option will sit
  // forever because the dispatcher skips unassigned rows (kanban_db.py:3567).
  // The dropdown now makes this an explicit choice (the user picked "—
  // Unassigned (won't auto-run) —"), but we still surface a one-time confirm
  // so they don't lose work to a typo.
  if (statusVal === 'ready' && !assigneeVal) {
    if (errEl && !errEl.dataset.warningShown) {
      errEl.textContent = t('kanban_ready_needs_assignee')
        || 'You picked Unassigned + Ready. The dispatcher will skip this task. Submit again to confirm, or pick a profile.';
      errEl.dataset.warningShown = '1';
      const sel = document.getElementById('kanbanTaskModalAssignee');
      if (sel) sel.focus();
      return;
    }
  }
  if (submitBtn) submitBtn.disabled = true;
  if (errEl) { errEl.textContent = ''; delete errEl.dataset.warningShown; }
  try {
    let saved;
    if (isEdit && state._kanbanTaskModalEditingId) {
      saved = await api(
        '/api/kanban/tasks/' + encodeURIComponent(state._kanbanTaskModalEditingId) + _kanbanBoardQuery(),
        {method: 'PATCH', body: JSON.stringify(payload)},
      );
    } else {
      saved = await api('/api/kanban/tasks' + _kanbanBoardQuery(), {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    }
    closeKanbanTaskModal();
    await loadKanban(true);
    const savedId = saved && saved.task && saved.task.id;
    if (savedId) {
      await loadKanbanTask(savedId);
    } else if (isEdit && state._kanbanTaskModalEditingId) {
      await loadKanbanTask(state._kanbanTaskModalEditingId);
    }
  } catch(e) {
    if (errEl) errEl.textContent = (e.message || String(e));
    if (submitBtn) submitBtn.disabled = false;
  }
}

export async function updateKanbanTask(taskId, patch, opts){
  if (!taskId || !patch) return;
  try {
    const openDetail = !opts || opts.openDetail !== false;
    const updated = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery(), {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    await loadKanban(true);
    if (openDetail) await loadKanbanTask((updated && updated.task && updated.task.id) || taskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export async function addKanbanComment(taskId){
  const input = document.getElementById('kanbanCommentInput');
  const body = input ? input.value.trim() : '';
  if (!taskId || !body) return;
  try {
    await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/comments' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({body}),
    });
    if (input) input.value = '';
    await loadKanbanTask(taskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export async function addKanbanDependency(taskId){
  const input = document.getElementById('kanbanDependencyInput');
  const linkTo = input ? input.value.trim() : '';
  if (!taskId || !linkTo) return;
  if (linkTo === taskId) {
    showToast(t('kanban_dependency_self') || 'A task cannot depend on itself', 'error');
    return;
  }
  try {
    // "Add dependency" on task X means "X depends on linkTo" → linkTo is the
    // prerequisite (parent) that must complete before X (child). The backend
    // models a (parent_id, child_id) row as parent=prerequisite/child=dependent
    // (api/kanban_bridge.py), so linkTo is the parent and the current task is
    // the child. (#3797)
    await api('/api/kanban/links' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({parent_id: linkTo, child_id: taskId}),
    });
    if (input) input.value = '';
    await loadKanbanTask(taskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export async function removeKanbanDependency(parentId, childId){
  if (!parentId || !childId) return;
  try {
    await api('/api/kanban/links/delete' + _kanbanBoardQuery(), {
      method: 'POST',
      body: JSON.stringify({parent_id: parentId, child_id: childId}),
    });
    await loadKanbanTask(state._kanbanCurrentTaskId);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

export function _kanbanRenderTaskDetail(data){
  const task = data.task || {};
  const log = data.log || {};
  const title = _kanbanTaskTitle(task);
  const body = _kanbanTaskBody(task) || t('kanban_no_description');
  const meta = _kanbanTaskMeta(task);
  const comments = data.comments || [];
  const events = data.events || [];
  const links = data.links || {};
  const runs = data.runs || [];
  // Note: 'running' is intentionally absent — entering 'running' is the
  // dispatcher/claim_task path's responsibility, not a user UI write. The
  // bridge rejects PATCH status='running' with HTTP 400 to match the agent
  // dashboard plugin's contract. UI users want to claim/promote a ready task
  // via the dispatcher Nudge button, not flip it to running by hand.
  const statusButtons = ['triage', 'todo', 'ready', 'blocked', 'done', 'archived'].map(status =>
    `<button class="btn secondary" onclick="updateKanbanTask('${esc(task.id)}',{status:'${status}'})">${esc(_kanbanColumnLabel(status))}</button>`
  ).join('') + `<button class="btn secondary" onclick="blockKanbanTask('${esc(task.id)}')">${esc(t('kanban_block'))}</button><button class="btn secondary" onclick="unblockKanbanTask('${esc(task.id)}')">${esc(t('kanban_unblock'))}</button>`;
  return `<div class="kanban-task-preview-header">
      <button class="btn secondary kanban-back-btn" onclick="closeKanbanTaskDetail()">${esc(t('kanban_back_to_board'))}</button>
      <div class="kanban-task-preview-title">${esc(title)}</div>
      <button class="btn secondary kanban-edit-btn" onclick="openKanbanEdit('${esc(task.id)}')" data-i18n="kanban_edit_task" title="${esc(t('kanban_edit_task') || 'Edit task')}">${esc(t('kanban_edit_task') || 'Edit task')}</button>
    </div>
    <div class="kanban-task-preview-body">${_kanbanRenderMarkdown(body)}</div>
    ${meta.length ? `<div class="kanban-meta">${esc(meta.join(' · '))}</div>` : ''}
    <div class="kanban-status-actions">${statusButtons}</div>
    <div class="kanban-detail-grid">
      ${_kanbanDetailSection('kanban-detail-comments', String(t('kanban_comments_count')).replace('{0}', comments.length), comments.map(_kanbanCommentHtml).join(''), 'kanban_no_comments')}
      ${_kanbanDetailSection('kanban-detail-events', String(t('kanban_events_count')).replace('{0}', events.length), events.map(_kanbanEventHtml).join(''), 'kanban_no_events')}
      ${_kanbanDetailSection('kanban-detail-links', t('kanban_links'), _kanbanLinksHtml(links), 'kanban_empty')}
      ${_kanbanDetailSection('kanban-detail-runs', String(t('kanban_runs_count')).replace('{0}', runs.length), runs.map(_kanbanRunHtml).join(''), 'kanban_no_runs')}
      ${_kanbanDetailSection('kanban-detail-log', t('kanban_worker_log'), log.content ? `<pre class="kanban-detail-pre">${esc(log.content)}</pre>` : '', 'kanban_empty')}
    </div>
    <div class="kanban-comment-form">
      <textarea id="kanbanCommentInput" rows="2" placeholder="${esc(t('kanban_add_comment'))}"></textarea>
      <button class="btn primary" onclick="addKanbanComment('${esc(task.id)}')">${esc(t('kanban_add_comment'))}</button>
    </div>`;
}

export async function loadKanbanTask(taskId){
  if (!taskId) return;
  try {
    const data = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + _kanbanBoardQuery());
    try { data.log = await api('/api/kanban/tasks/' + encodeURIComponent(taskId) + '/log' + _kanbanBoardQuery({tail: 65536})); } catch(e) { data.log = {}; }
    state._kanbanCurrentTaskId = taskId;
    const task = data.task || {};
    const title = _kanbanTaskTitle(task);
    const board = $('kanbanBoard');
    if (board) {
      board.querySelectorAll('.kanban-card').forEach(card => card.classList.remove('selected'));
      Array.from(board.querySelectorAll('.kanban-card')).find(card => card.dataset.kanbanTaskId === taskId)?.classList.add('selected');
    }
    const preview = $('kanbanTaskPreview');
    if (preview) {
      preview.style.display = '';
      preview.innerHTML = _kanbanRenderTaskDetail(data);
    }
    _closeMobileSidebarAfterPanelSelection();
    showToast(`${t('kanban_task')}: ${title}`);
  } catch(e) { showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error'); }
}

// Phase 2: Single-source-of-truth render.
//
// Reads `S.todos` (set by the `todo_state` SSE listener, INFLIGHT
// restore, or session cold-load — see _hydrateTodosFromSession in
// ui.js).  When `S.todoStateMeta` is null we have never seen an
// explicit signal and fall through to the legacy reverse-scan over
// settled tool messages — this keeps the panel populated against
// pre-Phase-1 servers and during the upgrade window.
//
// The render is short-circuited via `_todosLastRenderedHash` (defined
// in ui.js): repeated emissions that yield identical DOM are no-ops.
// Coalescing of bursty live updates happens upstream in
// scheduleTodosRefresh().
export function loadTodos() {
  const panel = $('todoPanel');
  if (!panel) return;

  let todos;
  if (S.todoStateMeta) {
    todos = Array.isArray(S.todos) ? S.todos : [];
  } else {
    todos = _legacyTodosFromMessages();
  }

  if (!todos.length) {
    if (typeof _todosLastRenderedHash !== 'undefined' && _todosLastRenderedHash === '__empty__') return;
    panel.innerHTML = renderTodoEmptyState();
    if (typeof _todosLastRenderedHash !== 'undefined') _todosLastRenderedHash = '__empty__';
    return;
  }

  if (typeof _todosHash === 'function' && typeof _todosLastRenderedHash !== 'undefined') {
    const hash = _todosHash(todos);
    if (hash === _todosLastRenderedHash) return;
    _todosLastRenderedHash = hash;
  }

  // Single innerHTML join is the cheapest correct way to materialize
  // ~10–50 leaf nodes.  All user-controlled content goes through esc().
  panel.innerHTML = renderTodoRows(todos, {metadata:true});
}

// Legacy fallback: reverse-scan settled tool messages for the most
// recent {"todos":[...]} payload.  Used only when no `todo_state`
// signal has been seen for the current session — primarily during
// upgrade windows where the server has not yet been redeployed with
// Phase 1.  Once Phase 1 is universally deployed and a stabilization
// period has passed, this can be removed (Phase 3).
//
// Variable name `sourceMessages` is preserved verbatim from the
// original loadTodos() implementation so the matching regression
// test (R-todo-survive-refresh in tests/test_regressions.py) keeps
// catching any future refactor that drops the raw-session-messages
// fallback. See the test for the exact contract.
export function _legacyTodosFromMessages() {
  const sourceMessages = (S.session && Array.isArray(S.session.messages) && S.session.messages.length) ? S.session.messages : S.messages;
  if (!Array.isArray(sourceMessages)) return [];
  for (let i = sourceMessages.length - 1; i >= 0; i--) {
    const m = sourceMessages[i];
    if (!m || m.role !== 'tool') continue;
    let content = m.content;
    if (typeof content !== 'string') {
      try { content = JSON.stringify(content); } catch (_) { continue; }
    }
    if (!content || content.indexOf('"todos"') < 0) continue;
    try {
      const d = JSON.parse(content);
      if (d && Array.isArray(d.todos)) return d.todos;
    } catch (_) {}
  }
  return [];
}
// ────────────────────────────────────────────────────────────────────────────
// Kanban: multi-board switcher + create/rename/archive modal
// ────────────────────────────────────────────────────────────────────────────
//
// The bridge exposes /api/kanban/boards (GET/POST), /boards/<slug>
// (PATCH/DELETE), and /boards/<slug>/switch (POST). The UI surfaces these
// as a "Default ▾" dropdown next to the Board title — clicking it opens
// a menu listing every board (current first, with task counts), plus
// actions to create / rename / archive.

const KANBAN_BOARD_LS_KEY = 'hermes-kanban-active-board';

export function _kanbanGetSavedBoard(){
  try { return localStorage.getItem(KANBAN_BOARD_LS_KEY) || null; } catch(_) { return null; }
}

export function _kanbanSetSavedBoard(slug){
  try {
    if (slug && slug !== 'default') localStorage.setItem(KANBAN_BOARD_LS_KEY, slug);
    else localStorage.removeItem(KANBAN_BOARD_LS_KEY);
  } catch(_) {}
}

export async function loadKanbanBoards(){
  // Fetches the boards list and updates the switcher UI. Best-effort —
  // failures hide the switcher rather than blocking the panel from rendering.
  const switcher = document.getElementById('kanbanBoardSwitcher');
  if (!switcher) return;
  let data;
  try {
    data = await api('/api/kanban/boards');
  } catch(e) {
    // Hide switcher on error so the user isn't stuck with a half-broken UI.
    switcher.hidden = true;
    return;
  }
  const boards = (data && data.boards) || [];
  const serverCurrent = (data && data.current) || 'default';
  state._kanbanBoardsList = boards;
  // Resolution chain for the active board:
  //   localStorage hint → server's `current` → 'default'.
  // The localStorage hint is honoured ONLY if it points at a board that
  // still exists; otherwise we fall back to the server's pointer.
  const saved = _kanbanGetSavedBoard();
  let active = serverCurrent;
  if (saved && boards.some(b => b.slug === saved)) {
    active = saved;
  } else if (saved) {
    _kanbanSetSavedBoard('default');
  }
  state._kanbanCurrentBoard = (active === 'default') ? null : active;
  // The switcher is visible whenever ≥1 non-default board exists OR the
  // current board is non-default. (If you only have 'default', a switcher
  // adds clutter without value.)
  const hasMultiple = boards.length > 1 || (active !== 'default');
  switcher.hidden = !hasMultiple;
  if (!hasMultiple) return;
  // Update the toggle label/icon
  const activeMeta = boards.find(b => b.slug === active) || {slug: active, name: active, icon: '', color: ''};
  const nameEl = document.getElementById('kanbanBoardSwitcherName');
  const iconEl = document.getElementById('kanbanBoardSwitcherIcon');
  if (nameEl) nameEl.textContent = activeMeta.name || activeMeta.slug || 'Default';
  if (iconEl) {
    iconEl.textContent = activeMeta.icon || '';
    if (activeMeta.color) iconEl.style.color = activeMeta.color;
    else iconEl.style.color = '';
  }
  // Re-render the menu (in case it was open or changed)
  _renderKanbanBoardMenu(boards, active);
}

// Restrict board.color to CSS hex codes or simple named colors before
// interpolating into a `style=""` attribute. esc() HTML-escapes but
// does not block CSS-context injection (`color:red;background:url(...)`
// would otherwise exfiltrate page state via an attacker-controlled URL,
// since neither this bridge nor the agent's kanban_db validates color).
export function _kanbanSafeColor(c){
  if (typeof c !== 'string') return '';
  const s = c.trim();
  if (!s) return '';
  if (/^#[0-9a-fA-F]{3,8}$/.test(s)) return s;
  if (/^[a-zA-Z]{3,32}$/.test(s)) return s;
  return '';
}

export function _renderKanbanBoardMenu(boards, current){
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  if (!menu) return;
  const items = boards.map(b => {
    const isCurrent = b.slug === current;
    const total = (b.total != null) ? b.total : (b.counts ? Object.values(b.counts).reduce((a,c)=>a+Number(c||0),0) : 0);
    const icon = b.icon ? esc(b.icon) : '';
    const safeColor = _kanbanSafeColor(b.color);
    const colorStyle = safeColor ? `color:${safeColor}` : '';
    return `<button type="button" class="kanban-board-switcher-item ${isCurrent ? 'is-current' : ''}" role="menuitem" data-board-slug="${esc(b.slug)}" onclick="switchKanbanBoard('${esc(b.slug)}')">
      <span class="kanban-board-switcher-item-icon" style="${colorStyle}">${icon || (isCurrent ? '✓' : '')}</span>
      <span class="kanban-board-switcher-item-name">${esc(b.name || b.slug)}</span>
      <span class="kanban-board-switcher-item-count">${esc(String(total))}</span>
    </button>`;
  }).join('');
  // Actions row — disable rename/archive when the only option is `default`
  // (the default board's display metadata is editable but the slug isn't,
  // and `default` cannot be archived).
  const renameDisabled = current === 'default';
  const archiveDisabled = current === 'default';
  const actions = `
    <div class="kanban-board-switcher-divider" role="separator"></div>
    <button type="button" class="kanban-board-switcher-action" onclick="openKanbanCreateBoard()" data-i18n="kanban_new_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      <span>${esc(t('kanban_new_board') || 'New board…')}</span>
    </button>
    <button type="button" class="kanban-board-switcher-action" onclick="openKanbanRenameBoard()" ${renameDisabled ? 'disabled' : ''} data-i18n="kanban_rename_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
      <span>${esc(t('kanban_rename_board') || 'Rename current board…')}</span>
    </button>
    <button type="button" class="kanban-board-switcher-action danger" onclick="archiveKanbanBoard()" ${archiveDisabled ? 'disabled' : ''} data-i18n="kanban_archive_board">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
      <span>${esc(t('kanban_archive_board') || 'Archive current board…')}</span>
    </button>
  `;
  menu.innerHTML = items + actions;
}

export function toggleKanbanBoardMenu(ev){
  if (ev) ev.stopPropagation();
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  const toggle = document.getElementById('kanbanBoardSwitcherToggle');
  if (!menu || !toggle) return;
  state._kanbanBoardMenuOpen = !state._kanbanBoardMenuOpen;
  menu.hidden = !state._kanbanBoardMenuOpen;
  toggle.setAttribute('aria-expanded', String(state._kanbanBoardMenuOpen));
  if (state._kanbanBoardMenuOpen) {
    // Click-away close
    setTimeout(() => {
      document.addEventListener('click', _kanbanCloseBoardMenuOnOutside, {once: true, capture: true});
    }, 0);
  }
}

export function _kanbanCloseBoardMenuOnOutside(ev){
  const switcher = document.getElementById('kanbanBoardSwitcher');
  if (!switcher || !switcher.contains(ev.target)) {
    state._kanbanBoardMenuOpen = false;
    const menu = document.getElementById('kanbanBoardSwitcherMenu');
    const toggle = document.getElementById('kanbanBoardSwitcherToggle');
    if (menu) menu.hidden = true;
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  } else {
    // Re-arm the listener — the user clicked inside the switcher, possibly
    // the toggle button which we want to handle through its own onclick.
    setTimeout(() => {
      document.addEventListener('click', _kanbanCloseBoardMenuOnOutside, {once: true, capture: true});
    }, 0);
  }
}

export async function switchKanbanBoard(slug){
  if (!slug) return;
  const newBoard = (slug === 'default') ? null : slug;
  if (newBoard === state._kanbanCurrentBoard) {
    // No-op switch — just close the menu.
    state._kanbanBoardMenuOpen = false;
    const menu = document.getElementById('kanbanBoardSwitcherMenu');
    if (menu) menu.hidden = true;
    return;
  }
  state._kanbanCurrentBoard = newBoard;
  _kanbanSetSavedBoard(slug);
  state._kanbanLatestEventId = 0;  // reset cursor — new board has its own event sequence
  state._kanbanBoardMenuOpen = false;
  const menu = document.getElementById('kanbanBoardSwitcherMenu');
  if (menu) menu.hidden = true;
  // Tell the server too (sets the on-disk active-board pointer for CLI/dashboard).
  try {
    await api('/api/kanban/boards/' + encodeURIComponent(slug) + '/switch', {method: 'POST'});
  } catch(e) {
    // Local UI switch still happens — the on-disk pointer is for cross-process
    // consistency, not for our own rendering.
  }
  // Re-open the SSE stream on the new board.
  _kanbanStopPolling();
  await loadKanban(true);
  await loadKanbanBoards();
  _kanbanStartPolling();
}

// ── Create / rename / archive board modals ──────────────────────────────────

export function openKanbanCreateBoard(){
  const modal = document.getElementById('kanbanBoardModal');
  if (!modal) return;
  document.getElementById('kanbanBoardModalMode').value = 'create';
  document.getElementById('kanbanBoardModalSlug').value = '';
  document.getElementById('kanbanBoardModalTitle').textContent = t('kanban_new_board') || 'New board';
  document.getElementById('kanbanBoardModalName').value = '';
  document.getElementById('kanbanBoardModalSlugInput').value = '';
  document.getElementById('kanbanBoardModalSlugInput').disabled = false;
  document.getElementById('kanbanBoardModalSlugRow').style.display = '';
  document.getElementById('kanbanBoardModalDesc').value = '';
  document.getElementById('kanbanBoardModalIcon').value = '';
  document.getElementById('kanbanBoardModalColor').value = '#7aa2ff';
  document.getElementById('kanbanBoardModalError').textContent = '';
  modal.hidden = false;
  if (state._kanbanBoardModalFocusCleanup) {
    state._kanbanBoardModalFocusCleanup();
    state._kanbanBoardModalFocusCleanup = null;
  }
  state._kanbanBoardModalFocusCleanup = _trapModalFocus(modal);
  // Auto-focus name field
  setTimeout(() => document.getElementById('kanbanBoardModalName').focus(), 50);
  // Auto-suggest slug from name as user types
  const nameEl = document.getElementById('kanbanBoardModalName');
  const slugEl = document.getElementById('kanbanBoardModalSlugInput');
  let userEditedSlug = false;
  slugEl.addEventListener('input', () => { userEditedSlug = true; }, {once: false});
  const onName = () => {
    if (!userEditedSlug) {
      slugEl.value = String(nameEl.value || '').toLowerCase().replace(/[^a-z0-9-_ ]+/g, '').replace(/\s+/g, '-').slice(0, 48);
    }
  };
  nameEl.removeEventListener('input', nameEl._kanbanOnNameInput || (() => {}));
  nameEl._kanbanOnNameInput = onName;
  nameEl.addEventListener('input', onName);
  // Close on Escape
  document.addEventListener('keydown', _kanbanBoardModalEsc);
}

export function openKanbanRenameBoard(){
  const modal = document.getElementById('kanbanBoardModal');
  if (!modal) return;
  const current = state._kanbanCurrentBoard || 'default';
  if (current === 'default') return;  // default's slug is immutable
  const meta = (state._kanbanBoardsList || []).find(b => b.slug === current);
  if (!meta) return;
  document.getElementById('kanbanBoardModalMode').value = 'rename';
  document.getElementById('kanbanBoardModalSlug').value = current;
  document.getElementById('kanbanBoardModalTitle').textContent = t('kanban_rename_board') || 'Rename board';
  document.getElementById('kanbanBoardModalName').value = meta.name || '';
  document.getElementById('kanbanBoardModalSlugInput').value = current;
  document.getElementById('kanbanBoardModalSlugInput').disabled = true;  // slug is immutable
  // Hide the slug row — it's locked, less visual noise.
  document.getElementById('kanbanBoardModalSlugRow').style.display = 'none';
  document.getElementById('kanbanBoardModalDesc').value = meta.description || '';
  document.getElementById('kanbanBoardModalIcon').value = meta.icon || '';
  document.getElementById('kanbanBoardModalColor').value = meta.color || '#7aa2ff';
  document.getElementById('kanbanBoardModalError').textContent = '';
  modal.hidden = false;
  if (state._kanbanBoardModalFocusCleanup) {
    state._kanbanBoardModalFocusCleanup();
    state._kanbanBoardModalFocusCleanup = null;
  }
  state._kanbanBoardModalFocusCleanup = _trapModalFocus(modal);
  setTimeout(() => document.getElementById('kanbanBoardModalName').focus(), 50);
  document.addEventListener('keydown', _kanbanBoardModalEsc);
}

export function _kanbanBoardModalEsc(ev){
  if (ev.key === 'Escape') closeKanbanBoardModal();
}

export function closeKanbanBoardModal(){
  const modal = document.getElementById('kanbanBoardModal');
  if (modal) modal.hidden = true;
  if (state._kanbanBoardModalFocusCleanup) {
    state._kanbanBoardModalFocusCleanup();
    state._kanbanBoardModalFocusCleanup = null;
  }
  document.removeEventListener('keydown', _kanbanBoardModalEsc);
}

export async function submitKanbanBoardModal(){
  const errEl = document.getElementById('kanbanBoardModalError');
  errEl.textContent = '';
  const mode = document.getElementById('kanbanBoardModalMode').value;
  const name = (document.getElementById('kanbanBoardModalName').value || '').trim();
  const slugInput = (document.getElementById('kanbanBoardModalSlugInput').value || '').trim();
  const description = (document.getElementById('kanbanBoardModalDesc').value || '').trim();
  const icon = (document.getElementById('kanbanBoardModalIcon').value || '').trim();
  const color = (document.getElementById('kanbanBoardModalColor').value || '').trim();
  const submitBtn = document.getElementById('kanbanBoardModalSubmit');
  if (!name) {
    errEl.textContent = t('kanban_board_name_required') || 'Name is required';
    return;
  }
  if (mode === 'create') {
    if (!slugInput) {
      errEl.textContent = t('kanban_board_slug_required') || 'Slug is required';
      return;
    }
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api('/api/kanban/boards', {
        method: 'POST',
        body: JSON.stringify({slug: slugInput, name, description, icon, color, switch: true}),
      });
      closeKanbanBoardModal();
      // Switch to the new board and reload
      const newSlug = (res && res.board && res.board.slug) || slugInput;
      state._kanbanCurrentBoard = (newSlug === 'default') ? null : newSlug;
      _kanbanSetSavedBoard(newSlug);
      state._kanbanLatestEventId = 0;
      _kanbanStopPolling();
      await loadKanban(true);
      await loadKanbanBoards();
      _kanbanStartPolling();
    } catch(e) {
      errEl.textContent = (e && (e.message || e.error)) || String(e);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  } else if (mode === 'rename') {
    const slug = document.getElementById('kanbanBoardModalSlug').value;
    if (!slug) { errEl.textContent = 'Missing slug'; return; }
    if (submitBtn) submitBtn.disabled = true;
    try {
      await api('/api/kanban/boards/' + encodeURIComponent(slug), {
        method: 'PATCH',
        body: JSON.stringify({name, description, icon, color}),
      });
      closeKanbanBoardModal();
      await loadKanbanBoards();  // refresh switcher label/icon
    } catch(e) {
      errEl.textContent = (e && (e.message || e.error)) || String(e);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }
}

export async function archiveKanbanBoard(){
  const current = state._kanbanCurrentBoard || 'default';
  if (current === 'default') return;
  const meta = (state._kanbanBoardsList || []).find(b => b.slug === current);
  const label = meta && meta.name ? meta.name : current;
  const ok = await showConfirmDialog({
    title: t('kanban_archive_board') || 'Archive board',
    message: (t('kanban_archive_board_confirm') || 'Archive board "{name}"? Tasks remain on disk and the board can be restored from kanban/boards/_archived/.').replace('{name}', label),
    confirmLabel: t('kanban_archive_board') || 'Archive',
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  // CRITICAL: stop the SSE stream BEFORE the archive call. The library's
  // kb.connect(board=<slug>) auto-creates the on-disk directory + DB on
  // first call — so any in-flight stream that polls task_events while
  // we're archiving will silently re-materialise the directory we just
  // moved to _archived/. Tearing down the stream first avoids that race.
  _kanbanStopPolling();
  try {
    await api('/api/kanban/boards/' + encodeURIComponent(current), {method: 'DELETE'});
    // Server falls back to default — match that locally.
    state._kanbanCurrentBoard = null;
    _kanbanSetSavedBoard('default');
    state._kanbanLatestEventId = 0;
    await loadKanban(true);
    await loadKanbanBoards();
    _kanbanStartPolling();
    showToast(t('kanban_board_archived') || 'Board archived');
  } catch(e) {
    // Restart the stream on failure so the UI doesn't go stale.
    _kanbanStartPolling();
    showToast(t('kanban_unavailable') + ': ' + (e.message || e), 'error');
  }
}
