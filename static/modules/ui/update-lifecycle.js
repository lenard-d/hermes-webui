import { showToast } from './composer.js';
import { showConfirmDialog } from './app-dialogs.js';
import { $ } from './state.js';

function _i18nUpdateText(key,fallback){
  if(typeof t==='function'){
    const val=t(key);
    if(val&&val!==key) return val;
  }
  return fallback;
}

function _isUpdateApplyNetworkError(error){
  if(error&&error.status) return false;
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
  if(btn){btn.disabled=true;btn.textContent=updateText('update_updating','Updating…');}
  const errEl=$('updateError');
  if(errEl){errEl.style.display='none';errEl.textContent='';}
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
    const baselineServerIdentity=await _readHealthServerIdentity();
    for(const target of targets){
      const applyBody={target};
      const channel=window._updateData?.[target]?.channel;
      if(channel==='stable'||channel==='experimental') applyBody.channel=channel;
      const res=await api('/api/updates/apply',{method:'POST',body:JSON.stringify(applyBody),timeoutMs:120000});
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
  if(errEl){errEl.textContent=msg;errEl.style.display='block';}
  else showToast(msg);
  if(forceBtn&&(res.conflict||res.diverged)){
    forceBtn.dataset.target=target;
    forceBtn.style.display='inline-block';
  }
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
    }else if(res.lock_held){
      _renderLockManualInstruction(target,res);
    }else{
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

function _renderLockManualInstruction(target,res){
  const cmd=res.manual_command||('rm -f '+(res.well_known_lock_path||'.git/index.lock'));
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
        setTimeout(()=>{copyBtn.textContent='Copy command';},1500);
      }else{
        copyBtn.textContent='Clipboard unavailable';
      }
    }catch(_){
      copyBtn.textContent='Copy failed';
    }
  };
  actions.appendChild(copyBtn);
  const retryBtn=document.createElement('button');
  retryBtn.type='button';
  retryBtn.className='update-btn update-primary';
  retryBtn.textContent="I've removed the lock — retry update";
  retryBtn.dataset.target=target;
  retryBtn.onclick=()=>{applyClearUpdateLock(retryBtn);};
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
    return value?value:null;
  }
  const numeric=Number(rawIdentity);
  return Number.isFinite(numeric)?String(numeric):null;
}

function _healthResponseServerIdentity(data){
  if(!data||typeof data!=='object') return null;
  const serverStartedAt=_normalizeHealthServerIdentity(data.server_started_at);
  const hasUptimeSeconds=data.uptime_seconds!==null&&data.uptime_seconds!==undefined;
  const uptimeSeconds=hasUptimeSeconds?Number(data.uptime_seconds):NaN;
  const normalizedUptime=Number.isFinite(uptimeSeconds)&&uptimeSeconds>=0?uptimeSeconds:null;
  if(serverStartedAt===null&&normalizedUptime===null) return null;
  return {serverStartedAt,uptimeSeconds:normalizedUptime};
}

async function _readHealthServerIdentity(){
  try{
    const response=await fetch(new URL('health',document.baseURI||location.href).href,{cache:'no-store'});
    if(!response.ok) return null;
    return _healthResponseServerIdentity(await response.json());
  }catch(_){
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
  btn.disabled=true;
  btn.textContent='Force updating…';
  const errEl=$('updateError');
  if(errEl) errEl.style.display='none';
  try{
    const baselineServerIdentity=await _readHealthServerIdentity();
    const body={target};
    const channel=window._updateData?.[target]?.channel;
    if(channel==='stable'||channel==='experimental') body.channel=channel;
    const res=await api('/api/updates/force',{method:'POST',body:JSON.stringify(body),timeoutMs:120000});
    if(!res.ok){
      if(errEl){errEl.textContent='Force update failed: '+(res.message||'unknown error');errEl.style.display='block';}
      btn.disabled=false;
      btn.textContent='Force update';
      return;
    }
    showToast('Force update applied — restarting…');
    sessionStorage.removeItem('hermes-update-checked');
    sessionStorage.removeItem('hermes-update-dismissed');
    _waitForServerThenReload({baselineServerIdentity});
  }catch(e){
    if(errEl){errEl.textContent='Force update failed: '+e.message;errEl.style.display='block';}
    btn.disabled=false;
    btn.textContent='Force update';
  }
}

async function _waitForServerThenReload(opts){
  opts=opts||{};
  const interval=opts.interval||500;
  const maxMs=opts.maxMs||15000;
  const baselineServerIdentity=(()=>{
    const rawIdentity=opts.baselineServerIdentity;
    if(!rawIdentity||typeof rawIdentity!=='object'){
      const normalizedServerStartedAt=_normalizeHealthServerIdentity(rawIdentity);
      return normalizedServerStartedAt===null?null:{serverStartedAt:normalizedServerStartedAt,uptimeSeconds:null};
    }
    const normalizedIdentity={
      serverStartedAt:_normalizeHealthServerIdentity(rawIdentity.serverStartedAt),
      uptimeSeconds:Number.isFinite(Number(rawIdentity.uptimeSeconds))&&Number(rawIdentity.uptimeSeconds)>=0?Number(rawIdentity.uptimeSeconds):null,
    };
    return normalizedIdentity.serverStartedAt===null&&normalizedIdentity.uptimeSeconds===null?null:normalizedIdentity;
  })();
  window._restartingForUpdate=true;
  const msgEl=$('reconnectMsg');
  const banner=$('reconnectBanner');
  if(msgEl) msgEl.textContent='⏳ Restarting… please wait';
  if(banner) banner.classList.add('visible');
  const deadline=Date.now()+maxMs;
  let _consecutiveOutages=0;
  const _restartOutageObserved=()=>_consecutiveOutages>=2;
  await new Promise(r=>setTimeout(r,interval));
  while(Date.now()<deadline){
    try{
      const r=await fetch(new URL('health',document.baseURI||location.href).href,{cache:'no-store'});
      if(r.ok){
        let data={};
        try{data=await r.json();}catch(_){}
        if(data && data.status==='ok'){
          const nextServerIdentity=_healthResponseServerIdentity(data);
          if(baselineServerIdentity===null){
            location.reload();
            return;
          }
          if(
            nextServerIdentity===null&&
            (baselineServerIdentity.serverStartedAt!==null||baselineServerIdentity.uptimeSeconds!==null)
          ){
            location.reload();
            return;
          }
          if(
            nextServerIdentity!==null&&
            baselineServerIdentity.serverStartedAt!==null&&
            nextServerIdentity.serverStartedAt===null&&
            nextServerIdentity.uptimeSeconds!==null
          ){
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
            _restartOutageObserved()&&
            nextServerIdentity!==null&&
            baselineServerIdentity.serverStartedAt===null&&
            nextServerIdentity.serverStartedAt===null&&
            baselineServerIdentity.uptimeSeconds!==null&&
            nextServerIdentity.uptimeSeconds!==null
          ){
            location.reload();
            return;
          }
          _consecutiveOutages=0;
        }else{
          _consecutiveOutages++;
        }
      }else{
        _consecutiveOutages++;
      }
    }catch(_){
      _consecutiveOutages++;
    }
    await new Promise(r=>setTimeout(r,interval));
  }
  if(msgEl) msgEl.textContent='⚠️ Server is taking longer than expected — click Reload when ready';
}

export {
  _formatUpdateApplyExceptionMessage,
  _healthResponseServerIdentity,
  _i18nUpdateText,
  _isUpdateApplyNetworkError,
  _normalizeHealthServerIdentity,
  _readHealthServerIdentity,
  _renderLockManualInstruction,
  _showUpdateError,
  _waitForServerThenReload,
  applyClearUpdateLock,
  applyUpdates,
  forceUpdate,
};

const compatibilityBindings={};
for(const [name,getter,setter] of [
  ['_i18nUpdateText',()=>_i18nUpdateText,(value)=>{_i18nUpdateText=value;}],
  ['_isUpdateApplyNetworkError',()=>_isUpdateApplyNetworkError,(value)=>{_isUpdateApplyNetworkError=value;}],
  ['_formatUpdateApplyExceptionMessage',()=>_formatUpdateApplyExceptionMessage,(value)=>{_formatUpdateApplyExceptionMessage=value;}],
  ['applyUpdates',()=>applyUpdates,(value)=>{applyUpdates=value;}],
  ['_showUpdateError',()=>_showUpdateError,(value)=>{_showUpdateError=value;}],
  ['applyClearUpdateLock',()=>applyClearUpdateLock,(value)=>{applyClearUpdateLock=value;}],
  ['_renderLockManualInstruction',()=>_renderLockManualInstruction,(value)=>{_renderLockManualInstruction=value;}],
  ['_normalizeHealthServerIdentity',()=>_normalizeHealthServerIdentity,(value)=>{_normalizeHealthServerIdentity=value;}],
  ['_healthResponseServerIdentity',()=>_healthResponseServerIdentity,(value)=>{_healthResponseServerIdentity=value;}],
  ['_readHealthServerIdentity',()=>_readHealthServerIdentity,(value)=>{_readHealthServerIdentity=value;}],
  ['forceUpdate',()=>forceUpdate,(value)=>{forceUpdate=value;}],
  ['_waitForServerThenReload',()=>_waitForServerThenReload,(value)=>{_waitForServerThenReload=value;}],
]){
  Object.defineProperty(compatibilityBindings,name,{enumerable:true,get:getter,set:setter});
}
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
