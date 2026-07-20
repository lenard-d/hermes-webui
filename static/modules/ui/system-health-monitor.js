import { $ } from './state.js';

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

function _systemHealthPanelIsVisible(){
  return document.visibilityState === 'visible' &&
    !!document.querySelector('main.main.showing-insights') &&
    !!$('systemHealthPanel');
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

function startSystemHealthMonitor(){
  if(!_systemHealthPanelIsVisible()||_systemHealthTimer) return;
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

export {
  SYSTEM_HEALTH_INTERVAL_MS,
  _formatSystemHealthBytes,
  _formatSystemHealthPercent,
  _syncSystemHealthMonitorVisibility,
  _systemHealthPanelIsVisible,
  _systemHealthPercent,
  _systemHealthTimer,
  _updateSystemHealthMetric,
  pollSystemHealth,
  renderSystemHealth,
  setSystemHealthUnavailable,
  startSystemHealthMonitor,
  stopSystemHealthMonitor,
};

const compatibilityBindings={};
Object.defineProperties(compatibilityBindings,{
  _systemHealthPercent:{enumerable:true,get:()=>_systemHealthPercent,set:(value)=>{_systemHealthPercent=value;}},
  _formatSystemHealthPercent:{enumerable:true,get:()=>_formatSystemHealthPercent,set:(value)=>{_formatSystemHealthPercent=value;}},
  _formatSystemHealthBytes:{enumerable:true,get:()=>_formatSystemHealthBytes,set:(value)=>{_formatSystemHealthBytes=value;}},
  _updateSystemHealthMetric:{enumerable:true,get:()=>_updateSystemHealthMetric,set:(value)=>{_updateSystemHealthMetric=value;}},
  setSystemHealthUnavailable:{enumerable:true,get:()=>setSystemHealthUnavailable,set:(value)=>{setSystemHealthUnavailable=value;}},
  renderSystemHealth:{enumerable:true,get:()=>renderSystemHealth,set:(value)=>{renderSystemHealth=value;}},
  _systemHealthPanelIsVisible:{enumerable:true,get:()=>_systemHealthPanelIsVisible,set:(value)=>{_systemHealthPanelIsVisible=value;}},
  pollSystemHealth:{enumerable:true,get:()=>pollSystemHealth,set:(value)=>{pollSystemHealth=value;}},
  startSystemHealthMonitor:{enumerable:true,get:()=>startSystemHealthMonitor,set:(value)=>{startSystemHealthMonitor=value;}},
  stopSystemHealthMonitor:{enumerable:true,get:()=>stopSystemHealthMonitor,set:(value)=>{stopSystemHealthMonitor=value;}},
  _syncSystemHealthMonitorVisibility:{enumerable:true,get:()=>_syncSystemHealthMonitorVisibility,set:(value)=>{_syncSystemHealthMonitorVisibility=value;}},
  SYSTEM_HEALTH_INTERVAL_MS:{enumerable:true,get:()=>SYSTEM_HEALTH_INTERVAL_MS},
  _systemHealthTimer:{enumerable:true,get:()=>_systemHealthTimer,set:(value)=>{_systemHealthTimer=value;}},
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
