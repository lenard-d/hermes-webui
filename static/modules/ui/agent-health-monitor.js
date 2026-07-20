import { showToast } from './composer.js';
import { $ } from './state.js';

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
  const btn=$('btnRestartGateway');
  const dismissBtn=$('agentHealthDismiss');
  if(!btn) return;
  btn.disabled=true;
  if(dismissBtn) dismissBtn.disabled=true;
  const originalText=btn.textContent;
  btn.textContent='Restarting...';
  try{
    const res=await api('/api/health/restart',{method:'POST'});
    if(res&&res.ok){
      showToast('Gateway service restarted successfully');
      _hideAgentHealthAlert();
      _lastGatewayRestartTime=Date.now();
      setTimeout(pollAgentHealth,15000);
    }else{
      showToast(res&&res.error||'Failed to restart gateway service');
    }
  }catch(e){
    showToast('Failed to restart gateway service: '+e.message);
  }finally{
    btn.disabled=false;
    if(dismissBtn) dismissBtn.disabled=false;
    btn.textContent=originalText;
  }
}

async function pollAgentHealth(){
  if(document.visibilityState !== 'visible') return;
  if(Date.now()-_lastGatewayRestartTime<15000) return;
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
  if(document.visibilityState!=='visible'||_agentHealthTimer) return;
  void pollAgentHealth();
  _agentHealthTimer=setInterval(pollAgentHealth,AGENT_HEALTH_INTERVAL_MS);
}

function stopAgentHealthMonitor(){
  if(_agentHealthTimer){clearInterval(_agentHealthTimer);_agentHealthTimer=null;}
}

function _syncAgentHealthMonitorVisibility(){
  if(document.visibilityState==='visible') startAgentHealthMonitor();
  else stopAgentHealthMonitor();
}

document.addEventListener('visibilitychange',_syncAgentHealthMonitorVisibility);
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',startAgentHealthMonitor);
else startAgentHealthMonitor();

export {
  AGENT_HEALTH_DISMISSED_KEY,
  AGENT_HEALTH_INTERVAL_MS,
  _agentHealthDismissed,
  _agentHealthLastState,
  _agentHealthTimer,
  _hideAgentHealthAlert,
  _lastGatewayRestartTime,
  _setAgentHealthDismissed,
  _showAgentHealthAlert,
  _syncAgentHealthMonitorVisibility,
  dismissAgentHealthAlert,
  pollAgentHealth,
  restartGatewayService,
  startAgentHealthMonitor,
  stopAgentHealthMonitor,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _agentHealthDismissed:{enumerable:true,get:()=>_agentHealthDismissed,set:(value)=>{_agentHealthDismissed=value;}},
  _setAgentHealthDismissed:{enumerable:true,get:()=>_setAgentHealthDismissed,set:(value)=>{_setAgentHealthDismissed=value;}},
  _hideAgentHealthAlert:{enumerable:true,get:()=>_hideAgentHealthAlert,set:(value)=>{_hideAgentHealthAlert=value;}},
  _showAgentHealthAlert:{enumerable:true,get:()=>_showAgentHealthAlert,set:(value)=>{_showAgentHealthAlert=value;}},
  dismissAgentHealthAlert:{enumerable:true,get:()=>dismissAgentHealthAlert,set:(value)=>{dismissAgentHealthAlert=value;}},
  restartGatewayService:{enumerable:true,get:()=>restartGatewayService,set:(value)=>{restartGatewayService=value;}},
  pollAgentHealth:{enumerable:true,get:()=>pollAgentHealth,set:(value)=>{pollAgentHealth=value;}},
  startAgentHealthMonitor:{enumerable:true,get:()=>startAgentHealthMonitor,set:(value)=>{startAgentHealthMonitor=value;}},
  stopAgentHealthMonitor:{enumerable:true,get:()=>stopAgentHealthMonitor,set:(value)=>{stopAgentHealthMonitor=value;}},
  _syncAgentHealthMonitorVisibility:{enumerable:true,get:()=>_syncAgentHealthMonitorVisibility,set:(value)=>{_syncAgentHealthMonitorVisibility=value;}},
  AGENT_HEALTH_INTERVAL_MS:{enumerable:true,get:()=>AGENT_HEALTH_INTERVAL_MS},
  AGENT_HEALTH_DISMISSED_KEY:{enumerable:true,get:()=>AGENT_HEALTH_DISMISSED_KEY},
  _agentHealthTimer:{enumerable:true,get:()=>_agentHealthTimer,set:(value)=>{_agentHealthTimer=value;}},
  _agentHealthLastState:{enumerable:true,get:()=>_agentHealthLastState,set:(value)=>{_agentHealthLastState=value;}},
  _lastGatewayRestartTime:{enumerable:true,get:()=>_lastGatewayRestartTime,set:(value)=>{_lastGatewayRestartTime=value;}},
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
