import { _restoreWorklogDetailDisclosureState } from './activity-presentation.js';
import { _postProcessWithAnchorSuppression } from './content-postprocessing.js';
import { S } from './state.js';
import { _syncToolCallGroupSummary } from './worklog-tool-groups.js';
import { _anchorSceneRowsForRendering, _renderAnchorSceneRowsIntoWorklog } from './anchor-scene-presentation.js';

let _liveActivityUserExpanded;
const _activityDisclosureStoragePrefix='hermes-activity-disclosure:';
function _activityDisclosureStorageKey(activityKey){
  if(!activityKey||!S.session||!S.session.session_id) return null;
  return _activityDisclosureStoragePrefix+S.session.session_id+':'+activityKey;
}
function _readActivityDisclosureState(activityKey){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return null;
  try{
    const saved=localStorage.getItem(key);
    return saved==='open'||saved==='closed'?saved:null;
  }catch(_){return null;}
}
function _writeActivityDisclosureState(activityKey, open){
  const key=_activityDisclosureStorageKey(activityKey);
  if(!key) return;
  try{localStorage.setItem(key, open?'open':'closed');}catch(_){}
}
function _copyActivityDisclosureState(fromActivityKey, toActivityKey){
  const state=_readActivityDisclosureState(fromActivityKey);
  if(state) _writeActivityDisclosureState(toActivityKey, state==='open');
}
function _activityKeyForLiveTurn(){
  return S.activeStreamId?'live:'+S.activeStreamId:null;
}
function _onLiveActivityToggle(group){
  if(!group) return;
  // Only track explicit user clicks on the live group, not programmatic toggles.
  if(group.getAttribute('data-live-tool-call-group')!=='1') return;
  _liveActivityUserExpanded = !group.classList.contains('tool-call-group-collapsed');
}
function _materializeDeferredWorklogRows(group){
  // #5839: build the row DOM for a settled worklog whose rows were deferred at
  // render time (collapsed). Idempotent — clears the marker so it runs once.
  if(!group||group.getAttribute('data-worklog-rows-deferred')!=='1') return false;
  let rows=group._deferredWorklogRows;
  // The JS-property stash is dropped when the transcript is restored from the
  // HTML cache (innerHTML round-trip). Recover the rows from the owning message
  // via the disclosure key (anchor-scene:<rawIdx>) so a post-restore expand
  // still fills the worklog. (#5839)
  if((!rows||!rows.length)&&typeof _deferredWorklogRowsFromGroup==='function'){
    rows=_deferredWorklogRowsFromGroup(group);
  }
  group.removeAttribute('data-worklog-rows-deferred');
  group._deferredWorklogRows=null;
  if(!rows||!rows.length) return false;
  const ok=_renderAnchorSceneRowsIntoWorklog(group,rows,{settled:true});
  if(!ok) return false;
  // #5839 fix: the eager render path post-processes its rows (syntax highlight,
  // copy buttons, mermaid, katex, structured trees) and restores detail-disclosure
  // state; a lazily-materialized group must do the same or expanded rows render
  // un-enhanced and any captured open/scroll state is lost. Post-process on the
  // next frame (matching the eager rebuild paths), then re-apply the disclosure
  // state stashed with the group at defer time.
  const disclosure=group._deferredWorklogDisclosure;
  group._deferredWorklogDisclosure=null;
  if(typeof _postProcessWithAnchorSuppression==='function'
     && typeof requestAnimationFrame==='function'){
    requestAnimationFrame(()=>{
      _postProcessWithAnchorSuppression(group);
      if(disclosure&&disclosure.size&&typeof _restoreWorklogDetailDisclosureState==='function'){
        _restoreWorklogDetailDisclosureState(group, disclosure);
      }
    });
  }else if(disclosure&&disclosure.size&&typeof _restoreWorklogDetailDisclosureState==='function'){
    _restoreWorklogDetailDisclosureState(group, disclosure);
  }
  return true;
}
function _deferredWorklogRowsFromGroup(group){
  // Recover a settled worklog's rows from S.messages using the group's
  // disclosure key `anchor-scene:<rawIdx>`. Used after an HTML-cache restore
  // where the _deferredWorklogRows JS property was dropped. (#5839)
  const key=group&&group.getAttribute&&group.getAttribute('data-activity-disclosure-key');
  const m=key&&/^anchor-scene:(\d+)$/.exec(key);
  if(!m) return null;
  const msg=S.messages&&S.messages[Number(m[1])];
  const scene=msg&&msg._anchor_activity_scene;
  if(!scene) return null;
  return _anchorSceneRowsForRendering(scene,{settled:true});
}
function _rehydrateDeferredWorklogsFromCache(root){
  // After restoring a transcript from _sessionHtmlCache, deferred settled
  // worklogs carry data-worklog-rows-deferred="1" but lost their stashed rows
  // (JS properties don't survive innerHTML). Re-stash from the owning message so
  // the first expand materializes correctly. (#5839)
  if(!root||!root.querySelectorAll) return;
  root.querySelectorAll('[data-worklog-rows-deferred="1"]').forEach(group=>{
    if(group._deferredWorklogRows&&group._deferredWorklogRows.length) return;
    const rows=_deferredWorklogRowsFromGroup(group);
    if(rows&&rows.length) group._deferredWorklogRows=rows;
    else group.removeAttribute('data-worklog-rows-deferred'); // nothing to defer
  });
}
function _toggleActivityGroup(summary){
  const group=summary&&summary.closest?summary.closest('.agent-activity-group,.tool-call-group'):null;
  if(!group) return;
  const collapsed=group.classList.toggle('tool-call-group-collapsed');
  group.classList.toggle('open',!collapsed);
  summary.setAttribute('aria-expanded',String(!collapsed));
  // #5839: materialize deferred settled rows on first expand (lazy render).
  if(!collapsed) _materializeDeferredWorklogRows(group);
  _writeActivityDisclosureState(group.getAttribute('data-activity-disclosure-key'), !collapsed);
  if(typeof _onLiveActivityToggle==='function') _onLiveActivityToggle(group);
}
function _toggleToolWorklogGroup(summary){
  const group=summary&&summary.closest?summary.closest('.tool-worklog-tool-group,.tool-group'):null;
  if(group){
    const collapsed=group.classList.toggle('tool-worklog-tool-group-collapsed');
    group.classList.toggle('open',!collapsed);
    summary.setAttribute('aria-expanded',String(!collapsed));
    return;
  }
  return _toggleActivityGroup(summary);
}
function _finalizeLiveActivityDisclosureGroup(group){
  if(!group) return;
  const keepOpen=!!(
    group.querySelector&&group.querySelector('.tool-card.open,.thinking-card.open,.tool-group.open,.tool-worklog-tool-group.open')
  );
  const disclosureKey=group.getAttribute('data-activity-disclosure-key')||group.getAttribute('data-tool-worklog-key')||'';
  group.removeAttribute('data-live-activity-current');
  group.removeAttribute('data-live-tool-call-group');
  group.removeAttribute('data-live-tool-worklog-group');
  group.removeAttribute('data-live-anchor-scene-owner');
  group.classList.toggle('tool-call-group-collapsed', !keepOpen);
  group.classList.toggle('open', keepOpen);
  if(keepOpen&&disclosureKey) _writeActivityDisclosureState(disclosureKey, true);
  const summary=group.querySelector&&group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
  if(summary){
    summary.removeAttribute('data-live-summary-static');
    summary.removeAttribute('aria-disabled');
    summary.disabled=false;
    summary.setAttribute('aria-expanded',keepOpen?'true':'false');
  }
  if(typeof _syncToolCallGroupSummary==='function') _syncToolCallGroupSummary(group);
}

export {
  _activityDisclosureStorageKey,
  _readActivityDisclosureState,
  _writeActivityDisclosureState,
  _copyActivityDisclosureState,
  _activityKeyForLiveTurn,
  _onLiveActivityToggle,
  _materializeDeferredWorklogRows,
  _deferredWorklogRowsFromGroup,
  _rehydrateDeferredWorklogsFromCache,
  _toggleActivityGroup,
  _toggleToolWorklogGroup,
  _finalizeLiveActivityDisclosureGroup,
  _activityDisclosureStoragePrefix,
  _liveActivityUserExpanded,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _activityDisclosureStorageKey: { enumerable: true, get: () => _activityDisclosureStorageKey, set: (value) => { _activityDisclosureStorageKey = value; } },
  _readActivityDisclosureState: { enumerable: true, get: () => _readActivityDisclosureState, set: (value) => { _readActivityDisclosureState = value; } },
  _writeActivityDisclosureState: { enumerable: true, get: () => _writeActivityDisclosureState, set: (value) => { _writeActivityDisclosureState = value; } },
  _copyActivityDisclosureState: { enumerable: true, get: () => _copyActivityDisclosureState, set: (value) => { _copyActivityDisclosureState = value; } },
  _activityKeyForLiveTurn: { enumerable: true, get: () => _activityKeyForLiveTurn, set: (value) => { _activityKeyForLiveTurn = value; } },
  _onLiveActivityToggle: { enumerable: true, get: () => _onLiveActivityToggle, set: (value) => { _onLiveActivityToggle = value; } },
  _materializeDeferredWorklogRows: { enumerable: true, get: () => _materializeDeferredWorklogRows, set: (value) => { _materializeDeferredWorklogRows = value; } },
  _deferredWorklogRowsFromGroup: { enumerable: true, get: () => _deferredWorklogRowsFromGroup, set: (value) => { _deferredWorklogRowsFromGroup = value; } },
  _rehydrateDeferredWorklogsFromCache: { enumerable: true, get: () => _rehydrateDeferredWorklogsFromCache, set: (value) => { _rehydrateDeferredWorklogsFromCache = value; } },
  _toggleActivityGroup: { enumerable: true, get: () => _toggleActivityGroup, set: (value) => { _toggleActivityGroup = value; } },
  _toggleToolWorklogGroup: { enumerable: true, get: () => _toggleToolWorklogGroup, set: (value) => { _toggleToolWorklogGroup = value; } },
  _finalizeLiveActivityDisclosureGroup: { enumerable: true, get: () => _finalizeLiveActivityDisclosureGroup, set: (value) => { _finalizeLiveActivityDisclosureGroup = value; } },
  _activityDisclosureStoragePrefix: { enumerable: true, get: () => _activityDisclosureStoragePrefix },
  _liveActivityUserExpanded: { enumerable: true, get: () => _liveActivityUserExpanded, set: (value) => { _liveActivityUserExpanded = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
