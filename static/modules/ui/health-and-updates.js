import { setStatus, showToast } from './composer.js';
import { showConfirmDialog } from './app-dialogs.js';
import { INFLIGHT_KEY } from './inflight-state.js';
import { clearInflight, dismissReconnect, showReconnectBanner } from './reconnect-banner.js';
import { _isContextCompactionMessage } from './live-activity.js';
import { msgContent } from './assistant-turn-presentation.js';
import { syncTopbar } from './topbar-presentation.js';
import { _renderMessagesWithScrollSnapshot } from './render-support.js';
import { $, S } from './state.js';

// ── Live host resource health panel (#693) ──
const SYSTEM_HEALTH_INTERVAL_MS=5000;
let _systemHealthTimer=null;
function _systemHealthPercent(metric){
  const percent=Number(metric&&metric.percent);
  if(!Number.isFinite(percent)) return null;
  return Math.max(0,Math.min(100,Math.round(percent*10)/10));
}
function _formatSystemHealthPercent(percent){
  if(percent == null) return '—';
  return `${percent.toFixed(percent%1?1:0)}%`;
}
function _formatSystemHealthBytes(metric){
  if(!metric||!metric.used_bytes||!metric.total_bytes) return '';
  const units=['B','KB','MB','GB','TB'];
  const fmt=(bytes)=>{
    let value=Number(bytes)||0, idx=0;
    while(value>=1024&&idx<units.length-1){value/=1024;idx++;}
    return `${value.toFixed(value>=10||idx===0?0:1)} ${units[idx]}`;
  };
  return `${fmt(metric.used_bytes)} / ${fmt(metric.total_bytes)}`;
}
function _updateSystemHealthMetric(name,metric){
  const row=document.querySelector(`[data-system-health-metric="${name}"]`);
  if(!row) return;
  const rawPercent=_systemHealthPercent(metric);
  const percent=rawPercent == null ? 0 : rawPercent;
  const label=row.querySelector('[data-system-health-value]');
  const bar=row.querySelector('.system-health-bar');
  const fill=row.querySelector('.system-health-bar-fill');
  const text=_formatSystemHealthPercent(rawPercent);
  if(label){
    label.textContent=text;
    const bytes=(name==='memory'||name==='disk')?_formatSystemHealthBytes(metric):'';
    label.title=bytes||text;
  }
  if(bar) bar.setAttribute('aria-valuenow',String(percent));
  if(fill) fill.style.width=`${percent}%`;
}
function setSystemHealthUnavailable(message){
  const panel=$('systemHealthPanel');
  const status=$('systemHealthStatus');
  if(!panel) return;
  panel.classList.remove('loading');
  panel.classList.add('unavailable');
  if(status) status.textContent=message||'Unavailable';
  ['cpu','memory','disk'].forEach(name=>_updateSystemHealthMetric(name,null));
}
function renderSystemHealth(payload){
  const panel=$('systemHealthPanel');
  const status=$('systemHealthStatus');
  if(!panel) return;
  if(!payload||payload.available===false){
    setSystemHealthUnavailable('Unavailable');
    return;
  }
  panel.classList.remove('loading','unavailable');
  if(status) status.textContent=payload.status==='partial'?'Partial':'Live';
  _updateSystemHealthMetric('cpu',payload.cpu);
  _updateSystemHealthMetric('memory',payload.memory);
  _updateSystemHealthMetric('disk',payload.disk);
}
async function pollSystemHealth(){
  if(document.visibilityState !== 'visible') return;
  if(!_systemHealthPanelIsVisible()) return;
  try{
    const payload=await api('/api/system/health',{timeoutToast:false});
    renderSystemHealth(payload);
  }catch(_){
    setSystemHealthUnavailable('Unavailable');
  }
}
function _systemHealthPanelIsVisible(){
  return document.visibilityState === 'visible' &&
    !!document.querySelector('main.main.showing-insights') &&
    !!$('systemHealthPanel');
}
function startSystemHealthMonitor(){
  if(!_systemHealthPanelIsVisible()) return;
  if(_systemHealthTimer) return;
  void pollSystemHealth();
  _systemHealthTimer=setInterval(pollSystemHealth,SYSTEM_HEALTH_INTERVAL_MS);
}
function stopSystemHealthMonitor(){
  if(_systemHealthTimer){clearInterval(_systemHealthTimer);_systemHealthTimer=null;}
}
function _syncSystemHealthMonitorVisibility(){
  if(_systemHealthPanelIsVisible()) startSystemHealthMonitor();
  else stopSystemHealthMonitor();
}
document.addEventListener('visibilitychange',_syncSystemHealthMonitorVisibility);
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startSystemHealthMonitor);
else startSystemHealthMonitor();

// ── Hermes agent/gateway heartbeat alert (#716) ──
const AGENT_HEALTH_INTERVAL_MS=30000;
const AGENT_HEALTH_DISMISSED_KEY='agent-health-dismissed';
let _agentHealthTimer=null;
let _agentHealthLastState='unknown';
let _lastGatewayRestartTime=0;
function _agentHealthDismissed(){
  try{return localStorage.getItem(AGENT_HEALTH_DISMISSED_KEY)==='1';}
  catch(_){return false;}
}
function _setAgentHealthDismissed(value){
  try{
    if(value)localStorage.setItem(AGENT_HEALTH_DISMISSED_KEY,'1');
    else localStorage.removeItem(AGENT_HEALTH_DISMISSED_KEY);
  }catch(_){ }
}
function _hideAgentHealthAlert(){
  const banner=$('agentHealthBanner');
  if(banner){banner.classList.remove('visible');banner.hidden=true;}
}
function _showAgentHealthAlert(payload){
  if(_agentHealthDismissed()) return;
  const banner=$('agentHealthBanner');
  const title=$('agentHealthTitle');
  const details=$('agentHealthDetails');
  if(!banner) return;
  if(title) title.textContent='Hermes agent is not responding';
  const state=payload&&payload.details&&payload.details.gateway_state?` State: ${payload.details.gateway_state}.`:'';
  if(details) details.textContent=`Gateway heartbeat failed.${state} Messages may not be delivered until it comes back.`;
  banner.hidden=false;
  banner.classList.add('visible');
}
function dismissAgentHealthAlert(){
  _setAgentHealthDismissed(true);
  _hideAgentHealthAlert();
}
async function restartGatewayService(){
  const btn = $('btnRestartGateway');
  const dismissBtn = $('agentHealthDismiss');
  if(!btn) return;
  btn.disabled = true;
  if(dismissBtn) dismissBtn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = 'Restarting...';
  try {
    const res = await api('/api/health/restart', {method: 'POST'});
    if(res && res.ok){
      showToast('Gateway service restarted successfully');
      _hideAgentHealthAlert();
      _lastGatewayRestartTime = Date.now();
      setTimeout(pollAgentHealth, 15000);
    } else {
      showToast(res && res.error || 'Failed to restart gateway service');
    }
  } catch(e) {
    showToast('Failed to restart gateway service: ' + e.message);
  } finally {
    btn.disabled = false;
    if(dismissBtn) dismissBtn.disabled = false;
    btn.textContent = originalText;
  }
}
async function pollAgentHealth(){
  if(document.visibilityState !== 'visible') return;
  if(Date.now() - _lastGatewayRestartTime < 15000) return;
  try{
    const payload=await api('/api/health/agent',{timeoutToast:false});
    if(payload.alive === true){
      _agentHealthLastState='alive';
      _setAgentHealthDismissed(false);
      _hideAgentHealthAlert();
      return;
    }
    if(payload.alive === false){
      _agentHealthLastState='down';
      _showAgentHealthAlert(payload);
      return;
    }
    if(payload.alive == null){
      _agentHealthLastState='unknown';
      _hideAgentHealthAlert();
    }
  }catch(_){
    _agentHealthLastState='unknown';
    _hideAgentHealthAlert();
  }
}
function startAgentHealthMonitor(){
  if(document.visibilityState !== 'visible') return;
  if(_agentHealthTimer) return;
  void pollAgentHealth();
  _agentHealthTimer=setInterval(pollAgentHealth, AGENT_HEALTH_INTERVAL_MS);
}
function stopAgentHealthMonitor(){
  if(_agentHealthTimer){clearInterval(_agentHealthTimer);_agentHealthTimer=null;}
}
function _syncAgentHealthMonitorVisibility(){
  if(document.visibilityState === 'visible') startAgentHealthMonitor();
  else stopAgentHealthMonitor();
}
document.addEventListener('visibilitychange',_syncAgentHealthMonitorVisibility);
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startAgentHealthMonitor);
else startAgentHealthMonitor();
async function refreshSession() {
  // When the banner is in post-update restart mode, the "Reload" button
  // should do a full page reload — a session refresh would just 502 while
  // the server is still restarting.
  if (window._restartingForUpdate) { location.reload(); return; }
  dismissReconnect();
  if (!S.session) return;
  try {
    const data = await api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`);
    S.session = data.session;
    S.messages = data.session.messages || [];
    _messagesTruncated = !!data.session._messages_truncated;
    _oldestIdx = data.session._messages_offset || 0;
    const pendingMsg=getPendingSessionMessage(data.session,S.messages);
    if(pendingMsg) S.messages.push(pendingMsg);
    S.activeStreamId=data.session.active_stream_id||null;

    syncTopbar(); _renderMessagesWithScrollSnapshot();
    showToast('Conversation refreshed');
  } catch(e) { setStatus('Refresh failed: ' + e.message); }
}
// ── Update banner ──
function _formatUpdateTargetStatus(label,info){
  const manualNoGit=!!(info&&info.no_git&&info.manual_update&&info.behind>0);
  if(!info||(info.no_git&&!manualNoGit)||!(info.behind>0)) return null;
  const release=(info.release_based&&info.latest_version)
    ?` (${info.current_version||'unknown'} -> ${info.latest_version})`
    :(info.branch?` (${info.branch})`:'');
  const noun=info.release_based?'release':'update';
  return `${label}${release}: ${info.behind} ${noun}${info.behind>1?'s':''}`;
}
function _formatManualUpdateInstruction(info){
  if(!(info&&info.no_git&&info.manual_update&&info.behind>0)) return null;
  return t('settings_update_manual_docker','docker pull ghcr.io/nesquena/hermes-webui:latest');
}
function _formatUpdateCheckError(label,info){
  if(!info||!info.error) return null;
  const detail=String(info.error).replace(/^fetch failed:?\s*/i,'').trim();
  return detail ? `${label}: ${detail}` : label;
}
function _isSafeUpdateCompareUrl(url){
  if(!url||!/^https?:\/\//i.test(url)) return false;
  try{
    const parsed=new URL(url);
    return parsed.protocol==='https:'||parsed.protocol==='http:';
  }catch(e){
    return false;
  }
}
function _updateCompareUrl(info){
  if(!info) return null;
  const compareUrl=info.compare_url||null;
  if(compareUrl) return _isSafeUpdateCompareUrl(compareUrl)?compareUrl:null;
  const repo_url=info.repo_url;
  const currentSha=info.current_sha;
  const latestSha=info.latest_sha;
  if(!(repo_url&&currentSha&&latestSha)) return null;
  const fallbackUrl=repo_url+'/compare/'+currentSha+'...'+latestSha;
  return _isSafeUpdateCompareUrl(fallbackUrl)?fallbackUrl:null;
}
function _updateWhatsNewTargets(data){
  const targets=[
    {key:'webui',label:'WebUI',info:data&&data.webui},
    {key:'agent',label:'Agent',info:data&&data.agent},
  ];
  return targets.map((target)=>({
    key:target.key,
    label:target.label,
    info:target.info,
    url:_updateCompareUrl(target.info),
  })).filter((target)=>target.info&&target.info.behind>0&&target.url);
}
function _appendUpdateDiffLinks(container,targets,prefix){
  if(!container) return;
  if(prefix) container.appendChild(document.createTextNode(prefix));
  targets.forEach((target,idx)=>{
    if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent=target.label;
    container.appendChild(link);
  });
}
function _hideUpdateSummaryPanel(){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  const toolbar=$('updateSummaryToolbar');
  if(panel){
    panel.style.display='none';
    panel.classList.remove('update-summary-expanded');
  }
  if(toolbar) toolbar.style.display='none';
  _syncUpdateSummaryExpandButton(false);
  if(text) text.textContent='';
  if(links){links.replaceChildren();links.style.display='none';}
}
function _syncUpdateSummaryExpandButton(expanded){
  const btn=$('btnUpdateSummaryExpand');
  if(!btn) return;
  btn.setAttribute('aria-expanded',expanded?'true':'false');
  btn.textContent=expanded?'Collapse summary':'Expand summary';
}
function toggleUpdateSummaryExpanded(){
  const panel=$('updateSummaryPanel');
  if(!panel||panel.style.display==='none') return;
  const expanded=!panel.classList.contains('update-summary-expanded');
  panel.classList.toggle('update-summary-expanded',expanded);
  _syncUpdateSummaryExpandButton(expanded);
}
const WHATS_NEW_SUMMARY_STORAGE_KEY='hermes-whats-new-generated-summaries';
const WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES=256*1024;
function _summaryStorageByteLength(value){
  const text=typeof value==='string'?value:JSON.stringify(value);
  if(text==null) return 0;
  if(typeof TextEncoder==='function') return new TextEncoder().encode(text).length;
  let bytes=0;
  for(const ch of text){
    const code=ch.codePointAt(0);
    bytes+=code<=0x7f?1:(code<=0x7ff?2:(code<=0xffff?3:4));
  }
  return bytes;
}
function _summaryCacheEntriesSortedByRecency(entries){
  return entries.slice().sort((left,right)=>{
    const leftKey=left[0];
    const rightKey=right[0];
    const leftSummary=left[1];
    const rightSummary=right[1];
    const leftUpdatedAt=leftSummary&&leftSummary.updatedAt;
    const rightUpdatedAt=rightSummary&&rightSummary.updatedAt;
    if(typeof leftUpdatedAt==='number'&&typeof rightUpdatedAt==='number'&&leftUpdatedAt!==rightUpdatedAt){
      return rightUpdatedAt-leftUpdatedAt;
    }
    if(typeof leftUpdatedAt==='number') return -1;
    if(typeof rightUpdatedAt==='number') return 1;
    if(leftKey==='webui'&&rightKey!=='webui') return -1;
    if(rightKey==='webui'&&leftKey!=='webui') return 1;
    if(leftKey==='agent'&&rightKey!=='agent') return -1;
    if(rightKey==='agent'&&leftKey!=='agent') return 1;
    return leftKey<rightKey?-1:(leftKey>rightKey?1:0);
  });
}
function _loadStoredUpdateSummaries(){
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  try{
    const raw=sessionStorage.getItem(WHATS_NEW_SUMMARY_STORAGE_KEY);
    if(!raw) return window._whatsNewGeneratedSummaries;
    const stored=JSON.parse(raw);
    if(stored&&typeof stored==='object') window._whatsNewGeneratedSummaries=stored;
  }catch(_e){
    try{sessionStorage.removeItem(WHATS_NEW_SUMMARY_STORAGE_KEY);}catch(_ignore){}
  }
  return window._whatsNewGeneratedSummaries;
}
function _persistGeneratedSummaries(){
  const current=window._whatsNewGeneratedSummaries||{};
  const next={};
  try{
    _summaryCacheEntriesSortedByRecency(Object.entries(current)).forEach((entry)=>{
      const candidate={...next,...Object.fromEntries([entry])};
      if(_summaryStorageByteLength(JSON.stringify(candidate))<=WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES){
        Object.assign(next, Object.fromEntries([entry]));
      }
    });
    window._whatsNewGeneratedSummaries=next;
    sessionStorage.setItem(WHATS_NEW_SUMMARY_STORAGE_KEY,JSON.stringify(next));
  }catch(_e){}
}
function _pruneGeneratedSummaries(data){
  const cache=_loadStoredUpdateSummaries();
  const valid=new Set(_updateWhatsNewTargets(data||{}).map((target)=>target.key));
  let changed=false;
  Object.keys(cache).forEach((key)=>{
    if(!valid.has(key)){delete cache[key];changed=true;}
  });
  if(changed) _persistGeneratedSummaries();
}
function _updateSummarySignature(info){
  if(!info) return '';
  return [info.current_sha||'',info.latest_sha||'',info.behind||0,info.compare_url||''].join('|');
}
function _updateSummaryButtonLabel(target,data){
  const labels=target.key==='webui'
    ? {generate:'Generate WebUI update summary',view:'View generated WebUI update summary',regenerate:'Re-generate WebUI update summary'}
    : {generate:'Generate Agent update summary',view:'View generated Agent update summary',regenerate:'Re-generate Agent update summary'};
  const cache=_loadStoredUpdateSummaries()[target.key];
  const signature=_updateSummarySignature(data&&data[target.key]);
  if(cache&&cache.signature===signature&&cache.payload) return labels.view;
  if(cache&&cache.signature!==signature) return labels.regenerate;
  return labels.generate;
}
function _rememberGeneratedSummary(target,payload,data){
  if(!target) return;
  window._whatsNewGeneratedSummaries=window._whatsNewGeneratedSummaries||{};
  window._whatsNewGeneratedSummaries[target]={
    signature:_updateSummarySignature(data&&data[target]),
    payload:payload,
    updatedAt:Date.now(),
  };
  _persistGeneratedSummaries();
}
function _renderUpdateSummaryPanel(payload,data,targetKey){
  const panel=$('updateSummaryPanel');
  const text=$('updateSummaryText');
  const links=$('updateSummaryDiffLinks');
  const toolbar=$('updateSummaryToolbar');
  if(!panel||!text) return;
  panel.style.display='block';
  panel.classList.remove('update-summary-expanded');
  _syncUpdateSummaryExpandButton(false);
  if(toolbar) toolbar.style.display='flex';
  const sections=Array.isArray(payload&&payload.summary_sections)?payload.summary_sections:null;
  text.replaceChildren();
  if(sections&&sections.length){
    const wrap=document.createElement('div');
    wrap.id='updateSummarySections';
    wrap.style.display='grid';
    wrap.style.gap='8px';
    sections.forEach((section)=>{
      const block=document.createElement('section');
      const title=document.createElement('div');
      title.style.fontWeight='650';
      title.style.marginBottom='3px';
      title.textContent=section.title||'Summary';
      block.appendChild(title);
      const ul=document.createElement('ul');
      ul.style.margin='0';
      ul.style.paddingLeft='18px';
      (Array.isArray(section.items)?section.items:[]).forEach((item)=>{
        const li=document.createElement('li');
        li.textContent=String(item||'').trim();
        if(li.textContent) ul.appendChild(li);
      });
      if(!ul.children.length){
        const li=document.createElement('li');
        li.textContent='No summary details available.';
        ul.appendChild(li);
      }
      block.appendChild(ul);
      wrap.appendChild(block);
    });
    text.appendChild(wrap);
  }else{
    text.textContent=(payload&&payload.summary)||payload||'No summary available.';
  }
  const targets=_updateWhatsNewTargets(data||window._updateData||{}).filter((target)=>!targetKey||target.key===targetKey);
  if(links){
    links.replaceChildren();
    if(targets.length){
      links.style.display='block';
      _appendUpdateDiffLinks(links,targets,'Regular diff comparison: ');
    }else{
      links.style.display='none';
    }
  }
}
async function showWhatsNewSummary(target){
  const data=window._updateData||{};
  const scopedUpdates=target?{[target]:data[target]}:data;
  const cache=target?_loadStoredUpdateSummaries()[target]:null;
  const signature=target?_updateSummarySignature(data[target]):'';
  if(cache&&cache.signature===signature&&cache.payload){
    _renderUpdateSummaryPanel(cache.payload,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
    return;
  }
  _renderUpdateSummaryPanel({summary:'Writing a simple summary…'},data,target);
  try{
    const res=await api('/api/updates/summary',{method:'POST',body:JSON.stringify({updates:scopedUpdates,target:target||null}),timeoutMs:60000});
    _rememberGeneratedSummary(target,res,data);
    _renderUpdateSummaryPanel(res,data,target);
    _renderUpdateWhatsNewLinks(data,{mode:'summary'});
  }catch(e){
    console.warn('[updates] summary failed',e);
    _renderUpdateSummaryPanel({
      summary_sections:[
        {title:"What you'll notice",items:['Could not generate the summary right now.']},
        {title:'Worth knowing',items:['Try again later, or use the comparison links below for the raw update details.']},
      ],
    },data,target);
  }
}
function _renderUpdateWhatsNewLinks(data){
  const options=arguments.length>1&&arguments[1]?arguments[1]:{};
  const container=$('updateWhatsNewLinks');
  if(!container) return;
  container.replaceChildren();
  const targets=_updateWhatsNewTargets(data);
  if(!targets.length){
    container.style.display='none';
    _hideUpdateSummaryPanel();
    return;
  }
  container.style.display='block';
  _pruneGeneratedSummaries(data);
  const useSummary=(options.mode||'')==='summary'||window._whatsNewSummaryEnabled===true;
  if(useSummary){
    targets.forEach((target,idx)=>{
      if(idx>0) container.appendChild(document.createTextNode(' \u00b7 '));
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='linklike';
      btn.style.color='var(--accent)';
      btn.style.textDecoration='underline';
      btn.style.background='none';
      btn.style.border='0';
      btn.style.padding='0';
      btn.style.cursor='pointer';
      btn.textContent=_updateSummaryButtonLabel(target,data);
      btn.onclick=()=>showWhatsNewSummary(target.key);
      container.appendChild(btn);
    });
    return;
  }
  _hideUpdateSummaryPanel();
  if(targets.length===1){
    const target=targets[0];
    const link=document.createElement('a');
    link.href=target.url;
    link.target='_blank';
    link.rel='noopener';
    link.style.color='var(--accent)';
    link.style.textDecoration='underline';
    link.textContent="What's new in "+target.label+'?';
    container.appendChild(link);
    return;
  }
  _appendUpdateDiffLinks(container,targets,"What's new: ");
}
function _showUpdateBanner(data){
  const parts=[];
  const webuiPart=_formatUpdateTargetStatus('WebUI',data.webui);
  const agentPart=_formatUpdateTargetStatus('Agent',data.agent);
  if(webuiPart) parts.push(webuiPart);
  if(agentPart) parts.push(agentPart);
  window._updateData=data;
  const btnApply=$('btnApplyUpdate');
  if(btnApply){
    const webuiManual=!!(data&&data.webui&&data.webui.manual_update&&data.webui.behind>0);
    const webuiUpdatable=!!(data&&data.webui&&data.webui.behind>0&&!webuiManual);
    const agentUpdatable=!!(data&&data.agent&&data.agent.behind>0);
    const hasApplyTargets=webuiUpdatable||agentUpdatable;
    btnApply.disabled=!hasApplyTargets;
    btnApply.style.display=hasApplyTargets?'':'none';
    if(webuiManual){
      const forceBtn=$('btnForceUpdate');
      if(forceBtn){forceBtn.disabled=true;forceBtn.style.display='none';forceBtn.dataset.target='';}
      const clearLockBtn=$('btnClearUpdateLock');
      if(clearLockBtn){clearLockBtn.disabled=true;clearLockBtn.style.display='none';clearLockBtn.dataset.target='';}
    }
  }
  if(!parts.length){
    _renderUpdateWhatsNewLinks(data);
    const staleBanner=$('updateBanner');
    if(staleBanner) staleBanner.classList.remove('visible');
    return;
  }
  const msg=$('updateMsg');
  if(msg){
    const manualInstruction=_formatManualUpdateInstruction(data&&data.webui);
    msg.textContent='\u2B06 '+parts.join(', ')+' available'+(manualInstruction?' · '+manualInstruction:'');
  }
  const banner=$('updateBanner');
  if(banner) banner.classList.add('visible');
  const summaryMode=window._whatsNewSummaryEnabled===true?'summary':'diff';
  _renderUpdateWhatsNewLinks(data,{mode:summaryMode});
}
function _i18nUpdateText(key, fallback){
  if(typeof t==='function'){
    const val=t(key);
    if(val&&val!==key) return val;
  }
  return fallback;
}
function dismissUpdate(){
  const b=$('updateBanner');if(b)b.classList.remove('visible');
  sessionStorage.setItem('hermes-update-dismissed','1');
}
function _isUpdateApplyNetworkError(error){
  if(error && error.status) return false;
  const message=(error&&error.message)||String(error||'');
  return /Failed to fetch|NetworkError|Load failed/i.test(message);
}
function _formatUpdateApplyExceptionMessage(error){
  if(_isUpdateApplyNetworkError(error)){
    return _i18nUpdateText('update_failed_network','Update failed: could not reach the WebUI server. It may have restarted or the connection was interrupted. Please wait a few seconds, reload the page, then check the server if it still does not come back.');
  }
  const message=(error&&error.message)||String(error||'unknown error');
  return _i18nUpdateText('update_failed_prefix','Update failed: ')+message;
}
async function applyUpdates(){
  if(window._updateApplyInFlight) return;
  window._updateApplyInFlight=true;
  const updateText=(key,fallback)=>(typeof _i18nUpdateText==='function'?_i18nUpdateText(key,fallback):fallback);
  const btn=$('btnApplyUpdate');
  const resetApplyButton=(delayMs)=>{
    const reset=()=>{
      window._updateApplyInFlight=false;
      if(btn){btn.disabled=false;btn.textContent=updateText('update_now','Update Now');}
    };
    if(delayMs>0) setTimeout(reset,delayMs);
    else reset();
  };
  if(btn){btn.disabled=true;btn.textContent=updateText('update_updating','Updating\u2026');}
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';errEl.textContent='';}
  // Hide any leftover force-update button from a prior conflict so a fresh
  // retry starts clean (otherwise stale state points at the wrong target).
  const forceBtnReset=$('btnForceUpdate');
  if(forceBtnReset){forceBtnReset.style.display='none';forceBtnReset.dataset.target='';}
  const targets=[];
  if(window._updateData?.agent?.behind>0) targets.push('agent');
  if(window._updateData?.webui?.behind>0&&!window._updateData?.webui?.manual_update) targets.push('webui');
  if(!targets.length){
    const msg=updateText('update_no_target','No update target selected. Refresh update status and retry.');
    if(errEl){errEl.textContent=msg;errEl.style.display='block';}
    else showToast(msg,5000,'error');
    resetApplyButton(0);
    return;
  }
  try{
    const stashConflictMessages=[];
    const baselineServerIdentity = await _readHealthServerIdentity();
    for(const target of targets){
      // Send the channel the CHECK reported for this target (what was actually
      // offered in the banner), not a fresh settings read — otherwise a channel
      // switch whose debounced autosave hasn't landed yet races apply, which
      // would then read the OLD saved channel (Codex gate). webui carries the
      // channel; agent is channel-neutral server-side so omitting it is fine.
      const _applyBody={target};
      const _ch=window._updateData?.[target]?.channel;
      if(_ch==='stable'||_ch==='experimental') _applyBody.channel=_ch;
      const res=await api('/api/updates/apply',{method:'POST',body:JSON.stringify(_applyBody),timeoutMs:120000});
      if(!res.ok){
        _showUpdateError(target,res);
        resetApplyButton(0);
        return;
      }
      if(res.stash_conflict){
        stashConflictMessages.push('Update applied ('+target+'): '+(res.message||'Local changes were preserved in git stash.'));
        if(errEl){errEl.textContent=stashConflictMessages.join('\n\n');errEl.style.display='block';}
      }
    }
    const stashConflictMessage=stashConflictMessages.join('\n\n');
    showToast(stashConflictMessage||'Update applied — restarting…',stashConflictMessages.length?10000:undefined,stashConflictMessages.length?'warning':undefined);
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload({baselineServerIdentity});
  }catch(e){
    const msg=_formatUpdateApplyExceptionMessage(e);
    if(errEl){errEl.textContent=msg;errEl.style.display='block';}
    else showToast(msg);
    resetApplyButton(_isUpdateApplyNetworkError(e)?5000:0);
  }
}
function _showUpdateError(target,res){
  const errEl=$('updateError');
  const forceBtn=$('btnForceUpdate');
  const msg='Update failed ('+target+'): '+(res.message||'unknown error');
  if(errEl){
    errEl.textContent=msg;
    errEl.style.display='block';
  } else {
    showToast(msg);
  }
  // Show "Force update" button ONLY for errors recoverable by a destructive
  // hard reset. Lock-only failures are routed to a separate non-destructive
  // "Clear lock and retry update" button (BRICK-2 fix for PR #5688: a lock
  // error should never invoke apply_force_update, which would discard local
  // modifications).
  if(forceBtn&&(res.conflict||res.diverged)){
    forceBtn.dataset.target=target;
    forceBtn.style.display='inline-block';
  }
  // Show "Clear lock and retry update" when the only failure was a stale
  // git lock. This calls the new non-destructive /api/updates/clear_lock
  // endpoint, which probes the lock for a holder and refuses if held.
  const clearLockBtn=$('btnClearUpdateLock');
  if(clearLockBtn&&res.lock_conflict){
    clearLockBtn.dataset.target=target;
    clearLockBtn.style.display='inline-block';
  }
}
async function applyClearUpdateLock(btn){
  if(window._clearLockInFlight) return;
  const target=btn.dataset.target;
  if(!target) return;
  window._clearLockInFlight=true;
  btn.disabled=true;
  const originalLabel=btn.textContent;
  btn.textContent='Checking lock…';
  try{
    const res=await api('/api/updates/clear_lock',{method:'POST',body:JSON.stringify({target}),timeoutMs:60000});
    if(res.ok){
      sessionStorage.removeItem('hermes-update-checked');
      sessionStorage.removeItem('hermes-update-dismissed');
      showToast('Update applied — restarting…');
      _waitForServerThenReload({});
    } else if(res.lock_held){
      // v2.2: server returns manual-instruction. Show the exact `rm`
      // command + a one-click "I've removed it, retry update" affordance
      // that POSTs the same endpoint a second time (now that the user
      // has presumably removed the lock, the server's success branch
      // runs the normal non-destructive apply).
      _renderLockManualInstruction(target, res);
    } else {
      const msg='Could not check the lock: '+(res.message||'unknown error');
      const errEl=$('updateError');
      if(errEl){errEl.textContent=msg;errEl.style.display='block';}
      else showToast(msg);
    }
  }catch(e){
    const msg='Lock-check request failed: '+((e&&e.message)||String(e));
    const errEl=$('updateError');
    if(errEl){errEl.textContent=msg;errEl.style.display='block';}
    else showToast(msg);
  }finally{
    window._clearLockInFlight=false;
    btn.disabled=false;
    btn.textContent=originalLabel;
  }
}
function _renderLockManualInstruction(target, res){
  // Replace the inline `updateError` text with a richer block that shows
  // the exact manual command and offers a one-click retry button. The
  // "retry" handler re-invokes `applyClearUpdateLock`; this time, with
  // the lock gone, the server's success branch runs the normal apply.
  const cmd = res.manual_command || ('rm -f ' + (res.well_known_lock_path || '.git/index.lock'));
  const errEl=$('updateError');
  if(!errEl){
    showToast('Lock present. Run: '+cmd);
    return;
  }
  errEl.style.display='block';
  errEl.innerHTML='';
  const intro=document.createElement('div');
  intro.style.marginBottom='6px';
  intro.textContent='A stale .git/index.lock is present. The server cannot remove it safely — please run this command on the host:';
  errEl.appendChild(intro);
  const code=document.createElement('pre');
  code.style.background='rgba(0,0,0,0.05)';
  code.style.padding='6px';
  code.style.margin='4px 0';
  code.style.fontFamily='ui-monospace,monospace';
  code.style.borderRadius='4px';
  code.style.whiteSpace='pre-wrap';
  code.style.wordBreak='break-all';
  code.textContent=cmd;
  errEl.appendChild(code);
  const actions=document.createElement('div');
  actions.style.display='flex';
  actions.style.gap='8px';
  actions.style.flexWrap='wrap';
  const copyBtn=document.createElement('button');
  copyBtn.type='button';
  copyBtn.className='update-btn';
  copyBtn.textContent='Copy command';
  copyBtn.onclick=async()=>{
    try{
      if(navigator.clipboard&&navigator.clipboard.writeText){
        await navigator.clipboard.writeText(cmd);
        copyBtn.textContent='Copied';
        setTimeout(()=>{ copyBtn.textContent='Copy command'; }, 1500);
      } else {
        copyBtn.textContent='Clipboard unavailable';
      }
    } catch(_){
      copyBtn.textContent='Copy failed';
    }
  };
  actions.appendChild(copyBtn);
  const retryBtn=document.createElement('button');
  retryBtn.type='button';
  retryBtn.className='update-btn update-primary';
  retryBtn.textContent="I've removed the lock — retry update";
  retryBtn.dataset.target=target;
  retryBtn.onclick=()=>{ applyClearUpdateLock(retryBtn); };
  actions.appendChild(retryBtn);
  errEl.appendChild(actions);
  if(Array.isArray(res.other_locks)&&res.other_locks.length){
    const other=document.createElement('div');
    other.style.marginTop='6px';
    other.style.fontSize='11px';
    other.style.opacity='0.85';
    other.textContent='Other lock files also present: '+res.other_locks.join(', ');
    errEl.appendChild(other);
  }
}
function _normalizeHealthServerIdentity(rawIdentity){
  if(rawIdentity===undefined||rawIdentity===null) return null;
  if(typeof rawIdentity==='string'){
    const value=rawIdentity.trim();
    return value ? value : null;
  }
  const numeric=Number(rawIdentity);
  return Number.isFinite(numeric) ? String(numeric) : null;
}

function _healthResponseServerIdentity(data){
  if(!data||typeof data!=='object') return null;
  const serverStartedAt=_normalizeHealthServerIdentity(data.server_started_at);
  const hasUptimeSeconds=data.uptime_seconds!==null&&data.uptime_seconds!==undefined;
  const uptimeSeconds=hasUptimeSeconds?Number(data.uptime_seconds):NaN;
  const normalizedUptime=Number.isFinite(uptimeSeconds)&&uptimeSeconds>=0 ? uptimeSeconds : null;
  if(serverStartedAt===null&&normalizedUptime===null) return null;
  return {serverStartedAt,uptimeSeconds:normalizedUptime};
}

async function _readHealthServerIdentity() {
  try {
    const r=await fetch(new URL('health', document.baseURI||location.href).href,{cache:'no-store'});
    if(!r.ok) return null;
    const data=await r.json();
    return _healthResponseServerIdentity(data);
  } catch (_) {
    return null;
  }
}
async function forceUpdate(btn){
  const target=btn&&btn.dataset.target;
  if(!target) return;
  const confirmed=await showConfirmDialog({
    title:'Force update '+target+'?',
    message:'This will discard all local changes and delete untracked files in the '+target+' repo, then reset to the latest remote version. This cannot be undone.',
    confirmLabel:'Force update',
    danger:true,
    focusCancel:true,
  });
  if(!confirmed) return;
  btn.disabled=true;btn.textContent='Force updating\u2026';
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';}
  try{
    const baselineServerIdentity = await _readHealthServerIdentity();
    const res=await api('/api/updates/force',{method:'POST',body:JSON.stringify((()=>{const b={target};const _ch=window._updateData?.[target]?.channel;if(_ch==='stable'||_ch==='experimental')b.channel=_ch;return b;})()),timeoutMs:120000});
    if(!res.ok){
      if(errEl){errEl.textContent='Force update failed: '+(res.message||'unknown error');errEl.style.display='block';}
      btn.disabled=false;btn.textContent='Force update';
      return;
    }
    showToast('Force update applied — restarting…');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload({baselineServerIdentity});
  }catch(e){
    if(errEl){errEl.textContent='Force update failed: '+e.message;errEl.style.display='block';}
    btn.disabled=false;btn.textContent='Force update';
  }
}

// Poll /health after an update-triggered restart, then reload.  Replaces the
// blind setTimeout(reload, 2500) that race-lost against slow hardware or
// reverse proxies that 502 immediately when the upstream socket closes (#874).
async function _waitForServerThenReload(opts){
  // Polls the /health endpoint; implementation uses a relative URL so subpath mounts keep working.
  opts=opts||{};
  const interval=opts.interval||500;
  const maxMs=opts.maxMs||15000;
  const baselineServerIdentity=(()=>{
    const rawIdentity=opts.baselineServerIdentity;
    if(!rawIdentity||typeof rawIdentity!=='object'){
      const normalizedServerStartedAt=_normalizeHealthServerIdentity(rawIdentity);
      return normalizedServerStartedAt===null ? null : {serverStartedAt:normalizedServerStartedAt,uptimeSeconds:null};
    }
    const normalizedIdentity={
      serverStartedAt:_normalizeHealthServerIdentity(rawIdentity.serverStartedAt),
      uptimeSeconds:Number.isFinite(Number(rawIdentity.uptimeSeconds))&&Number(rawIdentity.uptimeSeconds)>=0 ? Number(rawIdentity.uptimeSeconds) : null,
    };
    return normalizedIdentity.serverStartedAt===null&&normalizedIdentity.uptimeSeconds===null ? null : normalizedIdentity;
  })();
  window._restartingForUpdate=true;
  const msgEl=$('reconnectMsg');
  const banner=$('reconnectBanner');
  if(msgEl) msgEl.textContent='⏳ Restarting… please wait';
  if(banner) banner.classList.add('visible');
  const deadline=Date.now()+maxMs;
  // Track restart-outage evidence. An outage (failed or non-OK /health probes)
  // followed by a healthy response is a reliable new-instance signal even when
  // only uptime_seconds is comparable and the replacement's uptime is not strictly
  // lower than the captured baseline (e.g. a deployment that strips
  // server_started_at and whose baseline uptime was very low). We require at least
  // TWO consecutive outage probes before trusting it, so a single transient network
  // blip (with the OLD process still up and its uptime merely increasing) cannot
  // trigger a premature reload onto the old server. Both thrown fetch errors AND
  // non-OK responses (e.g. a reverse-proxy 502/503 during restart) count as outage
  // evidence. (#3713 Codex catches)
  let _consecutiveOutages=0;
  const _restartOutageObserved=()=>_consecutiveOutages>=2;
  // Give the server a moment to actually begin its restart before the first
  // probe — otherwise the old process may still respond ok on the first poll.
  await new Promise(r=>setTimeout(r, interval));
  while(Date.now()<deadline){
    try{
      const r=await fetch(new URL('health', document.baseURI||location.href).href,{cache:'no-store'});
      if(r.ok){
        let data={};
        try{ data=await r.json(); }catch(_){}
        if(data && data.status==='ok'){
          const nextServerIdentity=_healthResponseServerIdentity(data);
          if (baselineServerIdentity===null){
            location.reload();
            return;
          }
          if(
            nextServerIdentity===null &&
            (
              baselineServerIdentity.serverStartedAt!==null ||
              baselineServerIdentity.uptimeSeconds!==null
            )
          ){
            // If the replacement server comes back healthy without either
            // identity field after the baseline exposed a comparable identity,
            // treat that healthy response as the new server instead of timing
            // out on an uncomparable identity shape.
            location.reload();
            return;
          }
          if(
            nextServerIdentity!==null &&
            baselineServerIdentity.serverStartedAt!==null &&
            nextServerIdentity.serverStartedAt===null &&
            nextServerIdentity.uptimeSeconds!==null
          ){
            // If the baseline exposed server_started_at but the replacement
            // health response degrades to uptime-only, there is no longer a
            // comparable started_at field. Treat the first healthy uptime-only
            // response as the new server instead of timing out.
            location.reload();
            return;
          }
          if(
            nextServerIdentity!==null&&(
              (baselineServerIdentity.serverStartedAt===null&&nextServerIdentity.serverStartedAt!==null)||
              (baselineServerIdentity.serverStartedAt!==null&&nextServerIdentity.serverStartedAt!==null&&nextServerIdentity.serverStartedAt!==baselineServerIdentity.serverStartedAt)||
              (baselineServerIdentity.uptimeSeconds!==null&&nextServerIdentity.uptimeSeconds!==null&&nextServerIdentity.uptimeSeconds<baselineServerIdentity.uptimeSeconds)
            )
          ){
            location.reload();
            return;
          }
          if(
            _restartOutageObserved() &&
            nextServerIdentity!==null &&
            baselineServerIdentity.serverStartedAt===null &&
            nextServerIdentity.serverStartedAt===null &&
            baselineServerIdentity.uptimeSeconds!==null &&
            nextServerIdentity.uptimeSeconds!==null
          ){
            // Uptime-only on both sides AND we saw a sustained restart outage
            // (>=2 consecutive failed/non-OK probes) before this healthy response:
            // treat that outage as the restart, so reload even though the
            // replacement uptime is not strictly lower than a very-low baseline.
            location.reload();
            return;
          }
          // Healthy response still describing the pre-restart process: this is the
          // OLD server answering, so any earlier outage was a transient blip, not a
          // restart — reset the outage evidence so it can't accumulate into a false
          // positive across unrelated blips.
          _consecutiveOutages=0;
          // Keep polling while /health still describes the pre-restart process.
        }else{
          // Reachable but not status:ok (still starting up) — counts as outage.
          _consecutiveOutages++;
        }
      }else{
        // Non-OK HTTP (e.g. reverse-proxy 502/503 during restart) — outage evidence.
        _consecutiveOutages++;
      }
    }catch(_){ _consecutiveOutages++; /* socket closed during restart — retry */ }
    await new Promise(r=>setTimeout(r, interval));
  }
  if(msgEl) msgEl.textContent='⚠️ Server is taking longer than expected — click Reload when ready';
}

function _pendingCurrentTailUserMessage(messages){
  const list=Array.isArray(messages)?messages:[];
  for(let i=list.length-1;i>=0;i--){
    const msg=list[i];
    if(!msg) continue;
    if(String(msg.role||'')==='user'){
      // Compaction rows are synthetic user-role markers, not submitted turns.
      if(typeof _isContextCompactionMessage==='function'&&_isContextCompactionMessage(msg)) continue;
      return msg;
    }
    if(msg._live||String(msg.role||'')==='tool') continue;
    return null;
  }
  return null;
}

function getPendingSessionMessage(session, messagesOverride=null){
  const text=String(session?.pending_user_message||'').trim();
  if(!text) return null;
  const attachments=Array.isArray(session?.pending_attachments)?session.pending_attachments.filter(Boolean):[];
  const sourceMessages=Array.isArray(messagesOverride)?messagesOverride:session?.messages;
  const messages=Array.isArray(sourceMessages)?sourceMessages:[];
  const currentTailUser=_pendingCurrentTailUserMessage(messages);
  if(currentTailUser){
    const pendingCandidate={role:'user',content:text};
    const sameCurrentTurn=typeof _sameTranscriptMessage==='function'
      ? _sameTranscriptMessage(currentTailUser,pendingCandidate)
      : String(msgContent(currentTailUser)||'').trim()===text;
    if(sameCurrentTurn){
      if(attachments.length&&!currentTailUser.attachments?.length) currentTailUser.attachments=attachments;
      return null;
    }
  }
  return {
    role:'user',
    content:text,
    attachments:attachments.length?attachments:undefined,
    _ts:session?.pending_started_at||Date.now()/1000,
    _pending:true,
    _source:session?.pending_user_source||undefined,
  };
}
async function checkInflightOnBoot(sid) {
  const raw = localStorage.getItem(INFLIGHT_KEY);
  if (!raw) return;
  try {
    const {sid: inflightSid, streamId, ts} = JSON.parse(raw);
    if (inflightSid !== sid) { clearInflight(); return; }
    if (S.activeStreamId && S.activeStreamId === streamId) return;
    // Only show banner if the in-flight entry is less than 10 minutes old
    if (Date.now() - ts > 10 * 60 * 1000) { clearInflight(); return; }
    // Check if stream is still active
    const status = await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId || '')}`);
    if (status.active) {
      // Stream is genuinely still running -- show the banner
      showReconnectBanner(t('reconnect_active'));
    } else {
      // Stream finished. Only show banner if reload happened within 90 seconds
      // (longer gap = normal completed session, not a mid-stream reload)
      if (Date.now() - ts < 90 * 1000) {
        showReconnectBanner(t('reconnect_finished'));
      } else {
        clearInflight();  // completed normally, no banner needed
      }
    }
  } catch(e) { clearInflight(); }
}

function _topbarLoadedMessageCount(){
  const messages=Array.isArray(S.messages)?S.messages:[];
  return messages.filter(m=>m&&m.role&&m.role!=='tool').length;
}
function _topbarMessageMetaText(){
  const loadedCount=_topbarLoadedMessageCount();
  const totalCount=Number(S.session&&S.session.message_count);
  const hasTotal=Number.isFinite(totalCount)&&totalCount>0;
  const isTruncated=!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  if(isTruncated&&hasTotal&&totalCount>loadedCount){
    return `${loadedCount} loaded of ${totalCount} messages`;
  }
  // Fully loaded: use the tool-row-filtered loadedCount, NOT the raw server
  // total (api/routes.py sets message_count to len(_all_msgs), which counts
  // role:"tool" rows the topbar has always excluded). Only the truncated
  // branch above surfaces the raw server total, and only as "loaded of total".
  return t('n_messages',loadedCount);
}


export {
  _systemHealthPercent,
  _formatSystemHealthPercent,
  _formatSystemHealthBytes,
  _updateSystemHealthMetric,
  setSystemHealthUnavailable,
  renderSystemHealth,
  _systemHealthPanelIsVisible,
  startSystemHealthMonitor,
  stopSystemHealthMonitor,
  _syncSystemHealthMonitorVisibility,
  _agentHealthDismissed,
  _setAgentHealthDismissed,
  _hideAgentHealthAlert,
  _showAgentHealthAlert,
  dismissAgentHealthAlert,
  startAgentHealthMonitor,
  stopAgentHealthMonitor,
  _syncAgentHealthMonitorVisibility,
  _formatUpdateTargetStatus,
  _formatManualUpdateInstruction,
  _formatUpdateCheckError,
  _isSafeUpdateCompareUrl,
  _updateCompareUrl,
  _updateWhatsNewTargets,
  _appendUpdateDiffLinks,
  _hideUpdateSummaryPanel,
  _syncUpdateSummaryExpandButton,
  toggleUpdateSummaryExpanded,
  _summaryStorageByteLength,
  _summaryCacheEntriesSortedByRecency,
  _loadStoredUpdateSummaries,
  _persistGeneratedSummaries,
  _pruneGeneratedSummaries,
  _updateSummarySignature,
  _updateSummaryButtonLabel,
  _rememberGeneratedSummary,
  _renderUpdateSummaryPanel,
  _renderUpdateWhatsNewLinks,
  _showUpdateBanner,
  _i18nUpdateText,
  dismissUpdate,
  _isUpdateApplyNetworkError,
  _formatUpdateApplyExceptionMessage,
  _showUpdateError,
  _renderLockManualInstruction,
  _normalizeHealthServerIdentity,
  _healthResponseServerIdentity,
  _pendingCurrentTailUserMessage,
  getPendingSessionMessage,
  _topbarLoadedMessageCount,
  _topbarMessageMetaText,
  pollSystemHealth,
  restartGatewayService,
  pollAgentHealth,
  refreshSession,
  showWhatsNewSummary,
  applyUpdates,
  applyClearUpdateLock,
  _readHealthServerIdentity,
  forceUpdate,
  _waitForServerThenReload,
  checkInflightOnBoot,
  SYSTEM_HEALTH_INTERVAL_MS,
  AGENT_HEALTH_INTERVAL_MS,
  AGENT_HEALTH_DISMISSED_KEY,
  WHATS_NEW_SUMMARY_STORAGE_KEY,
  WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES,
  _systemHealthTimer,
  _agentHealthTimer,
  _agentHealthLastState,
  _lastGatewayRestartTime,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _systemHealthPercent: { enumerable: true, get: () => _systemHealthPercent, set: (value) => { _systemHealthPercent = value; } },
  _formatSystemHealthPercent: { enumerable: true, get: () => _formatSystemHealthPercent, set: (value) => { _formatSystemHealthPercent = value; } },
  _formatSystemHealthBytes: { enumerable: true, get: () => _formatSystemHealthBytes, set: (value) => { _formatSystemHealthBytes = value; } },
  _updateSystemHealthMetric: { enumerable: true, get: () => _updateSystemHealthMetric, set: (value) => { _updateSystemHealthMetric = value; } },
  setSystemHealthUnavailable: { enumerable: true, get: () => setSystemHealthUnavailable, set: (value) => { setSystemHealthUnavailable = value; } },
  renderSystemHealth: { enumerable: true, get: () => renderSystemHealth, set: (value) => { renderSystemHealth = value; } },
  _systemHealthPanelIsVisible: { enumerable: true, get: () => _systemHealthPanelIsVisible, set: (value) => { _systemHealthPanelIsVisible = value; } },
  startSystemHealthMonitor: { enumerable: true, get: () => startSystemHealthMonitor, set: (value) => { startSystemHealthMonitor = value; } },
  stopSystemHealthMonitor: { enumerable: true, get: () => stopSystemHealthMonitor, set: (value) => { stopSystemHealthMonitor = value; } },
  _syncSystemHealthMonitorVisibility: { enumerable: true, get: () => _syncSystemHealthMonitorVisibility, set: (value) => { _syncSystemHealthMonitorVisibility = value; } },
  _agentHealthDismissed: { enumerable: true, get: () => _agentHealthDismissed, set: (value) => { _agentHealthDismissed = value; } },
  _setAgentHealthDismissed: { enumerable: true, get: () => _setAgentHealthDismissed, set: (value) => { _setAgentHealthDismissed = value; } },
  _hideAgentHealthAlert: { enumerable: true, get: () => _hideAgentHealthAlert, set: (value) => { _hideAgentHealthAlert = value; } },
  _showAgentHealthAlert: { enumerable: true, get: () => _showAgentHealthAlert, set: (value) => { _showAgentHealthAlert = value; } },
  dismissAgentHealthAlert: { enumerable: true, get: () => dismissAgentHealthAlert, set: (value) => { dismissAgentHealthAlert = value; } },
  startAgentHealthMonitor: { enumerable: true, get: () => startAgentHealthMonitor, set: (value) => { startAgentHealthMonitor = value; } },
  stopAgentHealthMonitor: { enumerable: true, get: () => stopAgentHealthMonitor, set: (value) => { stopAgentHealthMonitor = value; } },
  _syncAgentHealthMonitorVisibility: { enumerable: true, get: () => _syncAgentHealthMonitorVisibility, set: (value) => { _syncAgentHealthMonitorVisibility = value; } },
  _formatUpdateTargetStatus: { enumerable: true, get: () => _formatUpdateTargetStatus, set: (value) => { _formatUpdateTargetStatus = value; } },
  _formatManualUpdateInstruction: { enumerable: true, get: () => _formatManualUpdateInstruction, set: (value) => { _formatManualUpdateInstruction = value; } },
  _formatUpdateCheckError: { enumerable: true, get: () => _formatUpdateCheckError, set: (value) => { _formatUpdateCheckError = value; } },
  _isSafeUpdateCompareUrl: { enumerable: true, get: () => _isSafeUpdateCompareUrl, set: (value) => { _isSafeUpdateCompareUrl = value; } },
  _updateCompareUrl: { enumerable: true, get: () => _updateCompareUrl, set: (value) => { _updateCompareUrl = value; } },
  _updateWhatsNewTargets: { enumerable: true, get: () => _updateWhatsNewTargets, set: (value) => { _updateWhatsNewTargets = value; } },
  _appendUpdateDiffLinks: { enumerable: true, get: () => _appendUpdateDiffLinks, set: (value) => { _appendUpdateDiffLinks = value; } },
  _hideUpdateSummaryPanel: { enumerable: true, get: () => _hideUpdateSummaryPanel, set: (value) => { _hideUpdateSummaryPanel = value; } },
  _syncUpdateSummaryExpandButton: { enumerable: true, get: () => _syncUpdateSummaryExpandButton, set: (value) => { _syncUpdateSummaryExpandButton = value; } },
  toggleUpdateSummaryExpanded: { enumerable: true, get: () => toggleUpdateSummaryExpanded, set: (value) => { toggleUpdateSummaryExpanded = value; } },
  _summaryStorageByteLength: { enumerable: true, get: () => _summaryStorageByteLength, set: (value) => { _summaryStorageByteLength = value; } },
  _summaryCacheEntriesSortedByRecency: { enumerable: true, get: () => _summaryCacheEntriesSortedByRecency, set: (value) => { _summaryCacheEntriesSortedByRecency = value; } },
  _loadStoredUpdateSummaries: { enumerable: true, get: () => _loadStoredUpdateSummaries, set: (value) => { _loadStoredUpdateSummaries = value; } },
  _persistGeneratedSummaries: { enumerable: true, get: () => _persistGeneratedSummaries, set: (value) => { _persistGeneratedSummaries = value; } },
  _pruneGeneratedSummaries: { enumerable: true, get: () => _pruneGeneratedSummaries, set: (value) => { _pruneGeneratedSummaries = value; } },
  _updateSummarySignature: { enumerable: true, get: () => _updateSummarySignature, set: (value) => { _updateSummarySignature = value; } },
  _updateSummaryButtonLabel: { enumerable: true, get: () => _updateSummaryButtonLabel, set: (value) => { _updateSummaryButtonLabel = value; } },
  _rememberGeneratedSummary: { enumerable: true, get: () => _rememberGeneratedSummary, set: (value) => { _rememberGeneratedSummary = value; } },
  _renderUpdateSummaryPanel: { enumerable: true, get: () => _renderUpdateSummaryPanel, set: (value) => { _renderUpdateSummaryPanel = value; } },
  _renderUpdateWhatsNewLinks: { enumerable: true, get: () => _renderUpdateWhatsNewLinks, set: (value) => { _renderUpdateWhatsNewLinks = value; } },
  _showUpdateBanner: { enumerable: true, get: () => _showUpdateBanner, set: (value) => { _showUpdateBanner = value; } },
  _i18nUpdateText: { enumerable: true, get: () => _i18nUpdateText, set: (value) => { _i18nUpdateText = value; } },
  dismissUpdate: { enumerable: true, get: () => dismissUpdate, set: (value) => { dismissUpdate = value; } },
  _isUpdateApplyNetworkError: { enumerable: true, get: () => _isUpdateApplyNetworkError, set: (value) => { _isUpdateApplyNetworkError = value; } },
  _formatUpdateApplyExceptionMessage: { enumerable: true, get: () => _formatUpdateApplyExceptionMessage, set: (value) => { _formatUpdateApplyExceptionMessage = value; } },
  _showUpdateError: { enumerable: true, get: () => _showUpdateError, set: (value) => { _showUpdateError = value; } },
  _renderLockManualInstruction: { enumerable: true, get: () => _renderLockManualInstruction, set: (value) => { _renderLockManualInstruction = value; } },
  _normalizeHealthServerIdentity: { enumerable: true, get: () => _normalizeHealthServerIdentity, set: (value) => { _normalizeHealthServerIdentity = value; } },
  _healthResponseServerIdentity: { enumerable: true, get: () => _healthResponseServerIdentity, set: (value) => { _healthResponseServerIdentity = value; } },
  _pendingCurrentTailUserMessage: { enumerable: true, get: () => _pendingCurrentTailUserMessage, set: (value) => { _pendingCurrentTailUserMessage = value; } },
  getPendingSessionMessage: { enumerable: true, get: () => getPendingSessionMessage, set: (value) => { getPendingSessionMessage = value; } },
  _topbarLoadedMessageCount: { enumerable: true, get: () => _topbarLoadedMessageCount, set: (value) => { _topbarLoadedMessageCount = value; } },
  _topbarMessageMetaText: { enumerable: true, get: () => _topbarMessageMetaText, set: (value) => { _topbarMessageMetaText = value; } },
  pollSystemHealth: { enumerable: true, get: () => pollSystemHealth, set: (value) => { pollSystemHealth = value; } },
  restartGatewayService: { enumerable: true, get: () => restartGatewayService, set: (value) => { restartGatewayService = value; } },
  pollAgentHealth: { enumerable: true, get: () => pollAgentHealth, set: (value) => { pollAgentHealth = value; } },
  refreshSession: { enumerable: true, get: () => refreshSession, set: (value) => { refreshSession = value; } },
  showWhatsNewSummary: { enumerable: true, get: () => showWhatsNewSummary, set: (value) => { showWhatsNewSummary = value; } },
  applyUpdates: { enumerable: true, get: () => applyUpdates, set: (value) => { applyUpdates = value; } },
  applyClearUpdateLock: { enumerable: true, get: () => applyClearUpdateLock, set: (value) => { applyClearUpdateLock = value; } },
  _readHealthServerIdentity: { enumerable: true, get: () => _readHealthServerIdentity, set: (value) => { _readHealthServerIdentity = value; } },
  forceUpdate: { enumerable: true, get: () => forceUpdate, set: (value) => { forceUpdate = value; } },
  _waitForServerThenReload: { enumerable: true, get: () => _waitForServerThenReload, set: (value) => { _waitForServerThenReload = value; } },
  checkInflightOnBoot: { enumerable: true, get: () => checkInflightOnBoot, set: (value) => { checkInflightOnBoot = value; } },
  SYSTEM_HEALTH_INTERVAL_MS: { enumerable: true, get: () => SYSTEM_HEALTH_INTERVAL_MS },
  AGENT_HEALTH_INTERVAL_MS: { enumerable: true, get: () => AGENT_HEALTH_INTERVAL_MS },
  AGENT_HEALTH_DISMISSED_KEY: { enumerable: true, get: () => AGENT_HEALTH_DISMISSED_KEY },
  WHATS_NEW_SUMMARY_STORAGE_KEY: { enumerable: true, get: () => WHATS_NEW_SUMMARY_STORAGE_KEY },
  WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES: { enumerable: true, get: () => WHATS_NEW_SUMMARY_STORAGE_MAX_BYTES },
  _systemHealthTimer: { enumerable: true, get: () => _systemHealthTimer, set: (value) => { _systemHealthTimer = value; } },
  _agentHealthTimer: { enumerable: true, get: () => _agentHealthTimer, set: (value) => { _agentHealthTimer = value; } },
  _agentHealthLastState: { enumerable: true, get: () => _agentHealthLastState, set: (value) => { _agentHealthLastState = value; } },
  _lastGatewayRestartTime: { enumerable: true, get: () => _lastGatewayRestartTime, set: (value) => { _lastGatewayRestartTime = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
