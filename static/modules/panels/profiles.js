import { state } from "./state.js";
import { _closeMobileSidebarAfterPanelSelection,_syncMobileSidebarPanelFromMainView,switchPanel } from "./core.js";
import { _clearCronDetail } from "./cron-editor.js";
import { loadCrons } from "./cron-list.js";
import { loadKanban } from "./kanban-board.js";
import { _invalidateKanbanProfileCache } from "./kanban-tasks.js";
import { _resetCronUnreadForProfileSwitch } from "./runtime-alerts.js";
import { _applyTabOrder,_applyTabVisibility,_ensureComposerControlVisibilityState,_renderComposerControlChips,_renderComposerSituationalControlChips,_setComposerControlOrder,_setHiddenTabs,_setTabOrder } from "./settings-state.js";
import { loadMemory,loadSkills } from "./skills-memory.js";
import { _positionProfileDropdown,closeWsDropdown,loadWorkspaceList,loadWorkspacesPanel } from "./workspaces.js";

// Panels domain: profile rendering, switching, and editing

// ── Profile panel + dropdown ──
const PROFILE_DROPDOWN_CACHE_KEY = 'hermes-webui-profile-dropdown-cache-v1';
const PROFILE_DROPDOWN_CACHE_TTL_MS = 5 * 60 * 1000;

export function _profileDropdownClearStoredCache(){
  try{localStorage.removeItem(PROFILE_DROPDOWN_CACHE_KEY);}catch(_){}
}

export function _profileDropdownDataCacheUsable(data){
  return !!(
    data &&
    Array.isArray(data.profiles) &&
    data.profiles.length &&
    data.profiles.every(p=>
      p &&
      typeof p.name==='string' &&
      // Renderer-read fields must be safe types: renderProfileDropdown /
      // renderProfilesPanel call p.model.split('/') guarded only by truthiness,
      // so a poisoned cached row like {name:"x", model:{}} would pass a
      // name-only check yet throw synchronously on dropdown open (bricking
      // profile switching). Reject rows whose model is a non-string truthy value.
      (p.model==null || typeof p.model==='string')
    )
  );
}

export function _profileDropdownCacheUsable(data){
  return !!(_profileDropdownDataCacheUsable(data) && data.single_profile_mode !== true);
}

export function _profileDropdownReadStoredCache(){
  if(state._profileDropdownCacheLoadedFromStorage) return _profileDropdownCacheUsable(state._profilesCache) ? state._profilesCache : null;
  state._profileDropdownCacheLoadedFromStorage = true;
  try{
    const raw=localStorage.getItem(PROFILE_DROPDOWN_CACHE_KEY);
    if(!raw) return null;
    const parsed=JSON.parse(raw);
    if(!parsed || typeof parsed.ts!=='number' || !parsed.data) { _profileDropdownClearStoredCache(); return null; }
    if(Date.now()-parsed.ts>PROFILE_DROPDOWN_CACHE_TTL_MS) { _profileDropdownClearStoredCache(); return null; }
    if(!_profileDropdownCacheUsable(parsed.data)) { _profileDropdownClearStoredCache(); return null; }
    state._profilesCache = parsed.data;
    return state._profilesCache;
  }catch(_){_profileDropdownClearStoredCache();return null;}
}

export function _profileDropdownWriteStoredCache(data){
  if(!_profileDropdownCacheUsable(data)) { _profileDropdownClearStoredCache(); return; }
  try{localStorage.setItem(PROFILE_DROPDOWN_CACHE_KEY, JSON.stringify({ts:Date.now(), data}));}catch(_){}
}

export function _profileDropdownBestCachedData(){
  if(_profileDropdownCacheUsable(state._profilesCache)) return state._profilesCache;
  if(_profileDropdownDataCacheUsable(state._profilesCache)) return null;
  state._profilesCache = null;
  return _profileDropdownReadStoredCache();
}

export function _profileDropdownFetchFresh(){
  if(state._profileDropdownFetchPromise) return state._profileDropdownFetchPromise;
  state._profileDropdownFetchPromise = api('/api/profiles', {timeoutToast:false}).then(data=>{
    if(_profileDropdownDataCacheUsable(data)) state._profilesCache = data;
    _profileDropdownWriteStoredCache(data);
    return data;
  }).finally(()=>{ state._profileDropdownFetchPromise = null; });
  return state._profileDropdownFetchPromise;
}

export function _warmProfileDropdownCache(){
  _profileDropdownBestCachedData();
  _profileDropdownFetchFresh().catch(()=>{});
}

if(typeof window!=='undefined'){
  window.addEventListener('load',()=>{
    setTimeout(()=>{
      if(typeof document==='undefined'||!document.hidden) _warmProfileDropdownCache();
    },1200);
  },{once:true});
}

export function _renderProfileDropdownLoading(){
  const dd=$('profileDropdown');
  if(!dd)return;
  dd.innerHTML=`<div class="profile-opt profile-opt-loading"><div class="profile-opt-name">${esc(t('loading')||'Loading...')}</div></div>`;
}

export function _openProfileDropdownShell(){
  const dd=$('profileDropdown');
  if(!dd)return;
  dd.classList.add('open');
  _positionProfileDropdown();
  const chip=$('profileChip');
  if(chip && state._profileDropdownTrigger===chip) chip.classList.add('active');
  const tbtn=$('titlebarProfileBtn');
  if(tbtn && state._profileDropdownTrigger===tbtn) tbtn.classList.add('active');
}

export async function _profileSwitchPanelLoad(){
  // Cross-profile cron visibility is an active-profile opt-in; never carry it
  // into the next profile when the Tasks panel wasn't the visible panel.
  state._showAllCronProfiles = false;
  state._cronOtherProfileCount = 0;
  state._cronPreFormDetail = null;
  state._editingCronId = null;
  state._cronIsDuplicate = false;
  _clearCronDetail();
  if (state._currentPanel === 'skills') await loadSkills();
  if (state._currentPanel === 'memory') await loadMemory();
  if (state._currentPanel === 'tasks') await loadCrons();
  if (state._currentPanel === 'kanban') await loadKanban();
  if (state._currentPanel === 'profiles') await loadProfilesPanel();
  if (state._currentPanel === 'workspaces') await loadWorkspacesPanel();
}

export function _refreshProfileSwitchBackground(gen){
  window._modelDropdownReady=null;
  // A cross-profile sidebar click immediately calls loadSession(), whose
  // post-paint session_visit refresh is the authoritative model-catalog load.
  // Starting the generic profile refresh here as well duplicates a potentially
  // multi-second /api/models rebuild while the conversation is opening.
  const openingExistingSidebarSession = !!(
    typeof _profileSwitchOpeningExistingSession !== 'undefined'
    && _profileSwitchOpeningExistingSession
  );
  if (!openingExistingSidebarSession && typeof window._ensureModelDropdownReady === 'function') {
    Promise.resolve(window._ensureModelDropdownReady()).catch(()=>{});
  }
  Promise.resolve(loadWorkspaceList()).then(()=>{
    if (gen !== state._profileSwitchGeneration) return;
    if (S.session && typeof syncTopbar === 'function') syncTopbar();
  }).catch(()=>{});
  // Reconcile per-profile sidebar tab visibility. hidden_tabs is a per-profile
  // appearance setting; without this fetch, Profile A's hidden-tabs choice
  // would remain in effect under Profile B until the user opens Settings.
  // Stage-394 follow-up to #2636 deep review.
  Promise.resolve(api('/api/settings')).then(function(s){
    if (gen !== state._profileSwitchGeneration) return;
    var hidden = (s && Array.isArray(s.hidden_tabs)) ? s.hidden_tabs : [];
    hidden = hidden.filter(function(x){ return typeof x === 'string' && x.trim(); });
    var order = (s && Array.isArray(s.tab_order)) ? s.tab_order : [];
    order = order.filter(function(x){ return typeof x === 'string' && x.trim(); });
    if (typeof _setHiddenTabs === 'function') _setHiddenTabs(hidden);
    if (typeof _setTabOrder === 'function') _setTabOrder(order);
    if (typeof _applyTabOrder === 'function') _applyTabOrder(order);
    if (typeof _applyTabVisibility === 'function') _applyTabVisibility(hidden);
    _ensureComposerControlVisibilityState(s||{});
    if(Array.isArray(s&&s.composer_control_order)){
      const nextOrder=_setComposerControlOrder(s.composer_control_order);
      if(typeof window._applyComposerControlOrder==='function') window._applyComposerControlOrder(nextOrder);
    }
    _renderComposerControlChips();
    _renderComposerSituationalControlChips();
    if(typeof _applyComposerFooterVisibilitySettings==='function') _applyComposerFooterVisibilitySettings();
    window._showTitlebarProfile=!!(s&&s.show_titlebar_profile);
    if(typeof _applyTitlebarProfileVisibility==='function') _applyTitlebarProfileVisibility();
  }).catch(function(){});
}

export async function loadProfilesPanel() {
  const panel = $('profilesPanel');
  if (!panel) return;
  try {
    const data = await api('/api/profiles');
    state._profilesCache = data;
    _profileDropdownWriteStoredCache(data);
    panel.innerHTML = '';

    // Hide "New profile" button in single profile mode
    const newProfileBtn = document.querySelector('[onclick="openProfileCreate()"]');
    if (newProfileBtn) {
      newProfileBtn.style.display = data.single_profile_mode ? 'none' : '';
    }

    // In single profile mode, don't show the explanatory card
    if (!data.single_profile_mode) {
      const explainer = document.createElement('div');
      explainer.className = 'profile-card profile-help-card';
      explainer.innerHTML = `
        <div class="profile-card-header">
          <div style="min-width:0;flex:1">
            <div class="profile-card-name">${esc(t('profile_concept_title'))}</div>
            <div class="profile-card-meta">${esc(t('profile_concept_subtitle'))}</div>
          </div>
        </div>`;
      explainer.onclick = () => _renderProfileConceptHelp(data.active || 'default');
      panel.appendChild(explainer);
    }

    if (!data.profiles || !data.profiles.length) {
      const emptyMsg = document.createElement('div');
      emptyMsg.style.cssText = 'padding:16px;color:var(--muted);font-size:12px';
      emptyMsg.textContent = t('profiles_no_profiles');
      panel.appendChild(emptyMsg);
      if (state._profileMode !== 'create') _clearProfileDetail();
      return;
    }
    const activeName = (S.activeProfile && data.profiles.some(p => p.name === S.activeProfile))
      ? S.activeProfile
      : (data.active || 'default');
    for (const p of data.profiles) {
      const card = document.createElement('div');
      card.className = 'profile-card';
      card.dataset.name = p.name;
      const meta = [];
      if (typeof p.model === 'string' && p.model) meta.push(p.model.split('/').pop());
      if (p.provider) meta.push(p.provider);
      if (p.total_skills && p.total_skills > 0) meta.push(t('profile_skill_count', p.total_skills).replace(String(p.total_skills), `${p.enabled_skills} / ${p.total_skills}`));
      const gwDot = p.gateway_running
        ? `<span class="profile-opt-badge running" title="${esc(t('profile_gateway_running'))}"></span>`
        : `<span class="profile-opt-badge stopped" title="${esc(t('profile_gateway_stopped'))}"></span>`;
      const isActive = p.name === activeName;
      const activeBadge = isActive ? `<span style="color:var(--link);font-size:10px;font-weight:600;margin-left:6px">${esc(t('profile_active'))}</span>` : '';
      const defaultBadge = p.is_default ? ` <span style="opacity:.5">${esc(t('profile_default_label'))}</span>` : '';
      const hiddenBadge = p.visible === false ? ' <span class="detail-badge" title="Hidden from chat">Hidden from chat</span>' : '';
      card.innerHTML = `
        <div class="profile-card-header">
          <div style="min-width:0;flex:1">
            <div class="profile-card-name${isActive ? ' is-active' : ''}">${gwDot}${esc(p.name)}${defaultBadge}${activeBadge}${hiddenBadge}</div>
            ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' \u00b7 '))}</div>` : `<div class="profile-card-meta">${esc(t('profile_no_configuration'))}</div>`}
          </div>
        </div>`;
      card.onclick = () => openProfileDetail(p.name, card);
      if (state._currentProfileDetail && state._currentProfileDetail.name === p.name) card.classList.add('active');
      panel.appendChild(card);
    }
    // Re-render detail with fresh data if we have one and we're not in a form
    if (state._currentProfileDetail && state._profileMode !== 'create') {
      const refreshed = data.profiles.find(p => p.name === state._currentProfileDetail.name);
      if (refreshed) _renderProfileDetail(refreshed, data.active);
      else _clearProfileDetail();
    }
  } catch (e) {
    panel.innerHTML = `<div style="color:var(--accent);font-size:12px;padding:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`;
  }
}

export function _renderProfileConceptHelp(activeName){
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = t('profile_concept_title');
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title">${esc(t('profile_concept_title'))}</div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('tab_profiles'))}</div><div class="detail-row-value">${esc(t('profile_concept_desc_profiles'))}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('tab_workspaces'))}</div><div class="detail-row-value">${esc(t('profile_concept_desc_workspaces'))}</div></div>
        <div class="detail-row"><div class="detail-row-label">${esc(t('profile_concept_label_together'))}</div><div class="detail-row-value">${esc(t('profile_concept_desc_together'))}</div></div>
        <div class="detail-row" style="border-top:1px solid var(--border);padding-top:8px;margin-top:4px"><div class="detail-row-label">${esc(t('profile_concept_label_example'))}</div><div class="detail-row-value">${esc(t('profile_concept_example'))}</div></div>
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  state._profileMode = 'read';
  state._currentProfileDetail = null;
  _setProfileHeaderButtons('help');
}

export function _renderProfileDetail(p, activeName){
  state._currentProfileDetail = p;
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = p.name;
  const isActive = p.name === activeName;
  const isDefault = !!p.is_default;
  const statusBadge = isActive
    ? `<span class="detail-badge active">${esc(t('profile_active'))}</span>`
    : `<span class="detail-badge">Inactive</span>`;
  const defaultBadge = isDefault ? ` <span class="detail-badge">${esc(t('profile_default_label'))}</span>` : '';
  const gwBadge = p.gateway_running
    ? `<span class="detail-badge ok">${esc(t('profile_gateway_running'))}</span>`
    : `<span class="detail-badge">${esc(t('profile_gateway_stopped'))}</span>`;
  const rows = [];
  rows.push(`<div class="detail-row"><div class="detail-row-label">Status</div><div class="detail-row-value">${statusBadge}${defaultBadge}</div></div>`);
  rows.push(`<div class="detail-row"><div class="detail-row-label">Gateway</div><div class="detail-row-value">${gwBadge}</div></div>`);
  if (p.model) rows.push(`<div class="detail-row"><div class="detail-row-label">Model</div><div class="detail-row-value"><code>${esc(p.model)}</code></div></div>`);
  if (p.provider) rows.push(`<div class="detail-row"><div class="detail-row-label">Provider</div><div class="detail-row-value">${esc(p.provider)}</div></div>`);
  if (p.base_url) rows.push(`<div class="detail-row"><div class="detail-row-label">Base URL</div><div class="detail-row-value"><code>${esc(p.base_url)}</code></div></div>`);
  rows.push(`<div class="detail-row"><div class="detail-row-label">API key</div><div class="detail-row-value">${p.has_env ? esc(t('profile_api_keys_configured')) : '<span style="color:var(--muted)">Not configured</span>'}</div></div>`);
  if (p.total_skills && p.total_skills > 0) rows.push(`<div class="detail-row"><div class="detail-row-label">Skills</div><div class="detail-row-value">${esc(t('profile_skill_count', p.total_skills).replace(String(p.total_skills), `${p.enabled_skills} / ${p.total_skills}`))}</div></div>`);
  if (p.default_workspace) rows.push(`<div class="detail-row"><div class="detail-row-label">Default space</div><div class="detail-row-value"><code>${esc(p.default_workspace)}</code></div></div>`);
  body.innerHTML = `
    <div class="main-view-content">
      <div class="detail-card">
        <div class="detail-card-title">Profile</div>
        ${rows.join('')}
      </div>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  state._profileMode = 'read';
  _setProfileHeaderButtons('read', p, activeName);
}

export function _setProfileHeaderButtons(mode, p, activeName){
  const header = $('mainProfiles') && $('mainProfiles').querySelector('.main-view-header');
  const actBtn = $('btnActivateProfileDetail');
  const delBtn = $('btnDeleteProfileDetail');
  const cancelBtn = $('btnCancelProfileDetail');
  const saveBtn = $('btnSaveProfileDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') {
    if (header) header.style.display = 'flex';
    const isActive = p && p.name === activeName;
    const isDefault = !!(p && p.is_default);
    const singleProfileMode = !!(state._profilesCache && state._profilesCache.single_profile_mode);
    if (isActive || singleProfileMode) hide(actBtn); else show(actBtn);
    if (isDefault || singleProfileMode) hide(delBtn); else show(delBtn);
    hide(cancelBtn); hide(saveBtn);
  } else if (mode === 'create') {
    if (header) header.style.display = 'flex';
    hide(actBtn); hide(delBtn); show(cancelBtn); show(saveBtn);
  } else if (mode === 'help') {
    // Read-only help/concept view: title is populated, so show the header but
    // hide every action button (no profile to act on).
    if (header) header.style.display = 'flex';
    [actBtn, delBtn, cancelBtn, saveBtn].forEach(hide);
  } else {
    if (header) header.style.display = 'none';
    [actBtn, delBtn, cancelBtn, saveBtn].forEach(hide);
  }
}

export function openProfileDetail(name, el){
  if (!state._profilesCache || !state._profilesCache.profiles) return;
  const p = state._profilesCache.profiles.find(x => x.name === name);
  if (!p) return;
  document.querySelectorAll('.profile-card').forEach(e => e.classList.remove('active'));
  const target = el || document.querySelector(`.profile-card[data-name="${CSS.escape(name)}"]`);
  if (target) target.classList.add('active');
  state._profilePreFormDetail = null;
  _renderProfileDetail(p, state._profilesCache.active);
  _closeMobileSidebarAfterPanelSelection();
}

export function _clearProfileDetail(){
  state._currentProfileDetail = null;
  state._profileMode = 'empty';
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (title) title.textContent = '';
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  _setProfileHeaderButtons('empty');
}

export async function activateCurrentProfile(){
  if (!state._currentProfileDetail) return;
  await switchToProfile(state._currentProfileDetail.name);
}

export async function deleteCurrentProfile(){
  if (!state._currentProfileDetail) return;
  const name = state._currentProfileDetail.name;
  const _ok = await showConfirmDialog({title:t('profile_delete_confirm_title',name),message:t('profile_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_ok) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    _invalidateKanbanProfileCache();
    _clearProfileDetail();
    await loadProfilesPanel();
    showToast(t('profile_deleted', name));
  } catch (e) { showToast(t('delete_failed') + e.message); }
}
export function renderProfileDropdown(data) {
  data = data || {};
  const dd = $('profileDropdown');
  if (!dd) return;
  dd.innerHTML = '';
  const allProfiles = (Array.isArray(data.profiles) ? data.profiles : []).filter(p => p && typeof p.name === 'string');
  const active = (S.activeProfile && allProfiles.some(p => p.name === S.activeProfile))
    ? S.activeProfile
    : (data.active || 'default');
  const profiles = allProfiles.filter(p => p && (p.visible !== false || p.name === active));
  for (const p of profiles) {
    const opt = document.createElement('div');
    opt.className = 'profile-opt' + (p.name === active ? ' active' : '');
    const meta = [];
    if (typeof p.model === 'string' && p.model) meta.push(p.model.split('/').pop());
    if (p.total_skills && p.total_skills > 0) meta.push(t('profile_skill_count', p.total_skills).replace(String(p.total_skills), `${p.enabled_skills} / ${p.total_skills}`));
    const gwDot = `<span class="profile-opt-badge ${p.gateway_running ? 'running' : 'stopped'}"></span>`;
    const checkmark = p.name === active ? ' <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--link)" stroke-width="3" style="vertical-align:-1px"><polyline points="20 6 9 17 4 12"/></svg>' : '';
    const defaultBadge = p.is_default ? ` <span style="opacity:.5;font-weight:400">${esc(t('profile_default_label'))}</span>` : '';
    opt.innerHTML = `<div class="profile-opt-name">${gwDot}${esc(p.name)}${defaultBadge}${checkmark}</div>` +
      (meta.length ? `<div class="profile-opt-meta">${esc(meta.join(' \u00b7 '))}</div>` : '');
    opt.onclick = async () => {
      closeProfileDropdown();
      if (p.name === active) return;
      await switchToProfile(p.name);
    };
    dd.appendChild(opt);
  }
  // Divider + Manage link (hidden in single profile mode)
  if (!data.single_profile_mode) {
    const div = document.createElement('div'); div.className = 'ws-divider'; dd.appendChild(div);
    const mgmt = document.createElement('div'); mgmt.className = 'profile-opt ws-manage';
    mgmt.innerHTML = `${li('settings',12)} ${esc(t('manage_profiles'))}`;
    mgmt.onclick = () => { closeProfileDropdown(); mobileSwitchPanel('profiles'); };
    dd.appendChild(mgmt);
  }
  // Sync titlebar label to the resolved active profile
  const tbl = $('titlebarProfileLabel');
  if (tbl) tbl.textContent = active;
}

export function toggleProfileDropdown(e) {
  const dd = $('profileDropdown');
  if (!dd) return;
  if (dd.classList.contains('open')) { closeProfileDropdown(); return; }
  closeWsDropdown(); // close workspace dropdown if open
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  // Track which element triggered the dropdown for positioning
  state._profileDropdownTrigger = (e && e.currentTarget) || $('profileChip');
  const openGen = ++state._profileDropdownOpenGeneration;
  const cached = _profileDropdownBestCachedData();

  if(cached && !cached.single_profile_mode){
    renderProfileDropdown(cached);
    _openProfileDropdownShell();
  }else{
    _renderProfileDropdownLoading();
    _openProfileDropdownShell();
  }

  _profileDropdownFetchFresh().then(data => {
    if(openGen !== state._profileDropdownOpenGeneration) return;
    // In single profile mode, don't show profile dropdown at all
    if (data.single_profile_mode) {
      closeProfileDropdown();
      return;
    }
    renderProfileDropdown(data);
    _openProfileDropdownShell();
  }).catch(e => {
    if(openGen !== state._profileDropdownOpenGeneration) return;
    if(cached && !cached.single_profile_mode){
      // Keep the cached menu open; the next click/background refresh will retry.
      return;
    }
    closeProfileDropdown();
    showToast(t('profiles_load_failed'));
  });
}

export function closeProfileDropdown() {
  state._profileDropdownOpenGeneration++;
  const dd = $('profileDropdown');
  if (dd) dd.classList.remove('open');
  const chip=$('profileChip');
  if(chip) chip.classList.remove('active');
  const tbtn=$('titlebarProfileBtn');
  if(tbtn) tbtn.classList.remove('active');
}
document.addEventListener('click', e => {
  if (!e.target.closest('#profileChipWrap') && !e.target.closest('#titlebarProfileBtn') && !e.target.closest('#profileDropdown')) closeProfileDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('profileDropdown');
  if(dd&&dd.classList.contains('open')) _positionProfileDropdown();
});

export function _openProfileSwitchSessionBrowser(){
  try{
    const isDesktop = (typeof _isDesktopWidth === 'function') ? _isDesktopWidth() : true;
    if(isDesktop){
      if(typeof expandSidebar === 'function') expandSidebar();
      return;
    }
    const sidebar=document.querySelector('.sidebar');
    if(!sidebar)return;
    try{if(typeof _syncMobileSidebarPanelFromMainView==='function')_syncMobileSidebarPanelFromMainView();}catch(_){}
    sidebar.classList.remove('mobile-session-page');
    sidebar.classList.add('mobile-panel-drawer','mobile-open');
  }catch(_){}
}

export async function switchToProfile(name) {
  // ── #4671 profile-switch loading-skeleton — FOUR-GUARD CONTRACT ───────────────
  // The skeleton must never be clobbered by the OLD profile's content and must never
  // strand. Four interacting pieces of state cooperate; an edit touching one without
  // the others can silently reopen a clobber/strand window, so keep them in sync:
  //   1. _profileSwitchListEmbargo (sessions.js) — set BEFORE the skeleton, drops EVERY
  //      session-list payload (success + fetch-failure) during the switch window; lifted
  //      immediately before the switch-owned renderSessionList(), on failure-restore, and
  //      in the _switchGen-guarded finally. Closes the "render that STARTS mid-switch,
  //      before the new-profile cookie is set, fetched the old profile" window.
  //   2. _invalidateSessionListRenders() (sessions.js) — bumps _renderSessionListGen +
  //      clears pending/queued at switch start; discards renders already in flight/queued.
  //   3. _sessionListSkeletonActive (sessions.js) — renderSessionListFromCache() bails
  //      while true; cleared ONLY on fresh data (_applySessionListPayload), fetch-error,
  //      and failure-restore — so a bail can't strand the skeleton.
  //   4. _wsTreeGen (workspace.js) — bumped UNCONDITIONALLY here (incl. panel-closed, since
  //      loadDir('.') still runs); loadDir rejects stale /api/list whose gen is superseded.
  //   Plus state._profileSwitchGeneration / _switchGen — guards superseded switches so a slower
  //   earlier switch can't clobber a newer one's skeleton/embargo.
  // ──────────────────────────────────────────────────────────────────────────────
  // No-op self-switch guard: bail before showing any loading skeleton if we're
  // already on this profile, so paths like activateCurrentProfile() (which
  // doesn't pre-check) can't flash a skeleton→restore for a click that changes
  // nothing. (#4662 Opus gate)
  if (name && name === S.activeProfile) return true;
  S._pendingSessionToolsets=null;
  // Profile switches are per-client cookie/TLS scoped, so a running stream in
  // the current session can safely continue while this tab moves to another
  // profile. The in-flight session stays attached to its original profile.

  // ── Loading indicator ───────────────────────────────────────────────────
  // Show spinner on the profile chip immediately so the user gets visual
  // feedback while the async switch is in progress.
  const _chip = $('profileChip');
  const _chipLabel = $('profileChipLabel');
  const _titlebarBtn = $('titlebarProfileBtn');
  const _titlebarLabel = $('titlebarProfileLabel');
  const _prevProfileName = S.activeProfile || 'default';
  const _switchGen = ++state._profileSwitchGeneration;
  const _openingExistingSidebarSession = !!(typeof _profileSwitchOpeningExistingSession !== 'undefined' && _profileSwitchOpeningExistingSession);
  // In all-profiles mode the sidebar cache already contains the clicked target
  // profile. Keep those valid rows visible while only the per-client profile
  // cookie changes; loadSession() will patch the active row from cache after the
  // target conversation arrives.
  const _preserveAllProfilesSidebar = _openingExistingSidebarSession
    && typeof _showAllProfiles !== 'undefined'
    && _showAllProfiles;
  if (_chip) { _chip.classList.add('switching'); _chip.disabled = true; }
  if (_titlebarBtn) { _titlebarBtn.classList.add('switching'); _titlebarBtn.disabled = true; }
  // Optimistic name update — shows the target name right away
  if (_chipLabel) _chipLabel.textContent = name;
  if (_titlebarLabel) _titlebarLabel.textContent = name;

  // ── Clear stale content + show loading skeletons immediately (#4662) ───────
  // The conversation list and workspace tree still show the PREVIOUS profile's
  // content until their fetches resolve (~1s). Replace them with skeletons the
  // instant the switch begins so the user never stares at the wrong profile's
  // data, and gets consistent loading feedback across the whole surface — not
  // just the spinning chip. The real renders below overwrite these.
  //
  // First dismiss any open inline-rename or row action menu: renderSessionList
  // FromCache() early-returns (no DOM swap) while _renamingSid or
  // _sessionActionMenu is set, which would otherwise strand the skeleton AND
  // defeat the failure-path restore (#4662 Opus gate). A profile switch is a
  // context change where dismissing those transient affordances is correct.
  if (typeof _renamingSid !== 'undefined' && _renamingSid) _renamingSid = null;
  if (typeof closeSessionActionMenu === 'function') closeSessionActionMenu();
  // Determine whether the current session must be replaced instead of being
  // retagged in place. A session with messages/active runtime belongs to the
  // current profile. After the profile-switch POST returns, we also treat an
  // otherwise-empty session whose recorded profile does not match the target
  // profile as replace-only: uploads send S.session.session_id and the backend
  // correctly rejects old-profile sessions under the new profile cookie.
  let sessionInProgress = !!(S.session && (
    (S.messages && S.messages.length > 0) ||
    S.session.active_stream_id ||
    S.session.pending_user_message
  ));
  if (_openingExistingSidebarSession && S.session) {
    // A cross-profile sidebar click is about to load a concrete existing session.
    // Do not create or retag a blank intermediary session in the destination profile.
    sessionInProgress = true;
  }
  const _workspaceVisibleAtStart = typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed';

  // #4671 CORE: the skeleton/embargo/generation setup is INSIDE the try so the
  // _switchGen-guarded finally always lifts the embargo — a throw in this synchronous
  // setup can't leak the embargo and freeze the sidebar (Codex re-gate 4).
  try {
    // Invalidate any in-flight/queued session-list render BEFORE showing the skeleton,
    // so a pre-switch /api/sessions response (old profile's rows, issued before the
    // switch) can't resolve, pass the generation guard, clear the skeleton flag, and
    // paint stale rows. Must precede showSessionListSkeleton().
    if (!_preserveAllProfilesSidebar) {
      if (typeof _invalidateSessionListRenders === 'function') _invalidateSessionListRenders();
      // ...and set the embargo so a render that STARTS during the switch window (after the
      // skeleton, before the new-profile cookie is set) also can't paint the old profile's
      // rows. Cleared right before the switch-owned renderSessionList() and on failure.
      if (typeof _setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(true);
      if (typeof showSessionListSkeleton === 'function') showSessionListSkeleton(name);
    }
    // invalidate any in-flight workspace-tree load UNCONDITIONALLY at switch start — even
    // when the panel is closed, loadDir('.') still runs later, and an empty-session switch
    // reuses the same session_id so loadDir's id guard alone can't reject a stale
    // previous-workspace /api/list. Bump here (not only inside the panel-gated
    // showWorkspaceTreeSkeleton) to close the closed-panel race.
    if (typeof bumpWorkspaceTreeGen === 'function') bumpWorkspaceTreeGen();
    if (_workspaceVisibleAtStart && typeof showWorkspaceTreeSkeleton === 'function') showWorkspaceTreeSkeleton();
    // timeoutToast:false — suppress api()'s generic "Request timed out" toast so a
    // superseded or transient-but-eventually-successful switch can't pop a spurious
    // red error while the real switch completes and renders. The catch block below is
    // the single source of truth for switch failure and is gated on _switchGen, so the
    // error surfaces ONLY when the CURRENT switch genuinely fails (@rodboev review, #4662).
    const data = await api('/api/profile/switch', { method: 'POST', body: JSON.stringify({ name }), timeoutToast: false });
    if (_switchGen !== state._profileSwitchGeneration) return false;
    S.activeProfile = data.active || name;
    S.activeProfileIsDefault = !!data.is_default;
    if (typeof _resetCronUnreadForProfileSwitch === 'function') {
      _resetCronUnreadForProfileSwitch();
    }
    const targetActiveProfile = S.activeProfile || 'default';
    let sessionProfileMatchesTarget = true;
    if (!sessionInProgress && S.session) {
      const currentSessionProfile = (typeof S.session.profile === 'string' && S.session.profile.trim())
        ? S.session.profile.trim()
        : 'default';
      sessionProfileMatchesTarget = (typeof _profileMatchesActiveProfile === 'function')
        ? _profileMatchesActiveProfile(currentSessionProfile, targetActiveProfile)
        : (currentSessionProfile === targetActiveProfile || (currentSessionProfile === 'default' && !!S.activeProfileIsDefault));
      if (!sessionProfileMatchesTarget) {
        sessionInProgress = true;
      }
    }
    // Reconnect the gateway SSE to the NEW profile's watcher. The backend watcher
    // registry is now profile-keyed (#3629), but this tab's existing EventSource is
    // still subscribed to the PREVIOUS profile's watcher — and the probe-based
    // reattach is gated on `!_gatewaySSE`, which can't fire while the old stream is
    // open. startGatewaySSE() closes the old ES (stopGatewaySSE) and reconnects with
    // the new profile cookie; it self-gates on window._showCliSessions internally.
    if (typeof startGatewaySSE === 'function') startGatewaySSE();

    // Update composer placeholder and title bar while the core profile-switch
    // state is still close to the profile API response.
    if (typeof applyBotName === 'function') applyBotName();

    // ── Model + Workspace ──────────────────────────────────────────────────
    // Apply the profile defaults returned by /api/profile/switch immediately.
    // Refreshing the full model/workspace catalogs is useful, but it should not
    // hold the visible switch animation open.
    if(typeof _clearPersistedModelState==='function') _clearPersistedModelState();
    else localStorage.removeItem('hermes-webui-model');
    state._skillsData = null;
    state._workspaceList = null;
    if (data.default_model) window._defaultModel = data.default_model;
    if (data.default_model_provider) window._activeProvider = data.default_model_provider;

    // ── Apply model ────────────────────────────────────────────────────────
    if (data.default_model) {
      const sel = $('modelSelect');
      const providerId = data.default_model_provider || window._activeProvider || null;
      const existingDefaultOpt = sel ? Array.from(sel.options).find(o => o.value === data.default_model) : null;
      if (existingDefaultOpt && providerId && !existingDefaultOpt.dataset.provider) {
        existingDefaultOpt.dataset.provider = providerId;
      }
      if (sel && !existingDefaultOpt) {
        const opt = document.createElement('option');
        opt.value = data.default_model;
        opt.textContent = typeof getModelLabel === 'function' ? getModelLabel(data.default_model) : data.default_model;
        opt.dataset.custom = '1';
        if (providerId) opt.dataset.provider = providerId;
        sel.querySelectorAll('option[data-custom]').forEach(o => o.remove());
        sel.appendChild(opt);
      }
      const resolved = _applyModelToDropdown(data.default_model, sel, providerId);
      const modelToUse = resolved || data.default_model;
      const modelState = (typeof _modelStateForSelect==='function')
        ? _modelStateForSelect(sel, modelToUse)
        : {model:modelToUse,model_provider:providerId};
      S._pendingProfileModel = modelToUse;
      S._pendingProfileModelProvider = modelState.model_provider||providerId||null;
      // Only patch the in-memory session model if we're NOT about to replace the session
      if (S.session && !sessionInProgress) {
        S.session.model = modelToUse;
        S.session.model_provider = modelState.model_provider||providerId||null;
        S.session.profile = data.active || name;
      }
    }
    // #3331 follow-up (Codex gate): retag the in-memory session's profile on
    // ANY profile switch, even when the switched-to profile returns no
    // default_model (empty session / model-less profile). Without this the
    // profile chip + project-picker filter keep the stale profile after a
    // switch to a model-less profile. Guarded by !sessionInProgress like the
    // model patch above (don't touch a session about to be replaced).
    if (S.session && !sessionInProgress) {
      S.session.profile = data.active || name;
    }
    if (typeof refreshProfileTransitionReasoningChip === 'function') {
      refreshProfileTransitionReasoningChip(data.default_model, data.default_model_provider);
    }

    // ── Apply workspace ────────────────────────────────────────────────────
    if (data.default_workspace) {
      // Always store the persistent profile default — used for blank-page display
      // and workspace auto-bind throughout the session lifecycle (#804, #823).
      S._profileDefaultWorkspace = data.default_workspace;
      // Also set the one-shot flag consumed by newSession() so the first new
      // session after a profile switch inherits this workspace (#424).
      S._profileSwitchWorkspace = data.default_workspace;

      if (S.session && !sessionInProgress) {
        // Empty session (no messages yet) — safe to update it in place
        try {
          await api('/api/session/update', { method: 'POST', body: JSON.stringify({
            session_id: S.session.session_id,
            workspace: data.default_workspace,
            model: S.session.model,
            model_provider: S.session.model_provider||null,
          })});
          S.session.workspace = data.default_workspace;
        } catch (_) {}
      }
    }

    // ── Session ────────────────────────────────────────────────────────────
    // Keep the all-profiles sidebar scope sticky across profile switches. It is
    // a navigation preference shared by the browser session, not a per-profile flag.
    if (!_preserveAllProfilesSidebar && typeof animateNextSessionListRefresh === 'function') {
      animateNextSessionListRefresh();
    }

    if (sessionInProgress && _openingExistingSidebarSession) {
      // The caller will immediately load the clicked session after this profile
      // cookie switch. Avoid creating/retagging an intermediate blank chat. In
      // all-profiles mode the cached list remains authoritative across this
      // cookie-only switch, so do not block the transcript behind another
      // projects + sessions round trip.
      const workspaceVisible = typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed';
      if (!_preserveAllProfilesSidebar) {
        if (typeof _setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(false);
        await renderSessionList();
        if (_switchGen !== state._profileSwitchGeneration) return false;
      }
      if (workspaceVisible && typeof clearWorkspaceTreeSkeleton === 'function') clearWorkspaceTreeSkeleton();
      showToast(t('profile_switched', name));
    } else if (sessionInProgress) {
      // The current session has messages and belongs to the previous profile.
      // Start a new session for the new profile so nothing gets cross-tagged.
      const workspaceVisible = typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed';
      await newSession(false, {awaitWorkspaceLoad: workspaceVisible, worktree: false});
      if (_switchGen !== state._profileSwitchGeneration) return false;
      // Keep topbar chips (workspace/profile) in sync after creating the
      // new profile-scoped session.
      syncTopbar();
      // #4671: lift the embargo immediately before the switch-owned render — JS is
      // single-threaded so nothing interleaves between this clear and the call, making
      // this render the first allowed to paint the new profile's rows.
      if (typeof _setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(false);
      await renderSessionList();
      // Re-check generation after the awaited list render: a newer switch can be
      // started while renderSessionList() is in flight, and without this guard
      // the superseded switch would clear the newer switch's workspace skeleton
      // and pop a stale toast. Mirrors the no-messages branch guard below.
      // (@rodboev/greptile review, #4662)
      if (_switchGen !== state._profileSwitchGeneration) return false;
      if (typeof _openProfileSwitchSessionBrowser === 'function') _openProfileSwitchSessionBrowser();
      // Safety net: if the new session has no workspace, newSession() won't have
      // painted the file tree — clear the up-front skeleton so it can't strand
      // (#4662 Opus gate). No-op when a real tree already rendered.
      if ((!S.session || !S.session.workspace) && typeof clearWorkspaceTreeSkeleton === 'function') {
        clearWorkspaceTreeSkeleton();
      }
      showToast(t('profile_switched_new_conversation', name));
    } else {
      // No messages yet — refresh the list and topbar in place, then the
      // workspace tree. The loading skeletons shown up front (top of this
      // function) already give immediate cross-surface feedback, so we keep the
      // workspace refresh AFTER the stale-switch guard: loadDir() paints the
      // file tree as soon as its fetch resolves with only a session-id check,
      // and empty-session switches reuse the same session id — so starting it
      // before the guard could let an older switch's /api/list paint over a
      // newer one (Codex gate #4662). renderSessionList() is the slow fetch and
      // has its own internal generation guard, so awaiting it first is fine.
      const workspaceVisible = typeof _workspacePanelMode !== 'undefined' && _workspacePanelMode !== 'closed';
      // #4671: lift the embargo immediately before the switch-owned render (see above).
      if (typeof _setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(false);
      await renderSessionList();
      if (_switchGen !== state._profileSwitchGeneration) return;
      if (typeof _openProfileSwitchSessionBrowser === 'function') _openProfileSwitchSessionBrowser();
      syncTopbar();
      // Refresh workspace file tree so the right panel shows the new
      // profile's workspace, not the previous one (#1214).
      if (S.session && S.session.workspace) {
        const dirLoad = loadDir('.');
        if (workspaceVisible) await dirLoad;
      } else if (typeof clearWorkspaceTreeSkeleton === 'function') {
        // New profile has no bound workspace — clear the up-front skeleton so it
        // doesn't strand (#4662 Opus gate).
        clearWorkspaceTreeSkeleton();
      }
      showToast(t('profile_switched', name));
    }

    await _profileSwitchPanelLoad();
    _refreshProfileSwitchBackground(_switchGen);
    return true;

  } catch (e) {
    // Revert the optimistic name update on error
    if (_switchGen === state._profileSwitchGeneration && _chipLabel) _chipLabel.textContent = _prevProfileName;
    if (_switchGen === state._profileSwitchGeneration && _titlebarLabel) _titlebarLabel.textContent = _prevProfileName;
    if (_switchGen === state._profileSwitchGeneration) showToast(t('switch_failed') + e.message);
    // The switch failed, so we're still on the previous profile and its caches
    // are intact — restore the real list/tree so the loading skeletons we showed
    // up front don't strand. (#4662)
    if (_switchGen === state._profileSwitchGeneration) {
      // The switch failed; _allSessions still holds the (still-current) previous
      // profile, so clear the skeleton flag and re-render to restore the real list
      // rather than strand the up-front skeleton (#4671). Lift the embargo too so the
      // restore render (and subsequent normal renders) can paint.
      if (typeof _setProfileSwitchListEmbargo === 'function') _setProfileSwitchListEmbargo(false);
      _sessionListSkeletonActive = false;
      if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
      if (_workspaceVisibleAtStart && S.session && S.session.workspace && typeof loadDir === 'function') {
        loadDir('.');
      } else if (_workspaceVisibleAtStart && typeof clearWorkspaceTreeSkeleton === 'function') {
        // No workspace to restore on the (still-current) previous profile —
        // clear the up-front workspace skeleton so it doesn't strand on a switch
        // failure, mirroring the success-path no-workspace handling (#4662).
        clearWorkspaceTreeSkeleton();
      }
    }
    return false;
  } finally {
    // Always remove loading indicator regardless of success or failure
    if (_switchGen === state._profileSwitchGeneration && _chip) { _chip.classList.remove('switching'); _chip.disabled = false; }
    if (_switchGen === state._profileSwitchGeneration && _titlebarBtn) { _titlebarBtn.classList.remove('switching'); _titlebarBtn.disabled = false; }
    // #4671 safety net: guarantee the session-list embargo is lifted on EVERY exit of the
    // current switch (success paths clear it before their authoritative render; this covers
    // early-returns/throws between skeleton-show and those clears so it can't freeze the
    // sidebar). Guarded by _switchGen so a superseded switch can't lift a newer switch's embargo.
    if (_switchGen === state._profileSwitchGeneration && typeof _setProfileSwitchListEmbargo === 'function') {
      _setProfileSwitchListEmbargo(false);
    }
  }
}
export function openProfileCreate(){
  if (typeof switchPanel === 'function' && state._currentPanel !== 'profiles') switchPanel('profiles');
  state._profilePreFormDetail = state._currentProfileDetail ? { ..._currentProfileDetail } : null;
  state._profileMode = 'create';
  _renderProfileForm();
}

export function _renderProfileForm(){
  const title = $('profileDetailTitle');
  const body = $('profileDetailBody');
  const empty = $('profileDetailEmpty');
  if (!title || !body) return;
  title.textContent = t('new_profile');
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveProfileForm();">
        <div class="detail-form-row">
          <label for="profileFormName">${esc(t('profile_name_label') || 'Name')}</label>
          <input type="text" id="profileFormName" placeholder="${esc(t('profile_name_placeholder') || 'lowercase, a-z 0-9 hyphens')}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" required>
          <div class="detail-form-hint">${esc(t('profile_name_rule') || 'Lowercase letters, numbers, hyphens, underscores only.')}</div>
        </div>
        <div class="detail-form-row">
          <label class="detail-form-check" for="profileFormClone">
            <input type="checkbox" id="profileFormClone"> <span>${esc(t('profile_clone_label') || 'Clone config from active profile')}</span>
          </label>
        </div>
        <div class="detail-form-row">
          <label for="profileFormModel">${esc(t('profile_model_label') || 'Model / provider')}</label>
          <select id="profileFormModel"></select>
          <div class="detail-form-hint">${esc(t('profile_model_hint') || 'Choose from configured providers and models for this new profile.')}</div>
        </div>
        <div class="detail-form-row">
          <label for="profileFormBaseUrl">${esc(t('profile_base_url_label') || 'Base URL')}</label>
          <input type="text" id="profileFormBaseUrl" placeholder="${esc(t('profile_base_url_placeholder') || 'Optional, e.g. http://localhost:11434')}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
        </div>
        <div class="detail-form-row">
          <label for="profileFormApiKey">${esc(t('profile_api_key_label') || 'API key')}</label>
          <input type="password" id="profileFormApiKey" placeholder="${esc(t('profile_api_key_placeholder') || 'Optional')}" autocomplete="off">
        </div>
        <div id="profileFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setProfileHeaderButtons('create');
  const n = $('profileFormName');
  if (n) n.focus();
  _populateProfileFormModelSelect();
}

export async function _populateProfileFormModelSelect(){
  const sel = $('profileFormModel');
  if (!sel) return;
  sel.innerHTML = `<option value="">${esc(t('profile_model_use_default') || 'Use active profile default')}</option>`;
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
        og.appendChild(opt);
      }
      if (og.children.length) sel.appendChild(og);
    }
    if (data && data.default_model && typeof _applyModelToDropdown === 'function') {
      _applyModelToDropdown(data.default_model, sel, data.active_provider || window._activeProvider || null);
    }
  } catch (e) {
    console.warn('Failed to load profile model picker:', e.message);
  }
}

export function cancelProfileForm(){
  if (state._profilePreFormDetail) {
    const snap = state._profilePreFormDetail;
    state._profilePreFormDetail = null;
    const activeName = state._profilesCache ? state._profilesCache.active : null;
    _renderProfileDetail(snap, activeName);
    return;
  }
  _clearProfileDetail();
}

export async function saveProfileForm(){
  const nameEl = $('profileFormName');
  const cloneEl = $('profileFormClone');
  const modelEl = $('profileFormModel');
  const baseEl = $('profileFormBaseUrl');
  const apiKeyEl = $('profileFormApiKey');
  const errEl = $('profileFormError');
  if (!nameEl || !errEl) return;
  const name = (nameEl.value || '').trim().toLowerCase();
  const cloneConfig = !!(cloneEl && cloneEl.checked);
  errEl.style.display = 'none';
  if (!name) { errEl.textContent = t('name_required'); errEl.style.display = ''; return; }
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) { errEl.textContent = t('profile_name_rule'); errEl.style.display = ''; return; }
  const baseUrl = (baseEl ? (baseEl.value || '') : '').trim();
  const apiKey = (apiKeyEl ? (apiKeyEl.value || '') : '').trim();
  if (baseUrl && !/^https?:\/\//.test(baseUrl)) { errEl.textContent = t('profile_base_url_rule'); errEl.style.display = ''; return; }
  try {
    const payload = { name, clone_config: cloneConfig };
    const selectedModel = modelEl ? (modelEl.value || '').trim() : '';
    if (selectedModel) {
      const modelState = (typeof _modelStateForSelect === 'function')
        ? _modelStateForSelect(modelEl, selectedModel)
        : { model: selectedModel, model_provider: null };
      if (modelState.model) payload.default_model = modelState.model;
      if (modelState.model_provider) payload.model_provider = modelState.model_provider;
    }
    if (baseUrl) payload.base_url = baseUrl;
    if (apiKey) payload.api_key = apiKey;
    await api('/api/profile/create', { method: 'POST', body: JSON.stringify(payload) });
    _invalidateKanbanProfileCache();
    state._profilePreFormDetail = null;
    await loadProfilesPanel();
    showToast(t('profile_created', name));
    openProfileDetail(name);
  } catch (e) {
    errEl.textContent = e.message || t('create_failed');
    errEl.style.display = '';
  }
}

// Back-compat
const submitProfileCreate = saveProfileForm;
export function toggleProfileForm(){ openProfileCreate();
}

export async function deleteProfile(name) {
  const _delProf=await showConfirmDialog({title:t('profile_delete_confirm_title',name),message:t('profile_delete_confirm_message'),confirmLabel:t('delete_title'),danger:true,focusCancel:true});
  if(!_delProf) return;
  try {
    await api('/api/profile/delete', { method: 'POST', body: JSON.stringify({ name }) });
    _invalidateKanbanProfileCache();
    await loadProfilesPanel();
    showToast(t('profile_deleted', name));
  } catch (e) { showToast(t('delete_failed') + e.message); }
}
