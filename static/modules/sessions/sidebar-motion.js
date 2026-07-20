function _captureSessionReflowPositions(){
  const list=$('sessionList');
  if(!list) return null;
  const positions=new Map();
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    positions.set(row.dataset.sid,row.getBoundingClientRect().top);
  });
  return positions;
}
function _waitForSessionMotion(ms){
  return new Promise(resolve=>setTimeout(resolve,ms));
}

function _playSessionRowsReflowFromPositions(before, timeoutMs, prefersReducedMotion){
  if(!before||!before.size) return;
  if(prefersReducedMotion&&prefersReducedMotion()) return;
  const list=$('sessionList');
  if(!list) return;
  const movingRows=[];
  list.querySelectorAll('.session-item[data-sid]').forEach(row=>{
    const oldTop=before.get(row.dataset.sid);
    if(oldTop===undefined) return;
    const delta=oldTop-row.getBoundingClientRect().top;
    if(Math.abs(delta)<1) return;
    movingRows.push({row,delta});
  });
  if(!movingRows.length) return;
  movingRows.forEach(({row,delta})=>{
    row.style.transition='none';
    row.style.setProperty('--session-reflow-offset',delta+'px');
    row.classList.add('session-reflowing');
  });
  list.getBoundingClientRect();
  movingRows.forEach(({row})=>{
    let reflowCleared=false;
    const clearReflow=()=>{
      if(reflowCleared) return;
      reflowCleared=true;
      row.classList.remove('session-reflowing');
      row.style.removeProperty('--session-reflow-offset');
      row.removeEventListener('transitionend',onReflowEnd);
    };
    const onReflowEnd=(event)=>{
      if(event.propertyName==='transform') clearReflow();
    };
    row.addEventListener('transitionend',onReflowEnd);
    row.style.removeProperty('transition');
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(!reflowCleared) row.style.setProperty('--session-reflow-offset','0px');
    }));
    setTimeout(clearReflow,timeoutMs);
  });
}

function _sessionPrefersReducedMotion(){
  try{
    return Boolean(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }catch(_){
    return false;
  }
}

function _makeSessionSwipeAffordance(side, icon, label){
  const affordance=document.createElement('div');
  affordance.className='session-swipe-affordance session-swipe-affordance-'+side;
  affordance.setAttribute('aria-hidden','true');
  const stack=document.createElement('span');
  stack.className='session-swipe-action-stack';
  const badge=document.createElement('span');
  badge.className='session-swipe-badge';
  badge.innerHTML=li(icon,18);
  const text=document.createElement('span');
  text.className='session-swipe-label';
  text.textContent=label;
  stack.append(badge,text);
  affordance.append(stack);
  return affordance;
}
