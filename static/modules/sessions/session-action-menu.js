import { _sessionPrefersReducedMotion } from './sidebar-motion.js';
import { sidebarStateBindings } from './sidebar-store.js';

function _focusSessionActionMenuRestoreTarget(target){
  if(!target||!target.isConnected||typeof target.focus!=='function') return false;
  try{target.focus({preventScroll:true});}catch(_){target.focus();}
  return document.activeElement===target;
}

function closeSessionActionMenu({restoreFocus=false}={}){
  const focusTarget=restoreFocus?sidebarStateBindings._sessionActionAnchor:null;
  const fallbackFocusTarget=restoreFocus?sidebarStateBindings._sessionActionPreviousFocus:null;
  if(sidebarStateBindings._sessionActionMenu){
    sidebarStateBindings._sessionActionMenu.remove();
    sidebarStateBindings._sessionActionMenu = null;
  }
  if(sidebarStateBindings._sessionActionAnchor){
    if(sidebarStateBindings._sessionActionAnchor.classList&&sidebarStateBindings._sessionActionAnchor.classList.contains('session-actions-trigger')){
      sidebarStateBindings._sessionActionAnchor.classList.remove('active');
      sidebarStateBindings._sessionActionAnchor.setAttribute('aria-expanded','false');
      sidebarStateBindings._sessionActionAnchor.removeAttribute('aria-controls');
    }
    const row=sidebarStateBindings._sessionActionAnchor.closest('.session-item,.session-child-session');
    if(row) row.classList.remove('menu-open','long-pressing');
    sidebarStateBindings._sessionActionAnchor = null;
  }
  sidebarStateBindings._sessionActionSessionId = null;
  sidebarStateBindings._sessionActionPreviousFocus = null;
  if(!_focusSessionActionMenuRestoreTarget(focusTarget)) _focusSessionActionMenuRestoreTarget(fallbackFocusTarget);
}

function _sessionActionMenuShouldIgnoreScrollTarget(target){
  if(!target || typeof target.closest !== 'function') return false;
  // #5347: active-chat auto-scroll / manual wheel must not dismiss the sidebar menu.
  return Boolean(target.closest('#messages, #msgInner, .messages-inner'));
}

function _sessionActionMenuShouldRepositionOnScroll(target){
  if(!target || typeof target.closest !== 'function') return false;
  return Boolean(target.closest('#sessionList, .session-list'));
}

function _positionSessionActionMenu(anchorEl){
  if(!sidebarStateBindings._sessionActionMenu || !anchorEl) return;
  const rect=anchorEl.getBoundingClientRect();
  const menuW=Math.min(280, Math.max(220, sidebarStateBindings._sessionActionMenu.scrollWidth || 220));
  let left=rect.right-menuW;
  if(left<8) left=8;
  if(left+menuW>window.innerWidth-8) left=window.innerWidth-menuW-8;
  sidebarStateBindings._sessionActionMenu.style.left=left+'px';
  sidebarStateBindings._sessionActionMenu.style.top='8px';
  sidebarStateBindings._sessionActionMenu.style.maxHeight='';
  const menuH=sidebarStateBindings._sessionActionMenu.offsetHeight || 0;
  const margin=8;
  const maxAvail=window.innerHeight-margin*2;
  let top=rect.bottom+6;
  if(top+menuH>window.innerHeight-margin && rect.top>menuH+12){
    top=rect.top-menuH-6;
  }
  if(menuH>maxAvail){
    sidebarStateBindings._sessionActionMenu.style.maxHeight=maxAvail+'px';
    top=margin;
  } else {
    if(top+menuH>window.innerHeight-margin) top=window.innerHeight-margin-menuH;
    if(top<margin) top=margin;
  }
  sidebarStateBindings._sessionActionMenu.style.top=top+'px';
}

function _buildSessionAction(label, meta, icon, onSelect, extraClass=''){
  const opt=document.createElement('button');
  opt.type='button';
  opt.className='ws-opt session-action-opt'+(extraClass?` ${extraClass}`:'');
  opt.setAttribute('role','menuitem');
  if(meta) opt.title=meta;
  opt.innerHTML=
    `<span class="ws-opt-action">`
      + `<span class="ws-opt-icon">${icon}</span>`
      + `<span class="session-action-copy">`
        + `<span class="ws-opt-name">${esc(label)}</span>`
      + `</span>`
    + `</span>`;
  opt.onclick=async(e)=>{
    e.preventDefault();
    e.stopPropagation();
    await onSelect();
  };
  return opt;
}

function _playSessionActionMenuEntrance(menu){
  if(!menu || _sessionPrefersReducedMotion()) return;
  if(typeof menu.animate==='function'){
    try{
      const anim=menu.animate(
        [
          {opacity:0, transform:'translate3d(0,-4px,0) scale(.985)'},
          {opacity:1, transform:'translate3d(0,0,0) scale(1)'}
        ],
        {duration:450, easing:'cubic-bezier(.2,.8,.2,1)'}
      );
      if(anim&&anim.finished) anim.finished.catch(()=>{});
      return;
    }catch(_){}
  }
  menu.classList.add('open-animated');
}

function _mountSessionActionMenu(menu, session, anchorEl){
  sidebarStateBindings._sessionActionPreviousFocus=document.activeElement;
  document.body.appendChild(menu);
  sidebarStateBindings._sessionActionMenu = menu;
  sidebarStateBindings._sessionActionAnchor = anchorEl;
  sidebarStateBindings._sessionActionSessionId = session.session_id;
  if(anchorEl.classList&&anchorEl.classList.contains('session-actions-trigger')){
    anchorEl.classList.add('active');
    anchorEl.setAttribute('aria-expanded','true');
    anchorEl.setAttribute('aria-controls',menu.id);
  }
  const row=anchorEl.closest('.session-item,.session-child-session');
  if(row) row.classList.add('menu-open');
  _positionSessionActionMenu(anchorEl);
  _playSessionActionMenuEntrance(menu);
  const menuItems=()=>Array.from(menu.querySelectorAll('.session-action-opt:not([disabled])'));
  menu.addEventListener('keydown',e=>{
    const items=menuItems();
    if(e.key==='Escape'){
      e.preventDefault();
      e.stopPropagation();
      closeSessionActionMenu({restoreFocus:true});
      return;
    }
    if(!items.length) return;
    const currentIndex=Math.max(0,items.indexOf(document.activeElement));
    let nextIndex=null;
    if(e.key==='ArrowDown') nextIndex=(currentIndex+1)%items.length;
    else if(e.key==='ArrowUp') nextIndex=(currentIndex-1+items.length)%items.length;
    else if(e.key==='Home') nextIndex=0;
    else if(e.key==='End') nextIndex=items.length-1;
    if(nextIndex===null) return;
    e.preventDefault();
    try{items[nextIndex].focus({preventScroll:true});}catch(_){items[nextIndex].focus();}
  });
  const firstAction=menuItems()[0];
  if(firstAction){
    try{firstAction.focus({preventScroll:true});}catch(_){firstAction.focus();}
  }
}

document.addEventListener('click',e=>{
  if(!sidebarStateBindings._sessionActionMenu) return;
  if(sidebarStateBindings._sessionActionMenu.contains(e.target)) return;
  if(sidebarStateBindings._sessionActionAnchor && sidebarStateBindings._sessionActionAnchor.contains(e.target)) return;
  closeSessionActionMenu();
});
document.addEventListener('scroll',e=>{
  if(!sidebarStateBindings._sessionActionMenu) return;
  if(sidebarStateBindings._sessionActionMenu.contains(e.target)) return;
  if(_sessionActionMenuShouldIgnoreScrollTarget(e.target)) return;
  if(_sessionActionMenuShouldRepositionOnScroll(e.target) && sidebarStateBindings._sessionActionAnchor){
    if(!sidebarStateBindings._sessionActionAnchor.isConnected){
      closeSessionActionMenu();
      return;
    }
    _positionSessionActionMenu(sidebarStateBindings._sessionActionAnchor);
    return;
  }
  closeSessionActionMenu();
}, true);
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && sidebarStateBindings._sessionActionMenu) closeSessionActionMenu({restoreFocus:true});
});
window.addEventListener('resize',()=>{
  if(sidebarStateBindings._sessionActionMenu && sidebarStateBindings._sessionActionAnchor) _positionSessionActionMenu(sidebarStateBindings._sessionActionAnchor);
});

export { _buildSessionAction, _mountSessionActionMenu, closeSessionActionMenu };
