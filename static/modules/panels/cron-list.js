import { state } from "./state.js";
import { _clearCronDetail,_renderCronDetail,openCronDetail } from "./cron-editor.js";
import { _cronNewJobIds } from "./runtime-alerts.js";



// Panels domain: cron scheduling and list

// ── Cron panel ──
export function _isRecurringCronJob(job) {
  const kind = job && job.schedule && job.schedule.kind;
  return kind === 'cron' || kind === 'interval';
}

export function _cronScheduleKindForInput(value) {
  const schedule = String(value || '').trim();
  if (!schedule) return '';
  const lower = schedule.toLowerCase();
  if (lower.startsWith('every ')) return 'interval';
  if (lower.startsWith('@')) return 'cron';
  const parts = schedule.split(/\s+/);
  if (parts.length >= 5 && parts.slice(0, 5).every(p => /^[\d*\-,/]+$/.test(p))) return 'cron';
  if (schedule.includes('T') || /^\d{4}-\d{2}-\d{2}/.test(schedule)) return 'once';
  if (/^\d+\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$/i.test(schedule)) return 'once';
  return '';
}

export function _syncCronScheduleWarning() {
  const input = $('cronFormSchedule');
  const warning = $('cronFormScheduleOnceWarning');
  if (!input || !warning) return;
  warning.style.display = _cronScheduleKindForInput(input.value) === 'once' ? '' : 'none';
  _syncCronSchedulePreview();
}

// Live preview of the generated cron expression in the preset hint line (the
// cron-job.org / GitHub-schedule-editor convention) so a cron-literate user sees
// exactly what the friendly controls produce. Empty on Custom (the raw field is shown).
export function _syncCronSchedulePreview() {
  const preview = $('cronFormSchedulePreview');
  const presetEl = $('cronFormSchedulePreset');
  const scheduleEl = $('cronFormSchedule');
  if (!preview) return;
  const presetId = presetEl ? presetEl.value : 'custom';
  const expr = String((scheduleEl && scheduleEl.value) || '').trim();
  preview.textContent = (presetId !== 'custom' && expr) ? `${expr} · ` : '';
}

const CRON_SCHEDULE_PRESETS = [
  { id: 'hourly', label: 'cron_schedule_preset_hourly', fallback: 'Hourly', fields: ['minute'], defaults: { minute: 0 } },
  { id: 'daily', label: 'cron_schedule_preset_daily', fallback: 'Daily', fields: ['time'], defaults: { hour: 9, minute: 0 } },
  { id: 'weekdays', label: 'cron_schedule_preset_weekdays', fallback: 'Weekdays (Mon–Fri)', fields: ['time'], defaults: { hour: 9, minute: 0 } },
  { id: 'weekly', label: 'cron_schedule_preset_weekly', fallback: 'Weekly', fields: ['weekday', 'time'], defaults: { hour: 9, minute: 0, weekday: 1 } },
  { id: 'monthly', label: 'cron_schedule_preset_monthly', fallback: 'Monthly', fields: ['monthDay', 'time'], defaults: { hour: 9, minute: 0, monthDay: 1 } },
  { id: 'custom', label: 'cron_schedule_preset_custom', fallback: 'Custom', fields: [] },
];

export function _cronSchedulePresetOptionHtml() {
  return CRON_SCHEDULE_PRESETS
    .map((preset) => `<option value="${preset.id}">${esc(t(preset.label) || preset.fallback)}</option>`)
    .join('');
}

export function _cronSchedulePresetForId(presetId) {
  return CRON_SCHEDULE_PRESETS.find((entry) => entry.id === presetId) || null;
}

export function _cronSchedulePresetControlIds() {
  return {
    time: 'cronFormScheduleTime',
    minute: 'cronFormScheduleMinute',
    weekday: 'cronFormScheduleWeekday',
    monthDay: 'cronFormScheduleMonthDay',
  };
}

// Which visible control wrapper each logical field lives in. `hour`+`minute` for
// time-based presets share the single #cronFormScheduleTime picker (in the Time
// field); `minute` alone (Hourly) uses the standalone Minute field.
export function _cronSchedulePresetFieldWrapId(field) {
  if (field === 'time' || field === 'hour') return 'cronFormScheduleTimeField';
  if (field === 'minute') return 'cronFormScheduleMinuteField';
  if (field === 'weekday') return 'cronFormScheduleWeekdayField';
  if (field === 'monthDay') return 'cronFormScheduleMonthDayField';
  return '';
}

export function _cronSchedulePresetFieldId(field) {
  const ids = _cronSchedulePresetControlIds();
  return ids[field] || '';
}

export function _cronSchedulePresetFieldEl(field) {
  const id = _cronSchedulePresetFieldId(field);
  return id ? $(id) : null;
}

export function _cronSchedulePresetBounds(field) {
  if (field === 'hour') return { min: 0, max: 23 };
  if (field === 'minute') return { min: 0, max: 59 };
  if (field === 'weekday') return { min: 0, max: 6 };
  if (field === 'monthDay') return { min: 1, max: 31 };
  return { min: 0, max: 999 };
}

export function _cronSchedulePresetNormalizeValue(field, value, fallback) {
  const bounds = _cronSchedulePresetBounds(field);
  const parsed = parseInt(String(value ?? '').trim(), 10);
  const fallbackParsed = parseInt(String(fallback ?? bounds.min).trim(), 10);
  const safeFallback = Number.isFinite(fallbackParsed) ? fallbackParsed : bounds.min;
  const n = Number.isFinite(parsed) ? parsed : safeFallback;
  return String(Math.min(bounds.max, Math.max(bounds.min, n)));
}

export function _cronSchedulePresetValueForField(field, fallback) {
  // hour/minute for time-based presets come from the single #cronFormScheduleTime
  // picker ("HH:MM"); the standalone Minute box (Hourly) still reads directly.
  if (field === 'hour' || field === 'minute') {
    const timeEl = $('cronFormScheduleTime');
    const minuteBox = $('cronFormScheduleMinute');
    // Hourly uses the standalone minute box; if the Time picker isn't the visible
    // source for minute, prefer the minute box when it's the shown control.
    if (field === 'minute' && minuteBox && (!timeEl || _cronScheduleMinuteBoxIsActive())) {
      return _cronSchedulePresetNormalizeValue('minute', minuteBox.value, fallback);
    }
    if (timeEl && /^\d{1,2}:\d{2}$/.test(String(timeEl.value || '').trim())) {
      const [h, m] = String(timeEl.value).trim().split(':');
      return _cronSchedulePresetNormalizeValue(field, field === 'hour' ? h : m, fallback);
    }
    return _cronSchedulePresetNormalizeValue(field, fallback, fallback);
  }
  const el = _cronSchedulePresetFieldEl(field);
  const raw = el ? String(el.value || '').trim() : '';
  return _cronSchedulePresetNormalizeValue(field, raw, fallback);
}

// The standalone Minute box is the active minute source only for the Hourly preset
// (the only preset whose visible fields include a bare 'minute').
export function _cronScheduleMinuteBoxIsActive() {
  const presetEl = $('cronFormSchedulePreset');
  return !!(presetEl && presetEl.value === 'hourly');
}

export function _cronSchedulePresetRawFieldInBounds(field, value) {
  const raw = String(value || '').trim();
  if (!/^\d+$/.test(raw)) return false;
  const bounds = _cronSchedulePresetBounds(field);
  const parsed = parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= bounds.min && parsed <= bounds.max;
}

export function _cronSchedulePresetApplyValues(values) {
  // Write hour/minute into the single time picker as zero-padded HH:MM.
  if (values.hour != null || values.minute != null) {
    const timeEl = $('cronFormScheduleTime');
    if (timeEl) {
      const h = _cronSchedulePresetNormalizeValue('hour', values.hour, values.hour ?? 9);
      const m = _cronSchedulePresetNormalizeValue('minute', values.minute, values.minute ?? 0);
      timeEl.value = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
    }
  }
  if (values.minute != null) {
    const minuteBox = $('cronFormScheduleMinute');
    if (minuteBox) minuteBox.value = _cronSchedulePresetNormalizeValue('minute', values.minute, values.minute);
  }
  ['weekday', 'monthDay'].forEach((field) => {
    const el = _cronSchedulePresetFieldEl(field);
    if (!el || values[field] == null) return;
    el.value = _cronSchedulePresetNormalizeValue(field, values[field], values[field]);
  });
}

export function _cronSchedulePresetSyncVisibility(presetId) {
  const wrapper = $('cronFormSchedulePresetParams');
  const customRow = $('cronFormScheduleCustomRow');
  const preset = _cronSchedulePresetForId(presetId);
  const showFields = preset && Array.isArray(preset.fields) ? preset.fields : [];
  const isCustom = presetId === 'custom';
  // Preset param controls hide entirely on Custom; the raw cron expression row
  // shows ONLY on Custom (kept in the DOM as a hidden field otherwise).
  if (wrapper) wrapper.style.display = isCustom ? 'none' : '';
  if (customRow) customRow.style.display = isCustom ? '' : 'none';
  ['time', 'minute', 'weekday', 'monthDay'].forEach((field) => {
    const fieldWrap = $(_cronSchedulePresetFieldWrapId(field));
    if (fieldWrap) fieldWrap.style.display = showFields.includes(field) ? '' : 'none';
  });
}

export function _cronSchedulePresetValuesForSelection(presetId) {
  const preset = _cronSchedulePresetForId(presetId);
  const defaults = (preset && preset.defaults) || {};
  if (presetId === 'hourly') {
    return { minute: _cronSchedulePresetValueForField('minute', defaults.minute) };
  }
  if (presetId === 'daily' || presetId === 'weekdays') {
    return {
      hour: _cronSchedulePresetValueForField('hour', defaults.hour),
      minute: _cronSchedulePresetValueForField('minute', defaults.minute),
    };
  }
  if (presetId === 'weekly') {
    return {
      hour: _cronSchedulePresetValueForField('hour', defaults.hour),
      minute: _cronSchedulePresetValueForField('minute', defaults.minute),
      weekday: _cronSchedulePresetValueForField('weekday', defaults.weekday),
    };
  }
  if (presetId === 'monthly') {
    return {
      hour: _cronSchedulePresetValueForField('hour', defaults.hour),
      minute: _cronSchedulePresetValueForField('minute', defaults.minute),
      monthDay: _cronSchedulePresetValueForField('monthDay', defaults.monthDay),
    };
  }
  return {};
}

export function _cronSchedulePresetValueForSelection(presetId, selectedValues) {
  const values = selectedValues || _cronSchedulePresetValuesForSelection(presetId);
  if (presetId === 'hourly') return `${values.minute} * * * *`;
  if (presetId === 'daily') return `${values.minute} ${values.hour} * * *`;
  if (presetId === 'weekdays') return `${values.minute} ${values.hour} * * 1-5`;
  if (presetId === 'weekly') return `${values.minute} ${values.hour} * * ${values.weekday}`;
  if (presetId === 'monthly') return `${values.minute} ${values.hour} ${values.monthDay} * *`;
  return '';
}

export function _cronSchedulePresetStateForInput(value) {
  const schedule = String(value || '').trim();
  if (!schedule) return { presetId: 'custom' };
  if (/^every\s+1h$/i.test(schedule)) {
    return { presetId: 'hourly', minute: '0' };
  }
  if (schedule.startsWith('@')) return { presetId: 'custom' };
  const parts = schedule.split(/\s+/);
  if (parts.length !== 5) return { presetId: 'custom' };
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  if (!_cronSchedulePresetRawFieldInBounds('minute', minute)) return { presetId: 'custom' };
  if (_cronSchedulePresetRawFieldInBounds('hour', hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return { presetId: 'daily', minute, hour };
  }
  if (_cronSchedulePresetRawFieldInBounds('hour', hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '1-5') {
    return { presetId: 'weekdays', minute, hour };
  }
  if (hour === '*' && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return { presetId: 'hourly', minute };
  }
  if (_cronSchedulePresetRawFieldInBounds('hour', hour) && dayOfMonth === '*' && month === '*' && (_cronSchedulePresetRawFieldInBounds('weekday', dayOfWeek) || dayOfWeek === '7')) {
    return { presetId: 'weekly', minute, hour, weekday: dayOfWeek === '7' ? '0' : dayOfWeek };
  }
  if (_cronSchedulePresetRawFieldInBounds('hour', hour) && _cronSchedulePresetRawFieldInBounds('monthDay', dayOfMonth) && month === '*' && dayOfWeek === '*') {
    return { presetId: 'monthly', minute, hour, monthDay: dayOfMonth };
  }
  return { presetId: 'custom' };
}

export function _cronSchedulePresetIdForValue(value) {
  return _cronSchedulePresetStateForInput(value).presetId;
}

export function _syncCronSchedulePresetFromInput() {
  const presetEl = $('cronFormSchedulePreset');
  const scheduleEl = $('cronFormSchedule');
  if (!presetEl || !scheduleEl) return;
  const state = _cronSchedulePresetStateForInput(scheduleEl.value);
  presetEl.value = state.presetId;
  _cronSchedulePresetSyncVisibility(state.presetId);
  if (state.presetId !== 'custom') _cronSchedulePresetApplyValues(state);
}

export function _syncCronSchedulePresetAndWarning() {
  _syncCronSchedulePresetFromInput();
  _syncCronScheduleWarning();
}

export function _applyCronSchedulePresetSelection() {
  const presetEl = $('cronFormSchedulePreset');
  const scheduleEl = $('cronFormSchedule');
  if (!presetEl || !scheduleEl) return;
  const presetId = presetEl.value;
  if (presetId !== 'custom') {
    const values = _cronSchedulePresetValuesForSelection(presetId);
    _cronSchedulePresetApplyValues(values);
    scheduleEl.value = _cronSchedulePresetValueForSelection(presetId, values);
    _cronSchedulePresetSyncVisibility(presetId);
    _syncCronScheduleWarning();
    return;
  }
  _cronSchedulePresetSyncVisibility(presetId);
  _syncCronScheduleWarning();
}

// Regenerate the cron expression from the current field values WITHOUT writing the
// clamped values back into the field the user is editing — so typing into the Minute
// box (or the time picker) doesn't snap a half-entered value to the default/clamp
// mid-keystroke. Value clamping still happens on `change`/blur via the full apply.
export function _regenCronScheduleFromFields() {
  const presetEl = $('cronFormSchedulePreset');
  const scheduleEl = $('cronFormSchedule');
  if (!presetEl || !scheduleEl) return;
  const presetId = presetEl.value;
  if (presetId === 'custom') return;
  scheduleEl.value = _cronSchedulePresetValueForSelection(presetId);
  _syncCronScheduleWarning();
}

export function _initCronSchedulePresetControls() {
  const presetEl = $('cronFormSchedulePreset');
  const scheduleEl = $('cronFormSchedule');
  if (!presetEl || !scheduleEl) return;
  presetEl.addEventListener('change', _applyCronSchedulePresetSelection);
  if ($('cronFormSchedulePresetParams')) {
    ['cronFormScheduleTime', 'cronFormScheduleMinute', 'cronFormScheduleWeekday', 'cronFormScheduleMonthDay'].forEach((id) => {
      const el = $(id);
      if (!el) return;
      // On `change`/blur, clamp + normalize (writes values back). On `input`
      // (per-keystroke), only regenerate the expression — never rewrite the field
      // being typed into, so partial input like clearing Hour to type "14" isn't
      // snapped to the default (#5554 UX fix).
      el.addEventListener('change', _applyCronSchedulePresetSelection);
      el.addEventListener('input', _regenCronScheduleFromFields);
    });
  }
  // On raw-cron `input` (only reachable on Custom, where the raw row is shown),
  // update ONLY the warning + preview — do NOT re-detect the preset or sync
  // visibility, or a partial value momentarily matching a preset (e.g. typing
  // "0 9 * * 1,3" transiently equals Weekly's "0 9 * * 1") would switch the preset
  // and hide the focused raw field mid-keystroke (#5554). Preset re-detection runs
  // on initial render and on `change`/blur.
  scheduleEl.addEventListener('input', _syncCronScheduleWarning);
  scheduleEl.addEventListener('change', _syncCronSchedulePresetAndWarning);
  _syncCronSchedulePresetAndWarning();
}
export function _hasUnlimitedRepeat(job) {
  return !!(job && job.repeat && job.repeat.times == null);
}

export function _isCronNeedsAttention(job) {
  return _isRecurringCronJob(job) &&
    _hasUnlimitedRepeat(job) &&
    job.enabled === false &&
    job.state === 'completed' &&
    !job.next_run_at;
}

export function _isCronScheduleError(job) {
  return _isRecurringCronJob(job) &&
    !job.next_run_at &&
    (job.state === 'error' || job.last_status === 'error');
}

export function _cronStatusMeta(job) {
  if (_isCronNeedsAttention(job)) return {
    state: 'needs_attention',
    listClass: 'attention',
    detailClass: 'warn',
    label: t('cron_status_needs_attention'),
  };
  if (_isCronScheduleError(job)) return {
    state: 'schedule_error',
    listClass: 'attention',
    detailClass: 'warn',
    label: t('cron_status_needs_attention'),
  };
  if (job.state === 'paused') return {
    state: 'paused',
    listClass: 'paused',
    detailClass: 'warn',
    label: t('cron_status_paused'),
  };
  if (job.enabled === false) return {
    state: 'off',
    listClass: 'disabled',
    detailClass: 'warn',
    label: t('cron_status_off'),
  };
  if (job.last_status === 'error') return {
    state: 'error',
    listClass: 'error',
    detailClass: 'err',
    label: t('cron_status_error'),
  };
  return {
    state: 'active',
    listClass: 'active',
    detailClass: 'ok',
    label: t('cron_status_active'),
  };
}


export function _cronProfileName(profile){
  return (profile || '').toString().trim();
}

export function _cronProfileLabel(profile){
  const name = _cronProfileName(profile);
  return name || (t('cron_profile_server_default') || 'server default');
}

export function _cronProfileTitle(profile){
  const name = _cronProfileName(profile);
  if (name) return (t('cron_profile_label') || 'Profile') + ': ' + name;
  return t('cron_profile_server_default_hint') || 'Uses the WebUI server default profile at run time';
}

export function _cronOwnerProfileName(job){
  return _cronProfileName(job && (job.owner_profile ?? job.profile));
}

export function _cronJobKey(job){
  return `${_cronOwnerProfileName(job)}\u0000${String(job && job.id || '')}`;
}

export function _cronItemId(job){
  return 'cron-' + encodeURIComponent(_cronJobKey(job));
}

export function _cronDetailMatches(jobId, detailKey){
  return !!(
    detailKey &&
    state._currentCronDetail &&
    !state._currentCronDetail.read_only &&
    String(state._currentCronDetail.id) === String(jobId) &&
    state._currentCronDetailKey === detailKey &&
    _cronJobKey(state._currentCronDetail) === detailKey
  );
}

export function _findCronJob(jobOrId){
  if (jobOrId && typeof jobOrId === 'object') return jobOrId;
  const id = String(jobOrId || '');
  if (!state._cronList || !id) return null;
  return state._cronList.find(j => !j.read_only && String(j.id) === id) ||
    state._cronList.find(j => String(j.id) === id) ||
    null;
}

export function _appendCronProfileToggle(parent){
  if (!parent || (!state._showAllCronProfiles && state._cronOtherProfileCount <= 0)) return;
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding:10px 0 0';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'sm-btn';
  btn.style.cssText = 'width:100%;justify-content:center';
  btn.textContent = state._showAllCronProfiles
    ? 'Show active profile only'
    : `Show ${state._cronOtherProfileCount} from other profiles`;
  btn.onclick = async () => {
    state._showAllCronProfiles = !state._showAllCronProfiles;
    await loadCrons();
  };
  wrap.appendChild(btn);
  parent.appendChild(wrap);
}

export async function loadCronProfiles(){
  if (state._cronProfilesCache) return state._cronProfilesCache;
  try {
    const data = await api('/api/profiles');
    state._cronProfilesCache = Array.isArray(data.profiles) ? data.profiles : [];
  } catch(e) {
    state._cronProfilesCache = [];
  }
  return state._cronProfilesCache;
}

export function _cronProfileOptions(selected){
  const current = _cronProfileName(selected);
  const profiles = Array.isArray(state._cronProfilesCache) ? state._cronProfilesCache : [];
  const seen = new Set(['']);
  const opts = [`<option value=""${current ? '' : ' selected'}>${esc(t('cron_profile_server_default') || 'server default')}</option>`];
  for (const p of profiles) {
    const name = _cronProfileName(p && p.name);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const label = p && p.is_default ? `${name} (${t('default') || 'default'})` : name;
    opts.push(`<option value="${esc(name)}"${current === name ? ' selected' : ''}>${esc(label)}</option>`);
  }
  if (current && !seen.has(current)) {
    opts.push(`<option value="${esc(current)}" selected>${esc(current)} (${esc(t('not_available') || 'not available')})</option>`);
  }
  return opts.join('');
}

export function _refreshCronProfileSelect(selected){
  const sel = $('cronFormProfile');
  if (!sel) return;
  const keep = selected === undefined ? sel.value : selected;
  sel.innerHTML = _cronProfileOptions(keep);
}

export function _cronDiagnostics(job) {
  const fields = {
    id: job.id,
    name: job.name || null,
    schedule: job.schedule || null,
    schedule_display: job.schedule_display || null,
    enabled: job.enabled,
    state: job.state,
    next_run_at: job.next_run_at || null,
    last_run_at: job.last_run_at || null,
    last_status: job.last_status || null,
    last_error: job.last_error || null,
    last_delivery_error: job.last_delivery_error || null,
    repeat: job.repeat || null,
    deliver: job.deliver || null,
  };
  return JSON.stringify(fields, null, 2);
}

export function _gatewayStatusReason(status) {
  const health = status && typeof status.health === 'object' ? status.health : null;
  if (!health) return '';
  return typeof health.reason === 'string' ? health.reason.trim() : '';
}

export function _cronGatewayNoticeHtml(status) {
  if (!status || (status.configured && status.running)) return '';
  const reason = _gatewayStatusReason(status);
  const isStaleMetadata = reason === 'gateway_stale_running_state';
  const isRemoteUnreachable = reason === 'remote_gateway_unreachable';
  const notConfigured = !status.configured;
  const title = notConfigured
    ? 'Gateway not configured'
    : isStaleMetadata
      ? 'Gateway metadata stale'
      : isRemoteUnreachable
        ? 'Gateway endpoint not reachable'
        : 'Gateway not running';
  const body = notConfigured
    ? 'In Hermes WebUI, scheduled jobs require the Hermes gateway daemon. If this is a single-container Docker install, jobs can be created and run manually here, but scheduled ticks need a gateway container or `hermes gateway` running outside the WebUI.'
    : isStaleMetadata
      ? 'The gateway is marked as configured, but its health metadata has gone stale. In Docker, scheduled jobs require a live gateway daemon that refreshes runtime metadata while ticking cron.'
      : isRemoteUnreachable
        ? 'The gateway health endpoint is not reachable from WebUI. Verify the configured gateway URL env var (`GATEWAY_HEALTH_URL`, `HERMES_GATEWAY_HEALTH_URL`, `HERMES_API_URL`, or `HERMES_WEBUI_GATEWAY_BASE_URL`) points to a reachable gateway service and network path before relying on cron ticking.'
        : 'In Hermes WebUI, scheduled jobs require the Hermes gateway daemon to be running. Start the gateway container or `hermes gateway` before relying on offline scheduled runs.';
  const docsHref = 'https://github.com/nesquena/hermes-webui/blob/master/docs/docker.md#scheduled-jobs-and-the-gateway-daemon';
  const helpLink = notConfigured || isRemoteUnreachable || isStaleMetadata
    ? `<p><a href="${docsHref}" target="_blank" rel="noopener">How to enable scheduled jobs in Docker ↗</a></p>`
    : '';
  return `
    <div class="detail-alert-title">${esc(title)}</div>
    <p>${esc(body)}</p>
    ${helpLink}
  `;
}

export async function loadCronGatewayNotice() {
  const box = $('cronGatewayNotice');
  if (!box) return;
  try {
    const status = await api('/api/gateway/status');
    const html = _cronGatewayNoticeHtml(status);
    if (html) {
      box.innerHTML = html;
      box.style.display = '';
    } else {
      box.innerHTML = '';
      box.style.display = 'none';
    }
  } catch (_) {
    box.innerHTML = '';
    box.style.display = 'none';
  }
}

export async function loadCrons(animate) {
  const box = $('cronList');
  const refreshBtn = $('cronRefreshBtn');
  loadCronGatewayNotice();
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  try {
    await loadCronProfiles();
    const allProfilesQS = state._showAllCronProfiles ? '?all_profiles=1' : '';
    const data = await api('/api/crons' + allProfilesQS);
    state._cronList = data.jobs || [];
    state._cronOtherProfileCount = Number(data.other_profile_count || 0);
    if (state._showAllCronProfiles && !state._cronList.some(job => job && job.read_only)) {
      state._showAllCronProfiles = false;
      state._cronOtherProfileCount = 0;
    }
    box.innerHTML = '';
    // Partition active vs paused so paused jobs don't drown the list (#4026).
    // state._cronList stays the single source of truth — only the render is split,
    // which keeps openCronDetail, _cronNewJobIds, and detail refresh untouched.
    const _activeJobs = [];
    const _pausedJobs = [];
    for (const job of state._cronList) {
      const status = _cronStatusMeta(job);
      (status.state === 'paused' ? _pausedJobs : _activeJobs).push({ job, status });
    }
    const _appendCronItem = (parent, { job, status }) => {
      const item = document.createElement('div');
      item.className = 'cron-item';
      item.id = _cronItemId(job);
      if (job.read_only) {
        item.classList.add('readonly');
        item.style.opacity = '0.78';
      }
      const isNewRun = !job.read_only && _cronNewJobIds.has(String(job.id));
      const isAgentMode = !job.no_agent;
      const ownerProfileLabel = _cronProfileLabel(_cronOwnerProfileName(job));
      const ownerProfileTitle = `Owner profile: ${ownerProfileLabel}`;
      const readOnlyBadge = job.read_only
        ? '<span class="cron-status disabled" title="Read-only from another profile">Read-only</span>'
        : '';
      item.innerHTML = `
        <div class="cron-header">
          ${isNewRun ? '<span class="cron-new-dot" title="New run"></span>' : ''}
          ${isAgentMode ? '<span class="cron-agent-badge" title="Agent mode">🤖</span>' : `<span class="cron-script-badge" title="${esc(t('cron_script_badge_title') || 'Script job (no agent)')}">📜</span>`}
          <span class="cron-name" title="${esc(job.name)}">${esc(job.name)}</span>
          <span class="cron-profile-badge" title="${esc(ownerProfileTitle)}">${esc(ownerProfileLabel)}</span>
          <span class="cron-status ${status.listClass}">${esc(status.label)}</span>
          ${readOnlyBadge}
        </div>`;
      item.onclick = () => openCronDetail(job, item);
      if (state._currentCronDetailKey && state._currentCronDetailKey === _cronJobKey(job)) item.classList.add('active');
      parent.appendChild(item);
    };
    if (!state._cronList.length) {
      const emptyText = (!state._showAllCronProfiles && state._cronOtherProfileCount > 0)
        ? 'No cron jobs in the active profile.'
        : (t('cron_no_jobs') || 'No jobs yet');
      box.innerHTML = `<div style="padding:16px;color:var(--muted);font-size:12px">${esc(emptyText)}</div>`;
      _appendCronProfileToggle(box);
      if (state._cronMode !== 'create' && state._cronMode !== 'edit') _clearCronDetail();
      return;
    }
    for (const entry of _activeJobs) _appendCronItem(box, entry);
    if (_pausedJobs.length) {
      let collapsed = true;
      try { collapsed = localStorage.getItem('cron-paused-collapsed') !== '0'; } catch (_e) {}
      const details = document.createElement('details');
      details.className = 'cron-paused-section';
      if (!collapsed) details.open = true;
      const pausedLabel = t('cron_status_paused') || 'paused';
      const headerLabel = pausedLabel.charAt(0).toUpperCase() + pausedLabel.slice(1);
      const summary = document.createElement('summary');
      summary.className = 'cron-paused-summary';
      summary.textContent = `${headerLabel} (${_pausedJobs.length})`;
      details.appendChild(summary);
      details.addEventListener('toggle', () => {
        try { localStorage.setItem('cron-paused-collapsed', details.open ? '0' : '1'); } catch (_e) {}
      });
      const inner = document.createElement('div');
      inner.className = 'cron-paused-inner';
      details.appendChild(inner);
      for (const entry of _pausedJobs) _appendCronItem(inner, entry);
      box.appendChild(details);
    }
    _appendCronProfileToggle(box);
    // Re-render current detail with fresh data if we have one and we're not in a form
    if (state._currentCronDetail && state._cronMode !== 'create' && state._cronMode !== 'edit') {
      const refreshed = state._cronList.find(j => _cronJobKey(j) === state._currentCronDetailKey);
      if (refreshed) _renderCronDetail(refreshed);
      else _clearCronDetail();
    }
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`; }
  finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
  }
}
