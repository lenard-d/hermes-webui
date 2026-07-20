import { syncToolsetsChip } from './composer-controls.js';
import { _liveModelFetchPending, syncModelChip } from './model-catalog.js';
import { syncReasoningChip } from './model-selection.js';
import { _applyModelToDropdown, _applySessionModelFallback, _ensureModelOptionInDropdown, _persistSessionModelCorrection, _providerDefersMissingModelFallback } from './model-state.js';
import { $, S, assistantDisplayName } from './state.js';
import { _syncWorkspaceHeadingState } from './workspace-and-uploads.js';

function _topbarLoadedMessageCount(){
  const messages=Array.isArray(S.messages)?S.messages:[];
  return messages.filter((message)=>message&&message.role&&message.role!=='tool').length;
}

function _topbarMessageMetaText(){
  const loadedCount=_topbarLoadedMessageCount();
  const totalCount=Number(S.session&&S.session.message_count);
  const hasTotal=Number.isFinite(totalCount)&&totalCount>0;
  const isTruncated=!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);
  if(isTruncated&&hasTotal&&totalCount>loadedCount){
    return `${loadedCount} loaded of ${totalCount} messages`;
  }
  return t('n_messages',loadedCount);
}

function syncTopbar(){
  if(!S.session){
    document.title=assistantDisplayName();
    if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
    if(typeof _syncWorkspaceHeadingState==='function') _syncWorkspaceHeadingState();
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof syncTerminalButton==='function') syncTerminalButton();
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
    else {
      const sidebarName=$('sidebarWsName');
      if(sidebarName && sidebarName.textContent==='Workspace'){
        sidebarName.textContent=t('no_workspace');
      }
    }
    if(typeof syncAppTitlebar==='function') syncAppTitlebar();
    // Update profile chip even when no session is active (e.g. right after profile switch)
    const _profileLabel=$('profileChipLabel');
    if(_profileLabel) _profileLabel.textContent=S.activeProfile||'default';
    const _titleLabel=$('titlebarProfileLabel');
    if(_titleLabel) _titleLabel.textContent=S.activeProfile||'default';
    return;
  }
  const sessionTitle=S.session.title||t('untitled');
  const _topbarTitle=$('topbarTitle');if(_topbarTitle)_topbarTitle.textContent=sessionTitle;
  document.title=sessionTitle+' \u2014 '+assistantDisplayName();
  if(typeof activeSessionHasPendingPromptAttention==='function'&&activeSessionHasPendingPromptAttention()){
    document.title='● '+document.title;
  }
  const _topbarMeta=$('topbarMeta');
  if(_topbarMeta){
    let sourceLabel=(S.session&&(S.session.source_label||S.session.source_tag||S.session.raw_source))||'';
    // Recovered sidecars stamp source_label 'WebUI' (api/session_recovery.py); don't badge a native session as its own source (#3338).
    if(/^webui$/i.test(sourceLabel)) sourceLabel='';
    const metaText=_topbarMessageMetaText();
    _topbarMeta.textContent=metaText;
    if(sourceLabel){
      const badge=document.createElement('span');
      badge.className='topbar-source-badge';
      badge.textContent=sourceLabel+(S.session.read_only?' · read-only':'');
      _topbarMeta.appendChild(document.createTextNode(' '));
      _topbarMeta.appendChild(badge);
    }
  }
  if(typeof syncAppTitlebar==='function') syncAppTitlebar();
  if(typeof _syncWorkspaceHeadingState==='function') _syncWorkspaceHeadingState();
  // If a profile switch just happened, apply its model rather than the session's stale value.
  // S._pendingProfileModel is set by switchToProfile() and cleared here after one application.
  const modelOverride=S._pendingProfileModel;
  let currentModel=S.session.model||'';
  if(modelOverride){
    S._pendingProfileModel=null;
    const providerOverride=S._pendingProfileModelProvider||null;
    S._pendingProfileModelProvider=null;
    _applyModelToDropdown(modelOverride,$('modelSelect'),providerOverride);
    currentModel=modelOverride;
  } else {
    const modelSel=$('modelSelect');
    const rawCurrentModel=String(currentModel||'').trim();
    const hasSessionModel=rawCurrentModel&&rawCurrentModel.toLowerCase()!=='unknown';
    if(!hasSessionModel){
      // Missing/unknown session metadata must not leave the picker on the
      // previously viewed chat's model (#1771). Apply the configured default
      // first, then the first available option only as an HTML fallback.
      const fallback=_applySessionModelFallback(modelSel);
      if(fallback){
        // Defer state mutation + network write while the live model resolution
        // is in flight — sessions.js sets _modelResolutionDeferred=true between
        // the fast-path session render and the resolve_model=1 round-trip.
        // Persisting here would race that resolution and would also issue
        // silent /api/session/update POSTs against imported/read-only CLI
        // sessions whose model field reads "unknown" (#1779 stage-310 review).
        // The visible sel.value change still happens above for UX; only the
        // state mutation + persist defers.
        const deferModelCorrection=Boolean(S.session._modelResolutionDeferred);
        if(!deferModelCorrection){
          S.session.model=fallback.model;
          S.session.model_provider=fallback.model_provider||null;
          currentModel=fallback.model;
          _persistSessionModelCorrection(fallback.model,S.session.model_provider||null);
        }
      }
    } else {
      const applied=_applyModelToDropdown(currentModel,modelSel,S.session.model_provider||null);
      // If the session model is missing from the current provider list, inject
      // a session-scoped option instead of displaying the previous/static
      // selection. Only fall back if that repair path is unavailable.
      if(!applied){
        const deferModelCorrection=Boolean(S.session._modelResolutionDeferred);
        const missingModelIsRoutable=_providerDefersMissingModelFallback(S.session.model_provider||window._activeProvider||null);
        // Also defer if a live model fetch is still in flight — the model may be
        // in the list once the fetch completes. Persisting now would corrupt the
        // session with the wrong model before live models arrive (#1169).
        const liveStillPending=window._activeProvider&&_liveModelFetchPending.has(window._activeProvider);
        if(liveStillPending||missingModelIsRoutable){
          // Live fetch in flight — don't touch sel.value or S.session.model yet.
          // _addLiveModelsToSelect() will re-apply S.session.model once done (#1169).
          // Named custom providers/OpenRouter can also route vendor-prefixed IDs
          // outside the static catalog, so preserve the user's explicit choice.
          if(typeof _ensureModelOptionInDropdown==='function'){
            const sessionOption=_ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null);
            if(sessionOption) currentModel=sessionOption;
          }
        } else {
          const sessionOption=(typeof _ensureModelOptionInDropdown==='function')
            ? _ensureModelOptionInDropdown(currentModel,modelSel,S.session.model_provider||null)
            : null;
          if(sessionOption){
            currentModel=sessionOption;
          } else {
            const fallback=_applySessionModelFallback(modelSel);
            if(fallback&&!deferModelCorrection){
              S.session.model=fallback.model;
              S.session.model_provider=fallback.model_provider||null;
              currentModel=fallback.model;
              // Persist the correction so the session doesn't re-inject on next load.
              _persistSessionModelCorrection(fallback.model,S.session.model_provider||null);
            }
          }
        }
      }
    }
  }
  if(typeof syncModelChip==='function') syncModelChip();
  if(typeof syncReasoningChip==='function') syncReasoningChip();
  if(typeof syncToolsetsChip==='function') syncToolsetsChip();
  // Show Clear button only when session has messages
  const clearBtn=$('btnClearConv');
  if(clearBtn) clearBtn.style.display=(S.messages&&S.messages.filter(msg=>msg.role!=='tool').length>0)?'':'none';
  if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  if(typeof syncWorkspaceDisplays==='function') syncWorkspaceDisplays();
  if(typeof syncTerminalButton==='function') syncTerminalButton();
  // modelSelect already set above
  // Update profile chip label.
  // The chip is the profile-SWITCHER trigger (it fronts the profile dropdown) and
  // governs where the next message / new chat routes — both follow the client
  // active profile (the hermes_profile cookie, set only by /api/profile/switch).
  // It must therefore reflect S.activeProfile, NOT the loaded session's profile.
  // #3331 briefly keyed this on S.session.profile so the label would track the
  // session being browsed, but loadSession() never updates S.activeProfile, so
  // opening a cross-profile session made the chip disagree with the dropdown
  // checkmark and lie about message routing (#3635). #3331's legitimate work —
  // scoping project/session operations to the session's own profile — is
  // unaffected by this line.
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
  const titleLabel=$('titlebarProfileLabel');
  if(titleLabel) titleLabel.textContent=S.activeProfile||'default';
}

export {
  _topbarLoadedMessageCount,
  _topbarMessageMetaText,
  syncTopbar,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _topbarLoadedMessageCount: { enumerable: true, get: () => _topbarLoadedMessageCount, set: (value) => { _topbarLoadedMessageCount = value; } },
  _topbarMessageMetaText: { enumerable: true, get: () => _topbarMessageMetaText, set: (value) => { _topbarMessageMetaText = value; } },
  syncTopbar: { enumerable: true, get: () => syncTopbar, set: (value) => { syncTopbar = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
