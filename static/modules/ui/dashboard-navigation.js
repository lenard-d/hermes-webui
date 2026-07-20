import { showToast } from './toast-notifications.js';
import { $ } from './state.js';

const DASHBOARD_STATUS_TTL_MS=60000;
let _dashboardStatusCache=null;
let _dashboardStatusFetchedAt=0;
let _dashboardLastNonNeverMode='auto'; // Server-scoped dashboard config keeps this restore target session-global on purpose.
let _dashboardSettingsLoadSeq=0;
let _dashboardSettingsWriteSeq=0;

function _dashboardIsBrowserLoopback(){
  const host=(window.location.hostname||'').replace(/^\[|\]$/g,'').toLowerCase();
  return host==='127.0.0.1'||host==='localhost'||host==='::1';
}

function _normalizeDashboardEnabledMode(mode){
  return mode==='auto'||mode==='always'||mode==='never'?mode:'auto';
}

function _setDashboardModeForChip(mode){
  mode=_normalizeDashboardEnabledMode(mode);
  if(mode==='auto'||mode==='always') _dashboardLastNonNeverMode=mode;
}

function _getDashboardChipRestoreMode(){
  return _dashboardLastNonNeverMode||'auto';
}

function _dashboardBrowserUrl(status){
  if(!status||!status.running) return '';
  if(status.browser_url||status.url){
    try{return new URL(status.browser_url||status.url).toString().replace(/\/$/,'');}
    catch(_){}
  }
  if(!status.port) return '';
  let source;
  try{source=new URL('http://127.0.0.1:'+status.port);}
  catch(_){return '';}
  const browserHost=window.location.hostname||source.hostname;
  const displayHost=browserHost.includes(':')&&!browserHost.startsWith('[')?'['+browserHost+']':browserHost;
  return source.protocol+'//'+displayHost+':'+status.port;
}
function _stripInlineEventHandlers(node){
  if(!node)return;
  const strip=el=>{
    Array.from(el.attributes||[]).forEach(attr=>{
      if(attr.name&&attr.name.toLowerCase().startsWith('on'))el.removeAttribute(attr.name);
    });
    if('onclick' in el)el.onclick=null;
    Array.from(el.children||[]).forEach(strip);
  };
  strip(node);
}
function _syncNavActionMirrors(){
  const rail=document.querySelector('.rail');
  const sidebar=document.querySelector('.sidebar-nav');
  if(!rail||!sidebar)return;
  const sources=Array.from(rail.querySelectorAll('.nav-tab:not([data-panel]):not([data-dashboard-link])')).filter(source=>source.id);
  const mirrors=Array.from(sidebar.querySelectorAll('[data-nav-action-mirror]'));
  const sourceIds=new Set(sources.map(source=>source.id));
  mirrors.forEach(mirror=>{
    if(!sourceIds.has(mirror.getAttribute('data-nav-action-mirror')))mirror.remove();
  });
  sources.forEach(source=>{
    const sourceVisible=(()=>{
      if(source.hidden||source.getAttribute('aria-hidden')==='true')return false;
      if(source.classList.contains('nav-tab-hidden'))return false;
      if(source.style&&(source.style.display==='none'||source.style.visibility==='hidden'))return false;
      if(typeof window!=='undefined'&&typeof window.getComputedStyle==='function'){
        const computed=window.getComputedStyle(source);
        if(computed&&(computed.display==='none'||computed.visibility==='hidden'))return false;
      }
      return true;
    })();
    let mirror=mirrors.find(el=>el.getAttribute('data-nav-action-mirror')===source.id);
    if(!mirror){
      mirror=source.cloneNode(true);
      _stripInlineEventHandlers(mirror);
      mirror.id=source.id+'Mobile';
      mirror.classList.remove('rail-btn');
      mirror.classList.add('has-tooltip--bottom');
      mirror.setAttribute('data-nav-action-mirror',source.id);
      mirror.addEventListener('click',e=>{
        e.preventDefault();
        if(mirror._navActionSource)mirror._navActionSource.click();
        if(typeof closeMobileSidebar==='function')closeMobileSidebar();
      });
      const anchor=sidebar.querySelector('.dashboard-link,[data-dashboard-link]')||sidebar.querySelector('[data-panel="logs"]');
      sidebar.insertBefore(mirror,anchor||null);
    }else{
      mirror.innerHTML=source.innerHTML;
      _stripInlineEventHandlers(mirror);
    }
    mirror._navActionSource=source;
    mirror.classList.toggle('nav-action-visible',sourceVisible);
    const label=source.getAttribute('data-tooltip')||source.getAttribute('aria-label')||'';
    if(label)mirror.setAttribute('data-label',label);
  });
}
function _initNavActionMirrors(){
  _syncNavActionMirrors();
  const rail=document.querySelector('.rail');
  if(rail&&window.MutationObserver)new MutationObserver(_syncNavActionMirrors).observe(rail,{
    childList:true,
    subtree:true,
    attributes:true,
    attributeFilter:['class','style','hidden','aria-hidden','data-tooltip','aria-label'],
  });
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_initNavActionMirrors,{once:true});
else _initNavActionMirrors();
function _applyDashboardStatus(status){
  const running=!!(status&&status.running);
  const url=running?_dashboardBrowserUrl(status):'';
  const warning=running&&!_dashboardIsBrowserLoopback()?t('dashboard_loopback_warning'):'';
  document.querySelectorAll('[data-dashboard-link]').forEach(btn=>{
    btn.classList.toggle('dashboard-link-visible',running);
    btn.classList.toggle('nav-action-visible',running);
    btn.style.display=running?'':'none';
    btn.dataset.dashboardUrl=url;
    const tipText=warning||t('tab_dashboard');
    if(btn.hasAttribute('data-tooltip')){
      // Sync the custom CSS tooltip and explicitly clear the native title so
      // the slow ~1.5s native browser tooltip does not co-fire alongside the
      // fast custom tooltip (#1775).
      btn.setAttribute('data-tooltip',tipText);
      if(btn.hasAttribute('title')) btn.removeAttribute('title');
    } else {
      btn.title=tipText;
    }
    btn.setAttribute('aria-label',tipText);
  });
}
async function refreshDashboardStatus(force=false){
  const now=Date.now();
  // Skip the interval-driven poll while the tab is hidden: the 60s interval
  // equals the cache TTL, so every background tick was a real /api/dashboard/status
  // fetch that never hit the cache — a needless wakeup on a tab nobody is
  // looking at (battery/CPU, #2476). Forced calls (settings save, init, the
  // visibilitychange catch-up) still run. A visible tab keeps its live status.
  if(!force&&typeof document!=='undefined'&&document.hidden){
    return _dashboardStatusCache;
  }
  if(!force&&_dashboardStatusCache&&(now-_dashboardStatusFetchedAt)<DASHBOARD_STATUS_TTL_MS){
    _applyDashboardStatus(_dashboardStatusCache);
    return _dashboardStatusCache;
  }
  try{
    const status=await api('/api/dashboard/status',{timeoutToast:false});
    _dashboardStatusCache=status||{running:false};
  }catch(_){
    _dashboardStatusCache={running:false};
  }
  _dashboardStatusFetchedAt=Date.now();
  _applyDashboardStatus(_dashboardStatusCache);
  return _dashboardStatusCache;
}
async function loadDashboardSettings(){
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  if(!modeEl&&!urlEl) return;
  const loadSeq=++_dashboardSettingsLoadSeq;
  const writeSeq=_dashboardSettingsWriteSeq;
  try{
    const cfg=await api('/api/dashboard/config');
    if(loadSeq!==_dashboardSettingsLoadSeq||writeSeq!==_dashboardSettingsWriteSeq) return;
    const mode=_normalizeDashboardEnabledMode(cfg&&cfg.enabled);
    if(modeEl) modeEl.value=mode;
    _setDashboardModeForChip(mode);
    if(urlEl) urlEl.value=cfg.url||'';
    if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();
  }catch(_){/* leave defaults visible */}
}
async function saveDashboardSettings(opts){
  opts=opts||{};
  const modeEl=$('settingsDashboardMode');
  const urlEl=$('settingsDashboardUrl');
  const statusEl=$('settingsDashboardStatus');
  const payload={enabled:(modeEl&&modeEl.value)||'auto',url:(urlEl&&urlEl.value||'').trim()};
  _dashboardSettingsWriteSeq+=1;
  try{
    const saved=await api('/api/dashboard/config',{method:'POST',body:JSON.stringify(payload)});
    const mode=_normalizeDashboardEnabledMode(saved&&saved.enabled);
    if(modeEl) modeEl.value=mode;
    _setDashboardModeForChip(mode);
    if(urlEl) urlEl.value=saved.url||'';
    if(statusEl) statusEl.textContent='Dashboard link settings saved.';
    await refreshDashboardStatus(true);
    if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();
  }catch(err){
    if(statusEl) statusEl.textContent='Dashboard link settings failed to save.';
    else if(typeof showToast==='function') showToast('Dashboard link settings failed to save.');
    try{await loadDashboardSettings();}catch(_){}
    if(opts.raiseOnError) throw err;
  }
}
function openHermesDashboard(event){
  if(event){event.preventDefault();event.stopPropagation();}
  const btn=event&&event.currentTarget?event.currentTarget:document.querySelector('[data-dashboard-link]');
  const url=(btn&&btn.dataset&&btn.dataset.dashboardUrl)||_dashboardBrowserUrl(_dashboardStatusCache);
  if(!url) return false;
  window.open(url,'_blank','noopener,noreferrer');
  return false;
}
function _initDashboardLinkProbe(){
  loadDashboardSettings();
  refreshDashboardStatus(true);
  setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS);
  // Catch up once when the tab becomes visible again, since the interval poll
  // was skipped while hidden and its cache is now stale.
  if(typeof document!=='undefined'&&typeof document.addEventListener==='function'){
    document.addEventListener('visibilitychange',()=>{
      if(!document.hidden) refreshDashboardStatus(true);
    });
  }
}
if(document.readyState==='complete'){
  _initDashboardLinkProbe();
}else{
  document.addEventListener('DOMContentLoaded',_initDashboardLinkProbe,{once:true});
}

/* ── Image lightbox — click any .msg-media-img to enlarge ─────────────────── */

export {
  _dashboardIsBrowserLoopback,
  _normalizeDashboardEnabledMode,
  _setDashboardModeForChip,
  _getDashboardChipRestoreMode,
  _dashboardBrowserUrl,
  _stripInlineEventHandlers,
  _syncNavActionMirrors,
  _initNavActionMirrors,
  _applyDashboardStatus,
  openHermesDashboard,
  _initDashboardLinkProbe,
  refreshDashboardStatus,
  loadDashboardSettings,
  saveDashboardSettings,
  DASHBOARD_STATUS_TTL_MS,
  _dashboardStatusCache,
  _dashboardStatusFetchedAt,
  _dashboardLastNonNeverMode,
  _dashboardSettingsLoadSeq,
  _dashboardSettingsWriteSeq,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _dashboardIsBrowserLoopback: { enumerable:true, get:()=>_dashboardIsBrowserLoopback, set:value=>{ _dashboardIsBrowserLoopback=value; } },
  _normalizeDashboardEnabledMode: { enumerable:true, get:()=>_normalizeDashboardEnabledMode, set:value=>{ _normalizeDashboardEnabledMode=value; } },
  _setDashboardModeForChip: { enumerable:true, get:()=>_setDashboardModeForChip, set:value=>{ _setDashboardModeForChip=value; } },
  _getDashboardChipRestoreMode: { enumerable:true, get:()=>_getDashboardChipRestoreMode, set:value=>{ _getDashboardChipRestoreMode=value; } },
  _dashboardBrowserUrl: { enumerable:true, get:()=>_dashboardBrowserUrl, set:value=>{ _dashboardBrowserUrl=value; } },
  _stripInlineEventHandlers: { enumerable:true, get:()=>_stripInlineEventHandlers, set:value=>{ _stripInlineEventHandlers=value; } },
  _syncNavActionMirrors: { enumerable:true, get:()=>_syncNavActionMirrors, set:value=>{ _syncNavActionMirrors=value; } },
  _initNavActionMirrors: { enumerable:true, get:()=>_initNavActionMirrors, set:value=>{ _initNavActionMirrors=value; } },
  _applyDashboardStatus: { enumerable:true, get:()=>_applyDashboardStatus, set:value=>{ _applyDashboardStatus=value; } },
  openHermesDashboard: { enumerable:true, get:()=>openHermesDashboard, set:value=>{ openHermesDashboard=value; } },
  _initDashboardLinkProbe: { enumerable:true, get:()=>_initDashboardLinkProbe, set:value=>{ _initDashboardLinkProbe=value; } },
  refreshDashboardStatus: { enumerable:true, get:()=>refreshDashboardStatus, set:value=>{ refreshDashboardStatus=value; } },
  loadDashboardSettings: { enumerable:true, get:()=>loadDashboardSettings, set:value=>{ loadDashboardSettings=value; } },
  saveDashboardSettings: { enumerable:true, get:()=>saveDashboardSettings, set:value=>{ saveDashboardSettings=value; } },
  DASHBOARD_STATUS_TTL_MS: { enumerable:true, get:()=>DASHBOARD_STATUS_TTL_MS },
  _dashboardStatusCache: { enumerable:true, get:()=>_dashboardStatusCache, set:value=>{ _dashboardStatusCache=value; } },
  _dashboardStatusFetchedAt: { enumerable:true, get:()=>_dashboardStatusFetchedAt, set:value=>{ _dashboardStatusFetchedAt=value; } },
  _dashboardLastNonNeverMode: { enumerable:true, get:()=>_dashboardLastNonNeverMode, set:value=>{ _dashboardLastNonNeverMode=value; } },
  _dashboardSettingsLoadSeq: { enumerable:true, get:()=>_dashboardSettingsLoadSeq, set:value=>{ _dashboardSettingsLoadSeq=value; } },
  _dashboardSettingsWriteSeq: { enumerable:true, get:()=>_dashboardSettingsWriteSeq, set:value=>{ _dashboardSettingsWriteSeq=value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
