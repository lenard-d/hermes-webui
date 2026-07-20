// Panels domain: cron detail, editing, and runtime
window.HermesPanels = window.HermesPanels || {};

function _cronPanelExpandKey(jobId, suffix){
  return `hermes-webui-cron-${suffix}-expanded-${encodeURIComponent(String(jobId||''))}`;
}

function _cronRunExpandKey(jobId, filename){
  return `${_cronPanelExpandKey(jobId, 'run')}-${encodeURIComponent(String(filename||''))}`;
}

function _cronExpansionGet(key){
  try { return localStorage.getItem(key) === '1'; } catch(_) { return false; }
}

function _cronExpansionSet(key, expanded){
  try { localStorage.setItem(key, expanded ? '1' : '0'); } catch(_) {}
}

function toggleCronPromptExpanded(jobId){
  const key = _cronPanelExpandKey(jobId, 'prompt');
  _cronExpansionSet(key, !_cronExpansionGet(key));
  if (_currentCronDetail && String(_currentCronDetail.id) === String(jobId)) {
    _renderCronDetail(_currentCronDetail);
  }
}

function toggleCronRunExpanded(jobId, filename, runId){
  const key = _cronRunExpandKey(jobId, filename);
  const expanded = !_cronExpansionGet(key);
  _cronExpansionSet(key, expanded);
  const item = document.getElementById(runId);
  const body = item ? item.querySelector('.detail-run-body') : null;
  const btn = item ? item.querySelector('.detail-expand-toggle') : null;
  if (body) body.classList.toggle('expanded', expanded);
  if (btn) {
    btn.textContent = expanded ? '▴' : '▾';
    btn.title = expanded ? (t('cron_collapse_output') || 'Collapse output') : (t('cron_expand_output') || 'Expand output');
    btn.setAttribute('aria-label', btn.title);
  }
}

function _isCronScriptJob(job){
  return !!(job && job.no_agent);
}

function _cronModeLabel(job){
  return _isCronScriptJob(job)
    ? (t('cron_mode_script') || 'Script')
    : (t('cron_mode_agent') || 'Agent');
}

function _cronOutputTitle(job){
  return _isCronScriptJob(job)
    ? (t('cron_script_output') || 'Script output')
    : (t('cron_last_output') || 'Last output');
}

function _cronScriptJobBannerHtml(){
  return `<div class="detail-alert cron-script-job-banner">
        <div class="detail-alert-title">${esc(t('cron_mode_script') || 'Script')}</div>
        <p>${esc(t('cron_script_job_banner') || 'Runs a script on schedule — stdout is delivered to the target. No agent, prompt, or skills.')}</p>
      </div>`;
}

function _cronScriptCardHtml(job){
  const script = String(job && job.script || '').trim() || '—';
  const workdir = String(job && job.workdir || '').trim();
  const workdirRow = workdir
    ? `<div class="detail-row"><div class="detail-row-label">${esc(t('cron_workdir_label') || 'Working directory')}</div><div class="detail-row-value"><code>${esc(workdir)}</code></div></div>`
    : '';
  return `<div class="detail-card cron-script-card">
        <div class="detail-card-title">${esc(t('cron_script_card_title') || 'Script')}</div>
        <div class="detail-script">${esc(script)}</div>
        ${workdirRow}
        <div class="detail-hint cron-script-card-hint">${esc(t('cron_script_path_hint') || 'Resolved under ~/.hermes/scripts/ unless an absolute path. Edit the script file on the server to change behavior.')}</div>
      </div>`;
}

function _cronAgentPromptCardHtml(job){
  const promptExpanded = _cronExpansionGet(_cronPanelExpandKey(job.id, 'prompt'));
  const promptToggleLabel = promptExpanded ? (t('cron_collapse_prompt') || 'Collapse prompt') : (t('cron_expand_prompt') || 'Expand prompt');
  return `<div class="detail-card">
        <div class="detail-card-title detail-card-title-row">
          <span>${esc(t('cron_prompt_label') || 'Prompt')}</span>
          <button type="button" class="detail-expand-toggle" onclick="toggleCronPromptExpanded('${esc(job.id)}')" title="${esc(promptToggleLabel)}" aria-label="${esc(promptToggleLabel)}">${esc(promptExpanded ? '▴' : '▾')}</button>
        </div>
        <div class="detail-prompt ${promptExpanded ? 'expanded' : ''}">${esc(job.prompt || '')}</div>
      </div>`;
}

function _renderCronDetail(job){
  _currentCronDetail = job;
  _currentCronDetailKey = _cronJobKey(job);
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (!title || !body) return;
  title.textContent = job.name || job.schedule_display || '(unnamed)';
  const status = _cronStatusMeta(job);
  const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : t('not_available');
  const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : t('never');
  const schedule = job.schedule_display || (job.schedule && job.schedule.expression) || '';
  const skills = Array.isArray(job.skills) && job.skills.length ? job.skills.join(', ') : '—';
  const deliver = job.deliver || 'local';
  const isNoAgent = _isCronScriptJob(job);
  const isReadOnly = !!job.read_only;
  const cronJobMode = _cronModeLabel(job);
  const modelProvider =
    job.provider && job.model ? `${esc(job.provider)}/${esc(job.model)}` :
    job.model ? esc(job.model) :
    job.provider ? esc(job.provider) :
    isNoAgent ? '' : esc(t('cron_model_use_default') || 'Use profile default');
  const profileLabel = _cronProfileLabel(job.profile);
  const profileTitle = _cronProfileTitle(job.profile);
  const ownerProfileName = _cronOwnerProfileName(job);
  const ownerProfileLabel = _cronProfileLabel(ownerProfileName);
  const ownerProfileTitle = `Owner profile: ${ownerProfileLabel}`;
  const showOwnerRow = !!ownerProfileName && (isReadOnly || ownerProfileName !== _cronProfileName(job.profile));
  const lastError = job.last_error ? `<div class="detail-row"><div class="detail-row-label">${esc(t('error_prefix').replace(/:\s*$/,''))}</div><div class="detail-row-value" style="color:var(--accent-text)">${esc(job.last_error)}</div></div>` : '';
  const attention = status.state === 'needs_attention' || status.state === 'schedule_error';
  const croniterHint = job.last_error && /croniter/i.test(job.last_error)
    ? `<p>${esc(t('cron_attention_croniter_hint'))}</p>`
    : '';
  const attentionBanner = !isReadOnly && attention ? `
      <div class="detail-alert cron-attention-panel">
        <div class="detail-alert-title">${esc(t('cron_status_needs_attention'))}</div>
        <p>${esc(t('cron_attention_desc'))}</p>
        ${croniterHint}
        <div class="detail-alert-actions">
          <button type="button" class="cron-btn run" onclick="resumeCurrentCron()">${esc(t('cron_attention_resume'))}</button>
          <button type="button" class="cron-btn" onclick="runCurrentCron()">${esc(t('cron_attention_run_once'))}</button>
          <button type="button" class="cron-btn" onclick="copyCurrentCronDiagnostics()">${esc(t('cron_attention_copy_diagnostics'))}</button>
        </div>
      </div>` : '';
  const readOnlyBanner = isReadOnly ? `
      <div class="detail-alert">
        <div class="detail-alert-title">Read-only from another profile</div>
        <p>Switch to ${esc(ownerProfileLabel)} to run, edit, or inspect live status and output for this cron job.</p>
      </div>` : '';
  const toastNotifications = job.toast_notifications !== false;
  const outputTitle = _cronOutputTitle(job);
  const skillsRow = isNoAgent ? '' : `<div class="detail-row"><div class="detail-row-label">${esc(t('cron_skills_label') || 'Skills')}</div><div class="detail-row-value">${esc(skills)}</div></div>`;
  const instructionCard = isNoAgent ? _cronScriptCardHtml(job) : _cronAgentPromptCardHtml(job);
  body.innerHTML = `
    <div class="main-view-content">
      ${attentionBanner}
      ${readOnlyBanner}
      ${isNoAgent ? _cronScriptJobBannerHtml() : ''}
      <div class="detail-card">
        <div class="detail-card-title">${esc(t('cron_status_active').replace(/./,c=>c.toUpperCase()))}</div>
        <div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value"><span class="detail-badge ${status.detailClass}">${esc(status.label)}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">Schedule</div><div class="detail-row-value"><code>${esc(schedule)}</code></div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_next'))}</div><div class="detail-row-value">${esc(nextRun)}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_last'))}</div><div class="detail-row-value">${esc(lastRun)}</div></div>
        <div class="detail-row"><div class="detail-row-label">Deliver</div><div class="detail-row-value">${esc(deliver)}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_mode_label') || 'Mode')}</div><div class="detail-row-value"><span class="detail-badge cron-mode-badge ${isNoAgent ? 'script' : 'agent'}" id="cronJobMode">${esc(cronJobMode)}</span>${modelProvider ? ` <code>${modelProvider}</code>` : ''}</div></div>
        ${showOwnerRow ? `<div class="detail-row"><div class="detail-row-label">Owner profile</div><div class="detail-row-value"><span class="detail-badge active" title="${esc(ownerProfileTitle)}">${esc(ownerProfileLabel)}</span></div></div>` : ''}
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_profile_label') || 'Profile')}</div><div class="detail-row-value"><span class="detail-badge active" title="${esc(profileTitle)}">${esc(profileLabel)}</span></div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('cron_toast_notifications_label') || 'Completion toasts')}</div><div class="detail-row-value"><span class="detail-badge ${toastNotifications ? 'active' : ''}">${esc(toastNotifications ? (t('cron_toast_notifications_enabled') || 'Enabled') : (t('cron_toast_notifications_disabled') || 'Disabled'))}</span></div></div>
        ${skillsRow}
        ${lastError}
      </div>
      ${instructionCard}
      <div class="detail-card ${!isReadOnly && _cronNewJobIds.has(String(job.id)) ? 'has-new-run' : ''}" id="cronDetailRuns">
        <div class="detail-card-title">${esc(outputTitle)}</div>
        <div style="color:var(--muted);font-size:12px">${esc(isReadOnly ? 'Live output is available only when this profile is active here.' : (t('loading') || 'Loading'))}</div>
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _cronMode = 'read';
  _setCronHeaderButtons('read', job);
  // Load runs asynchronously
  if (!isReadOnly) _loadCronDetailRuns(job.id, _currentCronDetailKey);
}

function _setCronHeaderButtons(mode, job) {
  const runBtn = $('btnRunTaskDetail');
  const pauseBtn = $('btnPauseTaskDetail');
  const resumeBtn = $('btnResumeTaskDetail');
  const editBtn = $('btnEditTaskDetail');
  const dupBtn = $('btnDuplicateTaskDetail');
  const delBtn = $('btnDeleteTaskDetail');
  const cancelBtn = $('btnCancelTaskDetail');
  const saveBtn = $('btnSaveTaskDetail');
  const header = $('mainTasks') && $('mainTasks').querySelector('.main-view-header');
  const hide = b => b && (b.style.display = 'none');
  const show = b => b && (b.style.display = '');
  if (mode === 'read') {
    if (header) header.style.display = 'flex';
    if (job && job.read_only) {
      [runBtn,pauseBtn,resumeBtn,editBtn,dupBtn,delBtn,cancelBtn,saveBtn].forEach(hide);
      return;
    }
    show(runBtn);
    const status = job ? _cronStatusMeta(job) : null;
    const resumable = job && (
      job.state === 'paused' ||
      (status && (status.state === 'needs_attention' || status.state === 'schedule_error'))
    );
    if (resumable) { hide(pauseBtn); show(resumeBtn); }
    else { show(pauseBtn); hide(resumeBtn); }
    show(editBtn); show(dupBtn); show(delBtn); hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create' || mode === 'edit') {
    if (header) header.style.display = 'flex';
    hide(runBtn); hide(pauseBtn); hide(resumeBtn); hide(editBtn); hide(dupBtn); hide(delBtn);
    show(cancelBtn); show(saveBtn);
  } else {
    [runBtn,pauseBtn,resumeBtn,editBtn,dupBtn,delBtn,cancelBtn,saveBtn].forEach(hide);
    if (header) header.style.display = 'none';
  }
}

async function _loadCronDetailRuns(jobId, detailKey){
  try {
    const data = await api(`/api/crons/history?job_id=${encodeURIComponent(jobId)}&limit=50`);
    if (!_cronDetailMatches(jobId, detailKey)) return;
    const card = $('cronDetailRuns');
    if (!card) return;
    const outputTitle = _cronOutputTitle(_currentCronDetail);
    const isScriptJob = _isCronScriptJob(_currentCronDetail);
    if (!data.runs || !data.runs.length) {
      card.innerHTML = `<div class="detail-card-title">${esc(outputTitle)}</div><div style="color:var(--muted);font-size:12px">${esc(t('cron_no_runs_yet'))}</div>`;
      return;
    }
    const rows = data.runs.map((run, i) => {
      const ts = run.filename.replace('.md','').replace(/_/g,' ');
      const sizeStr = run.size > 1024 ? (run.size/1024).toFixed(1)+' KB' : run.size+' B';
      const dateStr = new Date(run.modified * 1000).toLocaleString();
      const rid = `cron-det-run-${jobId}-${i}`;
      const usageStrip = isScriptJob ? '' : _formatCronRunUsageStrip(run.usage);
      const runExpanded = _cronExpansionGet(_cronRunExpandKey(jobId, run.filename));
      const runToggleLabel = runExpanded ? (t('cron_collapse_output') || 'Collapse output') : (t('cron_expand_output') || 'Expand output');
      return `<div class="detail-run-item" id="${rid}">
        <div class="detail-run-head" onclick="_loadRunContent('${esc(jobId)}','${esc(run.filename)}','${rid}')">
          <span><span style="opacity:.7">${esc(ts)}</span> <span style="opacity:.4;font-size:11px">${esc(sizeStr)}</span>${usageStrip ? ` <span class="cron-run-usage-strip">${esc(usageStrip)}</span>` : ''}</span>
          <span class="detail-run-actions">
            <button type="button" class="detail-expand-toggle" onclick="event.stopPropagation();toggleCronRunExpanded('${esc(jobId)}','${esc(run.filename)}','${rid}')" title="${esc(runToggleLabel)}" aria-label="${esc(runToggleLabel)}">${esc(runExpanded ? '▴' : '▾')}</button>
            <span style="opacity:.6">▸</span>
          </span>
        </div>
        <div class="detail-run-body ${runExpanded ? 'expanded' : ''}" style="color:var(--muted);font-size:12px">${esc(t('loading'))}</div>
      </div>`;
    }).join('');
    const countLabel = data.total > 50 ? ` (${data.total} runs, showing latest 50)` : ` (${data.total} runs)`;
    card.innerHTML = `<div class="detail-card-title">${esc(outputTitle)}${countLabel}</div>${rows}`;
  } catch(e) { /* ignore */ }
}

async function _loadRunContent(jobId, filename, runId){
  const body = document.querySelector(`#${runId} .detail-run-body`);
  if (!body) return;
  const item = document.getElementById(runId);
  if (item.classList.contains('open')) {
    // Already open → collapse and return (toggle behaviour)
    item.classList.remove('open');
    body.classList.remove('expanded');
    _cronExpansionSet(_cronRunExpandKey(jobId, filename), false);
    const btn = item ? item.querySelector('.detail-expand-toggle') : null;
    if (btn) {
      btn.textContent = '▾';
      btn.title = (t('cron_expand_output') || 'Expand output');
      btn.setAttribute('aria-label', btn.title);
    }
    return;
  }
  item.classList.add('open');
  body.classList.toggle('expanded', _cronExpansionGet(_cronRunExpandKey(jobId, filename)));
  body.innerHTML = `<span style="opacity:.5">${esc(t('loading'))}</span>`;
  try {
    const data = await api(`/api/crons/run?job_id=${encodeURIComponent(jobId)}&filename=${encodeURIComponent(filename)}`);
    if (data.error) {
      body.textContent = data.error;
      return;
    }
    const expanded = _cronExpansionGet(_cronRunExpandKey(jobId, filename));
    const output = expanded ? (data.content || data.snippet || '') : (data.snippet || data.content || '');
    body.classList.toggle('expanded', expanded);
    // Cron run output is never authored Markdown — render as literal
    // preformatted text using DOM-created <pre><code> so all content
    // (including shapes starting with #, |, >, ``` and embedded fences)
    // renders verbatim without Markdown interpretation.
    body.innerHTML = '';
    const pre = document.createElement('pre');
    pre.className = 'cron-run-pre';
    const code = document.createElement('code');
    code.textContent = output;
    pre.appendChild(code);
    body.appendChild(pre);
    const usageStrip = _formatCronRunUsageStrip(data.usage);
    if (usageStrip) {
      const usage = document.createElement('div');
      usage.className = 'cron-run-usage-strip cron-run-usage-footer';
      usage.textContent = usageStrip;
      body.appendChild(usage);
    }
    // Show "View full output" button only for collapsed previews. Expanded rows render the full body inline.
    if (!expanded && data.content && data.snippet && data.content.length > data.snippet.length) {
      const btn = document.createElement('button');
      btn.style.cssText = 'margin-top:8px;padding:4px 12px;border-radius:var(--radius-btn);border:1px solid var(--border-subtle);background:var(--surface-subtle);color:var(--text-secondary);cursor:pointer;font-size:12px';
      btn.textContent = t('cron_view_full_output') || 'View full output';
      btn.onclick = () => {
        _cronExpansionSet(_cronRunExpandKey(jobId, filename), true);
        body.classList.add('expanded');
        body.innerHTML = '';
        const pre = document.createElement('pre');
        pre.className = 'cron-run-pre';
        const code = document.createElement('code');
        code.textContent = data.content || '';
        pre.appendChild(code);
        body.appendChild(pre);
        const usageStrip = _formatCronRunUsageStrip(data.usage);
        if (usageStrip) {
          const usage = document.createElement('div');
          usage.className = 'cron-run-usage-strip cron-run-usage-footer';
          usage.textContent = usageStrip;
          body.appendChild(usage);
        }
        btn.remove();
      };
      body.appendChild(btn);
    }
  } catch(e) {
    body.textContent = 'Error: ' + e.message;
  }
}

function openCronDetail(jobOrId, el){
  const job = _findCronJob(jobOrId);
  if (!job) return;
  const detailKey = _cronJobKey(job);
  document.querySelectorAll('.cron-item').forEach(e => e.classList.remove('active'));
  const target = el || $(_cronItemId(job));
  if (target) target.classList.add('active');
  // Remove new-run dot from this job since user is now viewing it
  if (!job.read_only) _clearCronUnreadForJob(job.id);
  const dot = target && target.querySelector('.cron-new-dot');
  if (dot && !job.read_only) dot.remove();
  _cronPreFormDetail = null;
  _editingCronId = null;
  _stopCronWatch();
  _renderCronDetail(job);
  if (!job.read_only) _checkCronWatchOnDetail(job.id, detailKey);
  _closeMobileSidebarAfterPanelSelection();
}

function _clearCronDetail(){
  _currentCronDetail = null;
  _currentCronDetailKey = '';
  _cronMode = 'empty';
  _stopCronWatch();
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (title) title.textContent = '';
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  _setCronHeaderButtons('empty');
}

async function runCurrentCron(){ if (_currentCronDetail) await cronRun(_currentCronDetail.id); }
async function pauseCurrentCron(){ if (_currentCronDetail) await cronPause(_currentCronDetail.id); }
async function resumeCurrentCron(){ if (_currentCronDetail) await cronResume(_currentCronDetail.id); }
async function copyCurrentCronDiagnostics(){
  if (!_currentCronDetail) return;
  try {
    await _copyText(_cronDiagnostics(_currentCronDetail));
    showToast(t('cron_diagnostics_copied'));
  } catch(e) { showToast(t('copy_failed'), 4000); }
}
function editCurrentCron(){
  if (!_currentCronDetail) return;
  openCronEdit(_currentCronDetail);
}
function duplicateCurrentCron(){
  if (!_currentCronDetail) return;
  const job = _currentCronDetail;
  if (typeof switchPanel === 'function' && _currentPanel !== 'tasks') switchPanel('tasks');
  _cronPreFormDetail = { ...job };
  _editingCronId = null;
  _cronMode = 'create';
  _cronIsDuplicate = true;
  _cronSelectedSkills = Array.isArray(job.skills) ? [...job.skills] : [];
  // Deduplicate name: append "(copy)", "(copy 2)", "(copy 3)" etc.
  const baseName = job.name || '';
  let dupName = baseName + ' (copy)';
  if (_cronList && _cronList.length) {
    const taken = new Set(_cronList.filter(j => j.name).map(j => j.name));
    if (taken.has(dupName)) {
      let n = 2;
      while (taken.has(baseName + ' (copy ' + n + ')')) n++;
      dupName = baseName + ' (copy ' + n + ')';
    }
  }
  _renderCronForm({
    name: dupName,
    schedule: job.schedule_display || (job.schedule && job.schedule.expression) || '',
    prompt: job.prompt || '',
    deliver: job.deliver || 'local',
    profile: job.profile || '',
    toast_notifications: job.toast_notifications !== false,
    no_agent: !!job.no_agent,
    script: job.script || '',
    model: job.model || '',
    provider: job.provider || '',
    isEdit: false,
  });
  if (!_cronSkillsCache) {
    api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  } else {
    _bindCronSkillPicker();
  }
}
async function deleteCurrentCron(){
  if (!_currentCronDetail) return;
  const id = _currentCronDetail.id;
  const _ok = await showConfirmDialog({title:t('cron_delete_confirm_title'),message:t('cron_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_ok) return;
  try {
    await api('/api/crons/delete', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_deleted'));
    _clearCronDetail();
    await loadCrons();
  } catch(e) { showToast(t('delete_failed') + e.message, 4000); }
}
let _cronSelectedSkills=[];
let _cronIsDuplicate = false;
let _cronSkillsCache=null;
let _cronProfilesCache=null;
let _cronDeliveryOptionsCache=null;

function openCronCreate(){
  if (typeof switchPanel === 'function' && _currentPanel !== 'tasks') switchPanel('tasks');
  _cronPreFormDetail = _currentCronDetail ? { ..._currentCronDetail } : null;
  _editingCronId = null;
  _cronMode = 'create';
  _cronIsDuplicate = false;
  _cronSelectedSkills = [];
  _renderCronForm({ name:'', schedule:'0 9 * * *', prompt:'', deliver:'local', profile:'', toast_notifications:true, model:'', provider:'', isEdit:false });
  _cronSkillsCache = null;
  api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  loadCronProfiles().then(()=>_refreshCronProfileSelect('')).catch(()=>{});
}

function openCronEdit(job){
  if (!job) return;
  _cronPreFormDetail = { ...job };
  _editingCronId = job.id;
  _cronMode = 'edit';
  _cronSelectedSkills = Array.isArray(job.skills) ? [...job.skills] : [];
  _renderCronForm({
    name: job.name || '',
    schedule: job.schedule_display || (job.schedule && job.schedule.expression) || '',
    prompt: job.prompt || '',
    deliver: job.deliver || 'local',
    profile: job.profile || '',
    toast_notifications: job.toast_notifications !== false,
    no_agent: !!job.no_agent,
    script: job.script || '',
    model: job.model || '',
    provider: job.provider || '',
    isEdit: true,
  });
  if (!_cronSkillsCache) {
    api('/api/skills').then(d=>{_cronSkillsCache=d.skills||[]; _bindCronSkillPicker();}).catch(()=>{});
  } else {
    _bindCronSkillPicker();
  }
  loadCronProfiles().then(()=>_refreshCronProfileSelect(job.profile || '')).catch(()=>{});
}

function _renderCronForm({ name, schedule, prompt, deliver, profile, toast_notifications=true, no_agent=false, script='', model='', provider='', isEdit }){
  const title = $('taskDetailTitle');
  const body = $('taskDetailBody');
  const empty = $('taskDetailEmpty');
  if (!body || !title) return;
  const isNoAgent = !!no_agent;
  const toastNotifications = toast_notifications !== false;
  title.textContent = isEdit ? (t('edit') + ' · ' + (name || schedule || t('scheduled_jobs'))) : t('new_job');
  const promptBlock = isNoAgent ? '' : `
        <div class="detail-form-row">
          <label for="cronFormPrompt">${esc(t('cron_prompt_label') || 'Prompt')}</label>
          <textarea id="cronFormPrompt" rows="6" placeholder="${esc(t('cron_prompt_placeholder') || 'Must be self-contained')}" required>${esc(prompt || '')}</textarea>
        </div>`;
  const scriptBlock = isNoAgent ? `
        <div class="detail-form-row">
          <label for="cronFormScript">${esc(t('cron_script_path_label') || 'Script path')}</label>
          <input type="text" id="cronFormScript" value="${esc(script || '')}" readonly autocomplete="off">
          <div class="detail-form-hint">${esc(t('cron_script_path_hint') || 'Resolved under ~/.hermes/scripts/ unless an absolute path. Edit the script file on the server to change behavior.')}</div>
        </div>` : '';
  const skillsBlock = isNoAgent ? '' : `
        <div class="detail-form-row">
          <label for="cronFormSkillSearch">${esc(t('cron_skills_label') || 'Skills')}</label>
          <div class="skill-picker-wrap">
            <input type="text" id="cronFormSkillSearch" placeholder="${esc(t('cron_skills_placeholder') || 'Add skills (optional)...')}" autocomplete="off" ${isEdit ? 'disabled' : ''}>
            <div id="cronFormSkillDropdown" class="skill-picker-dropdown" style="display:none"></div>
            <div id="cronFormSkillTags" class="skill-picker-tags"></div>
          </div>
          ${isEdit ? `<div class="detail-form-hint">${esc(t('cron_skills_edit_hint') || 'Skill list is not editable after creation.')}</div>` : ''}
        </div>`;
  body.innerHTML = `
    <div class="main-view-content">
      ${isNoAgent ? _cronScriptJobBannerHtml() : ''}
      <form class="detail-form" onsubmit="event.preventDefault(); saveCronForm();">
        <div class="detail-form-row">
          <label for="cronFormName">${esc(t('cron_name_label') || 'Name')}</label>
          <input type="text" id="cronFormName" value="${esc(name || '')}" placeholder="${esc(t('cron_name_placeholder') || 'Optional')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="cronFormSchedulePreset">${esc(t('cron_schedule_preset_label') || 'Schedule')}</label>
          <div class="cron-schedule-preset-shell">
            <select id="cronFormSchedulePreset" class="cron-schedule-preset-select">
              ${_cronSchedulePresetOptionHtml()}
            </select>
            <div id="cronFormSchedulePresetParams" class="cron-schedule-preset-params" style="display:none">
              <div class="cron-schedule-preset-field" id="cronFormScheduleWeekdayField">
                <span class="cron-schedule-preset-conj" aria-hidden="true">${esc(t('cron_schedule_conj_on') || 'on')}</span>
                <select id="cronFormScheduleWeekday" aria-label="${esc(t('cron_schedule_weekday_label') || 'Day of week')}">
                  <option value="0">${esc(t('cron_weekday_sun') || 'Sunday')}</option>
                  <option value="1" selected>${esc(t('cron_weekday_mon') || 'Monday')}</option>
                  <option value="2">${esc(t('cron_weekday_tue') || 'Tuesday')}</option>
                  <option value="3">${esc(t('cron_weekday_wed') || 'Wednesday')}</option>
                  <option value="4">${esc(t('cron_weekday_thu') || 'Thursday')}</option>
                  <option value="5">${esc(t('cron_weekday_fri') || 'Friday')}</option>
                  <option value="6">${esc(t('cron_weekday_sat') || 'Saturday')}</option>
                </select>
              </div>
              <div class="cron-schedule-preset-field" id="cronFormScheduleMonthDayField">
                <span class="cron-schedule-preset-conj" aria-hidden="true">${esc(t('cron_schedule_conj_on_day') || 'on day')}</span>
                <select id="cronFormScheduleMonthDay" aria-label="${esc(t('cron_schedule_month_day_label') || 'Day of month')}">
                  ${Array.from({length:31},(_,i)=>`<option value="${i+1}">${i+1}</option>`).join('')}
                </select>
              </div>
              <div class="cron-schedule-preset-field" id="cronFormScheduleTimeField">
                <span class="cron-schedule-preset-conj" aria-hidden="true">${esc(t('cron_schedule_conj_at') || 'at')}</span>
                <input type="time" id="cronFormScheduleTime" value="09:00" step="60" autocomplete="off" aria-label="${esc(t('cron_schedule_time_label') || 'Time')}">
              </div>
              <div class="cron-schedule-preset-field" id="cronFormScheduleMinuteField">
                <span class="cron-schedule-preset-conj" aria-hidden="true">${esc(t('cron_schedule_conj_at_minute') || 'at minute')}</span>
                <input type="number" id="cronFormScheduleMinute" min="0" max="59" step="1" value="0" autocomplete="off" aria-label="${esc(t('cron_schedule_minute_label') || 'Minute')}">
              </div>
              <div class="detail-form-hint cron-schedule-preset-time-hint"><span id="cronFormSchedulePreview" class="cron-schedule-preview"></span>${esc(t('cron_schedule_time_hint') || 'Time is server time; cron runs server-side.')}</div>
            </div>
          </div>
        </div>
        <div class="detail-form-row" id="cronFormScheduleCustomRow" style="display:none">
          <label for="cronFormSchedule">${esc(t('cron_schedule_label') || 'Cron expression')}</label>
          <input type="text" id="cronFormSchedule" value="${esc(schedule || '')}" placeholder="0 9 * * *  —  every 1h  —  @daily" autocomplete="off" required>
          <div class="detail-form-hint">${esc(t('cron_schedule_hint') || "Cron expression or shorthand like 'every 1h'.")}</div>
          <div id="cronFormScheduleOnceWarning" class="detail-form-warning cron-once-warning" style="display:none">${esc(t('cron_schedule_once_warning') || "Duration forms like '30m' run once and are removed after running. Use 'every 30m' to keep a recurring job.")}</div>
        </div>
        ${scriptBlock}
        ${promptBlock}
        <div class="detail-form-row">
          <label for="cronFormDeliver">${esc(t('cron_deliver_label') || 'Deliver output to')}</label>
          <select id="cronFormDeliver">
            <option value="" disabled>loading...</option>
          </select>
        </div>
        <div class="detail-form-row">
          <label for="cronFormProfile">${esc(t('cron_profile_label') || 'Profile')}</label>
          <select id="cronFormProfile">
            ${_cronProfileOptions(profile)}
          </select>
          <div class="detail-form-hint">${esc(t('cron_profile_server_default_hint') || 'Uses the WebUI server default profile at run time')}</div>
        </div>
        <div class="detail-form-row">
          <label for="cronFormModel">${esc(t('cron_model_label') || 'Model')}</label>
          <select id="cronFormModel"${isNoAgent ? ' disabled' : ''}>
            <option value="">loading...</option>
          </select>
          <div class="detail-form-hint">${esc(isNoAgent ? (t('cron_model_no_agent_hint') || 'No-agent jobs run the configured script directly; model is unused.') : (t('cron_model_hint') || 'Use the profile default model at run time, or pin this job to a specific provider/model.'))}</div>
        </div>
        <div class="detail-form-row">
          <label for="cronFormToastNotifications">${esc(t('cron_toast_notifications_label') || 'Completion toasts')}</label>
          <label class="detail-form-check" for="cronFormToastNotifications">
            <input type="checkbox" id="cronFormToastNotifications" ${toastNotifications ? 'checked' : ''}>
            <span>${esc(t('cron_toast_notifications_hint') || 'Show a toast when this cron finishes.')}</span>
          </label>
        </div>
        ${skillsBlock}
        <div id="cronFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setCronHeaderButtons(isEdit ? 'edit' : 'create');
  _populateCronDeliverOptions(deliver, isEdit);
  _populateCronFormModelSelect(model, provider, isNoAgent);
  if (!isNoAgent) _renderCronSkillTags();
  _initCronSchedulePresetControls();
  const focusEl = $('cronFormName');
  if (focusEl) focusEl.focus();
}

async function _populateCronDeliverOptions(selectedValue, isEdit) {
  var sel = $('cronFormDeliver');
  if (!sel) return;
  sel.disabled = true;
  try {
    if (!_cronDeliveryOptionsCache) {
      var res = await api('/api/crons/delivery-options');
      _cronDeliveryOptionsCache = res && res.platforms ? res.platforms : [];
    }
    sel.innerHTML = '';
    for (var i = 0; i < _cronDeliveryOptionsCache.length; i++) {
      var p = _cronDeliveryOptionsCache[i];
      var opt = document.createElement('option');
      opt.value = p.value;
      opt.textContent = p.label;
      if (p.value === selectedValue) opt.selected = true;
      sel.appendChild(opt);
    }
    if (selectedValue && !sel.querySelector('option[value="' + CSS.escape(selectedValue) + '"]')) {
      var opt = document.createElement('option');
      opt.value = selectedValue;
      opt.textContent = selectedValue + ' *';
      opt.selected = true;
      sel.prepend(opt);
    }
  } catch (e) {
    sel.innerHTML = '<option value="local">Local (save output only)</option>';
  }
  sel.disabled = false;
}

async function _populateCronFormModelSelect(selectedModel, selectedProvider, disabled){
  const sel = $('cronFormModel');
  if (!sel) return;
  delete sel.dataset.loaded;
  sel.disabled = true;
  sel.innerHTML = `<option value="">${esc(t('cron_model_use_default') || 'Default (use profile/system default)')}</option>`;
  try {
    const data = await api('/api/models');
    const groups = (Array.isArray(data && data.groups) && data.groups.length) ? data.groups : [];
    for (const g of groups) {
      const og = document.createElement('optgroup');
      og.label = g.provider || g.provider_id || 'Configured';
      if (g.provider_id) og.dataset.provider = g.provider_id;
      for (const m of [...(Array.isArray(g.models) ? g.models : []), ...(Array.isArray(g.extra_models) ? g.extra_models : [])]) {
        if (!m || !m.id) continue;
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        if (g.provider_id) opt.dataset.provider = g.provider_id;
        og.appendChild(opt);
      }
      if (og.children.length) sel.appendChild(og);
    }

    let found = false;
    if (selectedModel) {
      if (typeof _applyModelToDropdown === 'function') {
        found = !!_applyModelToDropdown(selectedModel, sel, selectedProvider || null);
      }
      if (!found) {
        for (const opt of sel.options) {
          if (opt.value !== selectedModel) continue;
          const prov = opt.dataset.provider || (opt.parentElement && opt.parentElement.dataset.provider) || '';
          if (!selectedProvider || prov === selectedProvider) {
            opt.selected = true;
            found = true;
            break;
          }
        }
      }
    } else {
      found = true;
    }

    if (selectedModel && !found) {
      const opt = document.createElement('option');
      opt.value = selectedModel;
      opt.textContent = `${selectedModel} (${t('not_available') || 'not available'})`;
      if (selectedProvider) opt.dataset.provider = selectedProvider;
      opt.selected = true;
      sel.appendChild(opt);
    }
    sel.dataset.loaded = '1';
  } catch (e) {
    console.warn('Failed to load cron model picker:', e.message);
    // Load failed: dataset.loaded stays unset so saveCronForm omits model/provider
    // and preserves any existing override. Keep the select DISABLED rather than
    // re-enabling it showing only "Default" — an enabled "Default"-only select
    // would let the user think they cleared the override when a save actually
    // preserves it (Opus advisor, stage-345). A reopen retries the load.
    sel.disabled = true;
    return;
  }
  sel.disabled = !!disabled;
}

function _renderCronSkillTags(){
  const wrap=$('cronFormSkillTags');
  if(!wrap)return;
  wrap.innerHTML='';
  for(const name of _cronSelectedSkills){
    const tag=document.createElement('span');
    tag.className='skill-tag';
    tag.dataset.skill=name;
    const rm=document.createElement('span');
    rm.className='remove-tag';rm.textContent='×';
    rm.onclick=()=>{_cronSelectedSkills=_cronSelectedSkills.filter(s=>s!==name);tag.remove();};
    tag.appendChild(document.createTextNode(name));
    tag.appendChild(rm);
    wrap.appendChild(tag);
  }
}

function _bindCronSkillPicker(){
  const search=$('cronFormSkillSearch');
  const dropdown=$('cronFormSkillDropdown');
  if(!search||!dropdown)return;
  search.oninput=()=>{
    const q=search.value.trim().toLowerCase();
    if(!q||!_cronSkillsCache){dropdown.style.display='none';return;}
    const matches=_cronSkillsCache.filter(s=>
      !_cronSelectedSkills.includes(s.name)&&
      (s.name.toLowerCase().includes(q)||(s.category||'').toLowerCase().includes(q))
    ).slice(0,8);
    if(!matches.length){dropdown.style.display='none';return;}
    dropdown.innerHTML='';
    for(const s of matches){
      const opt=document.createElement('div');
      opt.className='skill-opt';
      opt.textContent=s.name+(s.category?' ('+s.category+')':'');
      opt.onclick=()=>{
        _cronSelectedSkills.push(s.name);
        _renderCronSkillTags();
        search.value='';
        dropdown.style.display='none';
      };
      dropdown.appendChild(opt);
    }
    dropdown.style.display='';
  };
  search.onblur=()=>setTimeout(()=>{dropdown.style.display='none';},150);
}

function cancelCronForm(){
  _editingCronId = null;
  if (_cronPreFormDetail) {
    const snap = _cronPreFormDetail;
    _cronPreFormDetail = null;
    _renderCronDetail(snap);
    return;
  }
  _cronPreFormDetail = null;
  _clearCronDetail();
}

function _cronModelBareName(model, provider) {
  // Strip @provider: prefix from a model value when provider is stored separately.
  // The model dropdown may contain values like "@custom:9router:chat" (from
  // _apply_provider_prefix) but cron jobs store model and provider separately,
  // so the model should be just "chat".
  if (model && provider && model.startsWith('@' + provider + ':')) {
    return model.slice(('@' + provider + ':').length);
  }
  return model;
}

async function saveCronForm(){
  const nameEl=$('cronFormName');
  const schEl=$('cronFormSchedule');
  const promptEl=$('cronFormPrompt');
  const delivEl=$('cronFormDeliver');
  const profileEl=$('cronFormProfile');
  const toastEl=$('cronFormToastNotifications');
  const errEl=$('cronFormError');
  if(!schEl||!errEl) return;
  const isNoAgent = !!(_cronPreFormDetail && _cronPreFormDetail.no_agent);
  if(!isNoAgent && !promptEl) return;
  const name=(nameEl?nameEl.value:'').trim();
  const schedule=schEl.value.trim();
  const prompt=promptEl ? promptEl.value.trim() : '';
  const deliver=delivEl?delivEl.value:'local';
  const profile=profileEl?profileEl.value:'';
  const toastNotifications=toastEl?!!toastEl.checked:true;
  errEl.style.display='none';
  if(!schedule){errEl.textContent=t('cron_schedule_required_example');errEl.style.display='';return;}
  if(!isNoAgent && !prompt){errEl.textContent=t('cron_prompt_required');errEl.style.display='';return;}
  try{
    const modelEl = $('cronFormModel');
    const modelLoaded = !!(modelEl && modelEl.dataset.loaded === '1');
    const selectedModel = modelEl ? (modelEl.value || '').trim() : '';
    if (_editingCronId) {
      const updates = {job_id: _editingCronId, schedule, profile: profile, toast_notifications: toastNotifications};
      if (!isNoAgent) updates.prompt = prompt;
      if (name) updates.name = name;
      if (deliver) updates.deliver = deliver;
      if (modelEl) {
        if (selectedModel && modelLoaded) {
          const modelState = (typeof _modelStateForSelect === 'function')
            ? _modelStateForSelect(modelEl, selectedModel)
            : { model: selectedModel, model_provider: null };
          updates.model = _cronModelBareName(modelState.model, modelState.model_provider) || null;
          updates.provider = modelState.model_provider || null;
        } else if (modelLoaded) {
          updates.model = null;
          updates.provider = null;
        }
        // else: select not yet populated — omit model/provider to preserve saved value
      }
      await api('/api/crons/update', {method:'POST', body: JSON.stringify(updates)});
      const editedId = _editingCronId;
      _editingCronId = null;
      _cronPreFormDetail = null;
      showToast(t('cron_job_updated'));
      await loadCrons();
      const job = _cronList && _cronList.find(j => j.id === editedId);
      if (job) openCronDetail(job);
      return;
    }
    const body={schedule,prompt,deliver,profile: profile, toast_notifications: toastNotifications};
    if(_cronIsDuplicate) body.enabled=false;
    if(name)body.name=name;
    if(_cronSelectedSkills.length)body.skills=_cronSelectedSkills;
    if (modelEl && modelLoaded) {
      if (selectedModel) {
        const modelState = (typeof _modelStateForSelect === 'function')
          ? _modelStateForSelect(modelEl, selectedModel)
          : { model: selectedModel, model_provider: null };
        body.model = _cronModelBareName(modelState.model, modelState.model_provider) || null;
        body.provider = modelState.model_provider || null;
      }
    } else if (_cronIsDuplicate && _cronPreFormDetail && _cronPreFormDetail.model) {
      body.model = _cronModelBareName(_cronPreFormDetail.model, _cronPreFormDetail.provider) || null;
      body.provider = _cronPreFormDetail.provider || null;
    }
    const res = await api('/api/crons/create',{method:'POST',body:JSON.stringify(body)});
    _cronPreFormDetail = null;
    _cronIsDuplicate = false;
    showToast(t('cron_job_created'));
    await loadCrons();
    const newId = res && (res.id || (res.job && res.job.id));
    if (newId) openCronDetail(newId);
    else if (_cronList && _cronList.length) openCronDetail(_cronList[_cronList.length - 1]);
  }catch(e){
    errEl.textContent=t('error_prefix')+e.message;errEl.style.display='';
  }
}

// Back-compat aliases for any stale callers
const submitCronCreate = saveCronForm;
function toggleCronForm(){ openCronCreate(); }

function _cronOutputSnippet(content) {
  // Extract the response body from a cron output .md file
  const lines = content.split('\n');
  const responseIdx = lines.findIndex(l => l.startsWith('## Response') || l.startsWith('# Response'));
  const body = (responseIdx >= 0 ? lines.slice(responseIdx + 1) : lines).join('\n').trim();
  return body.slice(0, 600) || '(empty)';
}

function _formatCronRunUsageStrip(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const parts = [];
  const fmt = n => {
    const value = Number(n || 0);
    if (!Number.isFinite(value) || value <= 0) return '';
    if (value >= 1000000) return (value / 1000000).toFixed(value >= 10000000 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (value >= 1000) return (value / 1000).toFixed(value >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return String(Math.round(value));
  };
  const input = fmt(usage.input_tokens);
  const output = fmt(usage.output_tokens);
  const total = fmt(usage.total_tokens);
  if (input || output) parts.push(`${input || '0'} in · ${output || '0'} out`);
  else if (total) parts.push(`${total} tokens`);
  const cost = Number(usage.estimated_cost_usd);
  if (Number.isFinite(cost) && cost > 0) parts.push(`$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)}`);
  if (usage.model) parts.push(String(usage.model));
  return parts.join(' · ');
}
// ── Cron run watch ────────────────────────────────────────────────────────────
let _cronWatchInterval = null;
let _cronWatchStart = null;
let _cronWatchTimerInterval = null;

function _startCronWatch(jobId, detailKey) {
  _stopCronWatch();
  _cronWatchStart = Date.now();
  _cronWatchInterval = setInterval(async () => {
    try {
      const data = await api(`/api/crons/status?job_id=${encodeURIComponent(jobId)}`,{timeoutToast:false});
      if (!data.running) {
        _stopCronWatch();
        if (_cronDetailMatches(jobId, detailKey)) {
          _loadCronDetailRuns(jobId, detailKey);
        }
        return;
      }
      // Still running — update elapsed
      if (_cronDetailMatches(jobId, detailKey)) {
        const el = $('cronRunningIndicator');
        if (el) el.querySelector('.cron-watch-elapsed').textContent = _formatElapsed(data.elapsed);
      }
    } catch(e) { /* ignore poll errors */ }
  }, 3000);
  // Timer update every second
  _cronWatchTimerInterval = setInterval(() => {
    if (_cronDetailMatches(jobId, detailKey) && _cronWatchStart) {
      const el = $('cronRunningIndicator');
      if (el) el.querySelector('.cron-watch-elapsed').textContent = _formatElapsed((Date.now() - _cronWatchStart) / 1000);
    }
  }, 1000);
  // Inject running indicator into detail card
  if (_cronDetailMatches(jobId, detailKey)) {
    _injectRunningIndicator();
  }
}

function _stopCronWatch() {
  if (_cronWatchInterval) { clearInterval(_cronWatchInterval); _cronWatchInterval = null; }
  if (_cronWatchTimerInterval) { clearInterval(_cronWatchTimerInterval); _cronWatchTimerInterval = null; }
  _cronWatchStart = null;
  const el = $('cronRunningIndicator');
  if (el) el.remove();
}

function _injectRunningIndicator() {
  const card = $('cronDetailRuns');
  if (!card || $('cronRunningIndicator')) return;
  const div = document.createElement('div');
  div.id = 'cronRunningIndicator';
  div.className = 'cron-running-indicator';
  div.innerHTML = `<span class="cron-watch-spinner"></span><span>${esc(t('cron_status_running'))}</span><span class="cron-watch-elapsed">0s</span>`;
  card.insertAdjacentElement('beforebegin', div);
}

function _formatElapsed(seconds) {
  if (seconds < 60) return Math.round(seconds) + 's';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m + 'm ' + s + 's';
}

function _checkCronWatchOnDetail(jobId, detailKey) {
  // When opening a detail view, check if job is running
  api(`/api/crons/status?job_id=${encodeURIComponent(jobId)}`,{timeoutToast:false}).then(data => {
    if (data.running && _cronDetailMatches(jobId, detailKey)) {
      _startCronWatch(jobId, detailKey);
    }
  }).catch(() => {});
}

async function cronRun(id) {
  try {
    await api('/api/crons/run', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_triggered'));
    _startCronWatch(id, _currentCronDetailKey);
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

async function cronPause(id) {
  try {
    await api('/api/crons/pause', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_paused'));
    await loadCrons();
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

async function cronResume(id) {
  try {
    await api('/api/crons/resume', {method:'POST', body: JSON.stringify({job_id: id})});
    showToast(t('cron_job_resumed'));
    await loadCrons();
  } catch(e) { showToast(t('failed_colon') + e.message, 4000); }
}

let _editingCronId = null;

window.HermesPanels.cronEditor = {
  _cronPanelExpandKey,
  _cronRunExpandKey,
  _cronExpansionGet,
  _cronExpansionSet,
  toggleCronPromptExpanded,
  toggleCronRunExpanded,
  _isCronScriptJob,
  _cronModeLabel,
  _cronOutputTitle,
  _cronScriptJobBannerHtml,
  _cronScriptCardHtml,
  _cronAgentPromptCardHtml,
  _renderCronDetail,
  _setCronHeaderButtons,
  _loadCronDetailRuns,
  _loadRunContent,
  openCronDetail,
  _clearCronDetail,
  runCurrentCron,
  pauseCurrentCron,
  resumeCurrentCron,
  copyCurrentCronDiagnostics,
  editCurrentCron,
  duplicateCurrentCron,
  deleteCurrentCron,
  openCronCreate,
  openCronEdit,
  _renderCronForm,
  _populateCronDeliverOptions,
  _populateCronFormModelSelect,
  _renderCronSkillTags,
  _bindCronSkillPicker,
  cancelCronForm,
  _cronModelBareName,
  saveCronForm,
  toggleCronForm,
  _cronOutputSnippet,
  _formatCronRunUsageStrip,
  _startCronWatch,
  _stopCronWatch,
  _injectRunningIndicator,
  _formatElapsed,
  _checkCronWatchOnDetail,
  cronRun,
  cronPause,
  cronResume,
};
