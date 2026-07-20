import { state } from "./state.js";
import { loadCrons } from "./cron-list.js";

// Panels domain: cron completion and background alerts

// ── Cron completion alerts ────────────────────────────────────────────────────

export const _cronNewJobIds=new Set();  // track which job IDs had new completions (unread)

export function _resetCronUnreadForProfileSwitch(){
  state._cronPollGeneration++;
  _cronNewJobIds.clear();
  state._cronPollSince=Date.now()/1000;
  // Clear persisted cron sidebar markers from the profile we left. Non-cron
  // completion unread stays intact (#5960 gate: sticky all-profile leak).
  if(typeof _clearCronSessionCompletionUnreadForInactiveProfiles==='function'){
    const activeProfile=(typeof S!=='undefined'&&S&&S.activeProfile)||'default';
    _clearCronSessionCompletionUnreadForInactiveProfiles(activeProfile);
  }
  updateCronBadge();
}

// Auto-refresh the cron list when a job is created from chat or any external source.
// The chat path dispatches this event when the agent response mentions cron creation.
window.addEventListener('hermes:cron_created', () => {
  if ($('cronList')) loadCrons();
});

export function startCronPolling(){
  if(state._cronPollTimer) return;
  state._cronPollTimer=setInterval(async()=>{
    if(document.hidden) return;  // don't poll when tab is in background
    try{
      const pollGeneration=state._cronPollGeneration;
      const data=await api(`/api/crons/recent?since=${state._cronPollSince}`);
      if(pollGeneration!==state._cronPollGeneration) return;
      if(data.completions&&data.completions.length>0){
        for(const c of data.completions){
          if(c.toast_notifications !== false){
            showToast(t('cron_completion_status', c.name, c.status==='error' ? t('status_failed') : t('status_completed')),4000);
          }
          state._cronPollSince=Math.max(state._cronPollSince,c.completed_at);
          if(c.job_id) _cronNewJobIds.add(String(c.job_id));
          if(c.session_id && typeof _markSessionCompletionUnreadIfBackground === 'function'){
            const activeProfile=(typeof S!=='undefined'&&S&&S.activeProfile)||'default';
            _markSessionCompletionUnreadIfBackground(c.session_id, c.message_count, {
              source:'cron',
              profile:activeProfile,
            });
          }
        }
        // state._cronUnreadCount is derived from _cronNewJobIds.size in updateCronBadge.
        updateCronBadge();
      }
    }catch(e){}
  },30000);
}

export function updateCronBadge(){
  const tab=document.querySelector('.nav-tab[data-panel="tasks"]');
  if(!tab) return;
  let badge=tab.querySelector('.cron-badge');
  state._cronUnreadCount=_cronNewJobIds.size;  // sync counter to set (source of truth)
  if(state._cronUnreadCount>0){
    if(!badge){
      badge=document.createElement('span');
      badge.className='cron-badge';
      tab.style.position='relative';
      tab.appendChild(badge);
    }
    badge.textContent=state._cronUnreadCount>9?'9+':state._cronUnreadCount;
    badge.style.display='';
  }else if(badge){
    badge.style.display='none';
  }
}

// Clear cron badge only when all unread jobs have been viewed (not on panel open)
export function _clearCronUnreadForJob(jobId){
  const id=String(jobId);
  if(_cronNewJobIds.has(id)){
    _cronNewJobIds.delete(id);
    updateCronBadge();  // re-derives state._cronUnreadCount from set size
  }
}

// Start polling on page load
startCronPolling();

// ── Background agent error tracking ──────────────────────────────────────────

const _backgroundErrors=[];  // {session_id, title, message, ts}

export function trackBackgroundError(sessionId, title, message){
  // Only track if user is NOT currently viewing this session
  if(S.session&&S.session.session_id===sessionId) return;
  _backgroundErrors.push({session_id:sessionId, title:title||t('untitled'), message, ts:Date.now()});
  showErrorBanner();
}

export function showErrorBanner(){
  let banner=$('bgErrorBanner');
  if(!banner){
    banner=document.createElement('div');
    banner.id='bgErrorBanner';
    banner.className='bg-error-banner';
    const msgs=document.querySelector('.messages');
    if(msgs) msgs.parentNode.insertBefore(banner,msgs);
    else document.body.appendChild(banner);
  }
  const latest=_backgroundErrors[0];  // FIFO: show oldest (first) error
  if(!latest){banner.style.display='none';return;}
  const count=_backgroundErrors.length;
  const msg=count>1?t('bg_error_multi',count):t('bg_error_single',latest.title);
  banner.innerHTML=`<span>\u26a0 ${esc(msg)}</span><div style="display:flex;gap:6px;flex-shrink:0"><button class="reconnect-btn" onclick="navigateToErrorSession()">${esc(t('view'))}</button><button class="reconnect-btn" onclick="dismissErrorBanner()">${esc(t('dismiss'))}</button></div>`;
  banner.style.display='';
}

export function navigateToErrorSession(){
  const latest=_backgroundErrors.shift();  // FIFO: show oldest error first
  if(latest){
    loadSession(latest.session_id);renderSessionList();
  }
  if(_backgroundErrors.length===0) dismissErrorBanner();
  else showErrorBanner();
}

export function dismissErrorBanner(){
  _backgroundErrors.length=0;
  const banner=$('bgErrorBanner');
  if(banner) banner.style.display='none';
}
