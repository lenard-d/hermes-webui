import { $ } from './state.js';
import { _renderUpdateWhatsNewLinks } from './update-summary.js';

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
  return detail?`${label}: ${detail}`:label;
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

function dismissUpdate(){
  const banner=$('updateBanner');
  if(banner) banner.classList.remove('visible');
  sessionStorage.setItem('hermes-update-dismissed','1');
}

export {
  _formatManualUpdateInstruction,
  _formatUpdateCheckError,
  _formatUpdateTargetStatus,
  _showUpdateBanner,
  dismissUpdate,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _formatUpdateTargetStatus:{enumerable:true,get:()=>_formatUpdateTargetStatus,set:(value)=>{_formatUpdateTargetStatus=value;}},
  _formatManualUpdateInstruction:{enumerable:true,get:()=>_formatManualUpdateInstruction,set:(value)=>{_formatManualUpdateInstruction=value;}},
  _formatUpdateCheckError:{enumerable:true,get:()=>_formatUpdateCheckError,set:(value)=>{_formatUpdateCheckError=value;}},
  _showUpdateBanner:{enumerable:true,get:()=>_showUpdateBanner,set:(value)=>{_showUpdateBanner=value;}},
  dismissUpdate:{enumerable:true,get:()=>dismissUpdate,set:(value)=>{dismissUpdate=value;}},
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
