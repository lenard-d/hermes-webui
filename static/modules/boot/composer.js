import {
  _forceMobileViewportReflow,
  _hasFinePointerCoexisting,
  _isDesktopWidth,
  _syncKeyboardBottomInset,
  _syncWorkspacePanelInlineWidth,
  closeMobileSidebar,
  handleWorkspaceClose,
  syncWorkspacePanelState,
  toggleSidebar,
} from './navigation.js';
import {
  _activeSlashCommandOffset,
  ensureSkillCommandsLoadedForAutocomplete,
  getComposerPathAutocompleteMatches,
  getMatchingCommands,
  getSlashAutocompleteMatches,
  hideCmdDropdown,
  navigateCmdDropdown,
  selectCmdDropdownItem,
  showCmdDropdown,
} from '../commands/index.js';

function _currentSessionIsReusableEmptyChat(){
  if(!S.session) return false;
  const hasVisibleMessages=Array.isArray(S.messages)
    && S.messages.some(m=>m&&m.role&&m.role!=='tool');
  return (S.session.message_count||0)===0
    && !hasVisibleMessages
    && !S.busy
    && !S.session.active_stream_id
    && !S.session.pending_user_message;
}

$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{
  // If the current session has no messages AND nothing is in flight, just focus
  // the composer rather than creating another empty session that will clutter the
  // sidebar list (#1171).
  //
  // The "nothing in flight" half is critical (#1432): if the user clicks + while
  // their first message is still streaming (or queued), `message_count` is still 0
  // server-side because the user turn hasn't been merged yet. The old guard treated
  // that as "empty" and made + a no-op for the entire stream duration, so users
  // couldn't actually start a parallel chat. Use the same in-flight signal as
  // `_restoreSettledSession()` in messages.js: an active stream id or a queued
  // pending user message means the session is real, not empty.
  if(_currentSessionIsReusableEmptyChat()){
    $('msg').focus();closeMobileSidebar();return;
  }
  if(typeof _restoreRememberedNewChatDraftSession==='function'
     && await _restoreRememberedNewChatDraftSession()){
    await renderSessionList();closeMobileSidebar();$('msg').focus();return;
  }
  await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();
};
$('btnDownload').onclick=()=>{
  if(!S.session)return;
  const blob=new Blob([transcript()],{type:'text/markdown'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`hermes-${S.session.session_id}.md`;a.click();URL.revokeObjectURL(a.href);
};
$('btnExportJSON').onclick=()=>{
  if(!S.session)return;
  const url=`/api/session/export?session_id=${encodeURIComponent(S.session.session_id)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${S.session.session_id}.json`;a.click();
};
$('btnShareSession').onclick=async()=>{
  if(!S.session) return;
  try{
    const existing=(S.session&&S.session.share_token)?new URL(`/share/${encodeURIComponent(S.session.share_token)}`,location.origin).href:null;
    if(existing){
      const reuse=await showConfirmDialog({
        title:t('share_session'),
        message:t('share_session_existing_confirm'),
        confirmLabel:t('share_session_copy_existing'),
        cancelLabel:t('share_session_refresh_snapshot'),
      });
      if(reuse){
        await _copyText(existing);
        showToast(t('share_session_link_copied'));
        window.open(existing,'_blank','noopener');
        return;
      }
    }
    const res=await api('/api/share/create',{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    if(res&&res.session) S.session=res.session;
    const href=new URL(String(res&&res.share&&res.share.url||''),location.origin).href;
    await _copyText(href);
    showToast(t('share_session_created'));
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
    window.open(href,'_blank','noopener');
  }catch(err){
    showToast(t('share_session_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
  }
};
$('btnStopSharingSession').onclick=async()=>{
  if(!S.session||!S.session.share_token) return;
  const ok=await showConfirmDialog({
    title:t('stop_sharing_session'),
    message:t('stop_sharing_session_confirm'),
    confirmLabel:t('stop_sharing_session'),
    danger:true,
  });
  if(!ok) return;
  try{
    const res=await api('/api/share/revoke',{method:'POST',body:JSON.stringify({session_id:S.session.session_id})});
    if(res&&res.session) S.session=res.session;
    showToast(t('share_session_revoked'));
    if(typeof _syncHermesPanelSessionActions==='function') _syncHermesPanelSessionActions();
  }catch(err){
    showToast(t('share_session_revoke_failed')+(err&&err.message?err.message:String(err||'')),4000,'error');
  }
};
function exportSessionHTML(session){
  const target=session||S.session;
  if(!target||!target.session_id)return;
  const sid=target.session_id;
  const theme=document.documentElement.classList.contains('dark')?'dark':'light';
  // Capture the live WebUI palette so the export matches the user's active
  // theme + skin exactly, not just the built-in dark/light fallback. Map each
  // export-template variable to its WebUI source; getComputedStyle resolves to
  // the currently rendered colour regardless of which skin is selected.
  const cs=getComputedStyle(document.documentElement);
  const read=(...names)=>{for(const n of names){const v=cs.getPropertyValue(n).trim();if(v)return v;}return '';};
  const palette={
    'bg':read('--bg'),
    'panel':read('--surface','--bg'),
    'panel2':read('--code-bg','--surface'),
    'border':read('--border'),
    'text':read('--text'),
    'muted':read('--muted','--text'),
    'accent':read('--accent'),
    'code-bg':read('--code-bg'),
    'code-border':read('--border2','--border'),
    'code-text':read('--text'),
  };
  // Drop empties so the inlined fallback keeps working for anything we couldn't read.
  const clean={};for(const k in palette){if(palette[k])clean[k]=palette[k];}
  const paletteB64=btoa(unescape(encodeURIComponent(JSON.stringify(clean))));
  const url=`/api/session/export?session_id=${encodeURIComponent(sid)}&format=html&theme=${theme}&palette=${encodeURIComponent(paletteB64)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${sid}.html`;a.click();
}
$('btnExportHTML').onclick=()=>exportSessionHTML();
$('btnImportJSON').onclick=()=>$('importFileInput').click();
$('importFileInput').onchange=async(e)=>{
  const file=e.target.files[0];
  if(!file)return;
  e.target.value='';
  try{
    const text=await file.text();
    const data=JSON.parse(text);
    const res=await api('/api/session/import',{method:'POST',body:JSON.stringify(data)});
    if(res.ok&&res.session){
      await loadSession(res.session.session_id);
      await renderSessionList();
      if(_currentPanel==='settings') switchPanel('chat');
      showToast(t('session_imported'));
    }
  }catch(err){
    showToast(t('import_failed')+(err.message||t('import_invalid_json')));
  }
};
$('btnClearPreview').onclick=handleWorkspaceClose;
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
function _applySessionContextMetadataUpdate(data){
  if(!S.session||!data||!data.session)return;
  S.session.context_length=data.session.context_length||0;
  S.session.threshold_tokens=data.session.threshold_tokens||0;
  S.session.last_prompt_tokens=data.session.last_prompt_tokens||0;
  S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;
  if(typeof _syncCtxIndicator==='function'){
    const u=S.lastUsage||{};
    const _pick=(latest,stored,dflt=0)=>latest!=null?latest:(stored!=null?stored:dflt);
    _syncCtxIndicator({
      input_tokens:_pick(u.input_tokens,S.session.input_tokens),
      output_tokens:_pick(u.output_tokens,S.session.output_tokens),
      estimated_cost:_pick(u.estimated_cost,S.session.estimated_cost),
      context_length:S.session.context_length||0,
      last_prompt_tokens:_pick(u.last_prompt_tokens,S.session.last_prompt_tokens),
      post_compression_context_tokens_estimate:S.session.post_compression_context_tokens_estimate,
      threshold_tokens:S.session.threshold_tokens||0,
    });
  }
}

$('modelSelect').onchange=async()=>{
  const selectedModel=$('modelSelect').value;
  const modelState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect($('modelSelect'),selectedModel)
    : {model:selectedModel,model_provider:null};
  if(typeof clearProfileTransitionReasoningContext==='function') clearProfileTransitionReasoningContext();
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  if(typeof _writePersistedModelState==='function') _writePersistedModelState(modelState.model,modelState.model_provider);
  else try{localStorage.setItem('hermes-webui-model',modelState.model)}catch{}
  if(!S.session){
    if(typeof _rememberEmptyComposerModelOverride==='function') _rememberEmptyComposerModelOverride(modelState.model,modelState.model_provider);
    if(typeof syncModelChip==='function') syncModelChip();
    if(typeof syncReasoningChip==='function') syncReasoningChip();
    return;
  }
  if(typeof _rememberPendingSessionModel==='function') _rememberPendingSessionModel(S.session.session_id,modelState.model,modelState.model_provider);
  S.session.model=modelState.model;
  S.session.model_provider=modelState.model_provider||null;
  if(typeof syncModelChip==='function') syncModelChip();
  if(typeof syncReasoningChip==='function') syncReasoningChip();
  syncTopbar();
  // Clarify scope: composer model changes are session-local, not the global default.
  if(typeof showToast==='function'){
    showToast(t('model_scope_toast')||'Applies to this conversation from your next message.', 3000);
  }
  const data=await api('/api/session/update',{method:'POST',body:JSON.stringify({
    session_id:S.session.session_id,
    workspace:S.session.workspace,
    model:modelState.model,
    model_provider:modelState.model_provider||null,
  })});
  // NOTE: do NOT clear the pending explicit-pick marker here. It must survive until
  // the NEXT send() consumes it, otherwise the normal "pick → session-update → send"
  // flow loses the explicit-pick signal before /api/chat/start runs and the server
  // re-reverts a cross-family pick (the #3737 bug, Codex catch). send() clears it
  // after reading a matching pending pick. (#3739/#3737)
  _applySessionContextMetadataUpdate(data);
  // Warn if selected model belongs to a different provider than what Hermes is configured for
  if(typeof _checkProviderMismatch==='function'){
    const warn=_checkProviderMismatch(selectedModel);
    if(warn&&typeof showToast==='function') showToast(warn,4000);
  }
};
$('msg').addEventListener('input',()=>{
  updateSendBtn();
  scheduleComposerAutoResize();
  // Persist composer draft to server (debounced in _saveComposerDraft).
  const sid = S && S.session && S.session.session_id;
  if (sid && typeof _saveComposerDraft === 'function') {
    _saveComposerDraft(sid, $('msg').value, S.pendingFiles ? [...S.pendingFiles] : []);
  }
  const text=$('msg').value;
  const _slashIdx=typeof _activeSlashCommandOffset==='function'?_activeSlashCommandOffset(text):-1;
  if(_slashIdx>=0&&text.indexOf('\n')===-1){
    if(typeof getSlashAutocompleteMatches==='function'){
      getSlashAutocompleteMatches(text).then(matches=>{
        if(($('msg').value||'')!==text) return;
        if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
      });
    }else{
      const prefix=text.slice(_slashIdx+1);
      const matches=getMatchingCommands(prefix);
      if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
    }
    if(typeof ensureSkillCommandsLoadedForAutocomplete==='function') ensureSkillCommandsLoadedForAutocomplete();
  } else if(typeof getComposerPathAutocompleteMatches==='function'){
    const cursor=$('msg').selectionStart;
    getComposerPathAutocompleteMatches(text,cursor).then(matches=>{
      const ta=$('msg');
      if(!ta||ta.value!==text||ta.selectionStart!==cursor) return;
      if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
    }).catch(()=>hideCmdDropdown());
  } else {
    hideCmdDropdown();
  }
});
// #5514/#5515: re-pin the transcript on ANY composer height change, not only the
// ones that route through the input->autoResize path. A multi-line paste
// (WisprFlow), a draft restore, an attachment tray / selection-chip appearing, a
// programmatic value set, or a font/reflow can all grow the composer and shrink
// the flex:1 transcript viewport, stranding a pinned reader above the bottom
// (reads as a "random" upward jump — #5515). Observe the whole #composerWrap
// (not just #msg) so tray/chip growth is covered too, at one seam. The re-pin is
// guarded (only fires when genuinely pinned), so it never fights a reader who
// scrolled away. First callback fires on observe (initial size) — the guard
// makes that a cheap no-op.
(()=>{
  const _cw=$('composerWrap')||$('msg');
  if(!_cw || typeof ResizeObserver!=='function' || typeof _repinMessagesAfterComposerResize!=='function') return;
  let _lastComposerH=_cw.offsetHeight;
  const _ro=new ResizeObserver(()=>{
    const h=_cw.offsetHeight;
    if(h<=_lastComposerH){_lastComposerH=h;return;}   // shrink/no-op: enlarges the viewport, can't strand
    _lastComposerH=h;
    _repinMessagesAfterComposerResize();               // grow: re-pin the pinned reader
  });
  try{ _ro.observe(_cw); }catch(_){ }
})();
// Track IME composition for East Asian input. Safari fires the committing
// keydown AFTER compositionend with isComposing=false, so we also keep a
// manual flag and reset it on the next tick to swallow that trailing Enter.
// Also reset on blur so the flag can never get stuck in a true state if
// compositionend never fires (focus loss with some IME implementations).
//
// The `_imeComposing` flag is bound to the chat composer (`#msg`); other
// inputs (session/project rename, app dialog, message edit, workspace rename)
// rely on the state-free `e.isComposing || e.keyCode === 229` part of
// `_isImeEnter`, which is sufficient for the Safari race because keyCode 229
// is the canonical "still composing" signal regardless of which field is
// focused. Promote `_isImeEnter` to `window` so other modules can reuse it
// without duplicating the full IIFE per input (issue #1443).
let _imeComposing=false;
(()=>{const _c=$('msg');if(!_c)return;
  _c.addEventListener('compositionstart',()=>{_imeComposing=true;});
  _c.addEventListener('compositionend',()=>{setTimeout(()=>{_imeComposing=false;},0);});
  _c.addEventListener('blur',()=>{_imeComposing=false;});
})();
function _isImeEnter(e){return e.isComposing||e.keyCode===229||_imeComposing;}
// #3076: a touch-primary device (`pointer:coarse`) can still have a
// physical keyboard attached (Android tablet + Bluetooth keyboard,
// detachable Surface in tablet mode, iPad + Magic Keyboard). When that
// happens we should NOT force the mobile newline-on-Enter override
// because Shift+Enter / Ctrl+Enter come from real keys and the user
// expects desktop semantics. `matchMedia('(any-pointer:fine)')` is true
// whenever ANY available pointing device is fine-grained — which is the
// strongest signal browsers expose for "there is a real keyboard /
// trackpad in the picture too". Skip the mobile default in that case.
function _isNumpadEnter(e){
  return e.key==='Enter'&&(e.code==='NumpadEnter'||e.location===KeyboardEvent.DOM_KEY_LOCATION_NUMPAD);
}
$('msg').addEventListener('keydown',e=>{
  // Autocomplete navigation when dropdown is open
  const dd=$('cmdDropdown');
  const dropdownOpen=dd&&dd.classList.contains('open');
  if(dropdownOpen){
    if(e.key==='ArrowUp'){e.preventDefault();navigateCmdDropdown(-1);return;}
    if(e.key==='ArrowDown'){e.preventDefault();navigateCmdDropdown(1);return;}
    if(e.key==='Tab'){e.preventDefault();selectCmdDropdownItem();return;}
    if(e.key==='Escape'){e.preventDefault();e.stopPropagation();hideCmdDropdown();return;}
    if(e.key==='Enter'&&!e.shiftKey){
      if(_isImeEnter(e)){return;}
      if(window._sendKey==='shift+enter'){
        return;
      }
      e.preventDefault();
      selectCmdDropdownItem();
      return;
    }
  }
  // Send key: respect user preference.
  // On touch-primary devices (coarse pointer, no fine pointer co-existing),
  // default to Enter = newline regardless of whether the visual viewport has
  // shrunk. The viewport-shrink heuristic (_isVirtualKeyboardLikelyOpen) was
  // unreliable on iOS Safari and some Android browsers where the keyboard
  // doesn't consistently reduce vv.height by >120px. The pointer media query
  // pair is a sufficient and more reliable signal for "software keyboard only".
  // Hardware keyboards on tablets are covered by _hasFinePointerCoexisting.
  // The 'ctrl+enter' and 'shift+enter' settings also use this behavior
  // (plain Enter = newline).
  // Users can override in Settings by explicitly choosing 'enter' mode.
  if(e.key==='Enter'){
    if(_isImeEnter(e)){return;}
    const isNumpadEnter=_isNumpadEnter(e);
    const _mobileDefault=matchMedia('(pointer:coarse)').matches
      &&!_hasFinePointerCoexisting()
      &&window._sendKey==='enter';
    if(window._sendKey==='shift+enter'){
      if(e.shiftKey){e.preventDefault();send();}
    } else if(window._sendKey==='ctrl+enter'||_mobileDefault){
      if(isNumpadEnter||e.ctrlKey||e.metaKey){e.preventDefault();send();}
    } else {
      if(!e.shiftKey){e.preventDefault();send();}
    }
  }
});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  // Cmd/Ctrl+B toggles desktop sidebar collapse (VS Code convention).
  // Skip when typing in an input/textarea/contenteditable so text-edit
  // shortcuts (e.g. bold in some embedded editors) are never stolen.
  if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&(e.key==='b'||e.key==='B')){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(!isText&&typeof toggleSidebar==='function'&&_isDesktopWidth()){
      e.preventDefault();
      toggleSidebar();
      return;
    }
  }
  // Cmd/Ctrl+/ focuses the message composer without creating a chat.
  // Match on the '/' CHARACTER (e.key), not the physical key position: on QWERTZ
  // layouts the physical Slash key produces Ctrl+- (browser zoom-out) and '/' is
  // typed as Shift+7, so matching the physical code both steals zoom and misses
  // the real '/' chord. e.key==='/' is layout-correct on every keyboard.
  if((e.metaKey||e.ctrlKey)&&!e.altKey&&e.key==='/'){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(isText) return;
    const composer=$('msg');
    if(composer){e.preventDefault();composer.focus();}
    return;
  }
  // Enter on approval card = Allow once (when a button inside the card is focused or
  // card is visible and focus is not on an input/textarea/select)
  if(e.key==='Enter'&&!e.metaKey&&!e.ctrlKey&&!e.shiftKey){
    const card=$('approvalCard');
    const tag=(document.activeElement||{}).tagName||'';
    if(card&&card.classList.contains('visible')&&tag!=='TEXTAREA'&&tag!=='INPUT'&&tag!=='SELECT'){
      e.preventDefault();
      if(typeof respondApproval==='function') respondApproval('once');
      return;
    }
  }
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    const t=e.target;
    const isText=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
    if(isText) return;
    e.preventDefault();
    // If the current session has no messages AND nothing is in flight, just focus
    // the composer rather than creating another empty session that will clutter
    // the sidebar list (#1171). See the matching guard in $('btnNewChat').onclick
    // and bug #1432 for why the in-flight check is needed.
    if(_currentSessionIsReusableEmptyChat()){
      $('msg').focus();return;
    }
    // Cmd/Ctrl+K should always create a new conversation, even while the current
    // one is still streaming. The old !S.busy guard meant users had to wait for
    // a long generation to finish before they could start something new — exactly
    // the moment they want to switch context. newSession() leaves the in-flight
    // stream running on its own session; the user just gets a fresh blank one.
    await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();
  }
  // Cmd/Ctrl+, opens/closes Settings (VS Code convention).
  // Fire globally — like VS Code, don't skip text inputs.
  if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&e.key===','){
    e.preventDefault();
    if(typeof toggleSettings==='function') toggleSettings();
    return;
  }
  if(e.key==='Escape'){
    // Close onboarding overlay if open (skip/dismiss the wizard)
    const onboardingOverlay=$('onboardingOverlay');
    if(onboardingOverlay&&onboardingOverlay.style.display!=='none'){
      if(typeof skipOnboarding==='function') skipOnboarding();
      return;
    }
    // Close settings panel if active
    if(_currentPanel==='settings'){_closeSettingsPanel();return;}
    // Close workspace dropdown
    closeWsDropdown();
    // Clear session search
    const ss=$('sessionSearch');
    if(ss&&ss.value){
      if(typeof clearSessionSearch==='function') clearSessionSearch(false);
      else { ss.value=''; filterSessions(); }
    }
    // Cancel any active message edit
    const editArea=document.querySelector('.msg-edit-area');
    if(editArea){
      const bar=editArea.closest('.msg-row')&&editArea.closest('.msg-row').querySelector('.msg-edit-bar');
      if(bar){const cancel=bar.querySelector('.msg-edit-cancel');if(cancel)cancel.click();}
    }
    // Blur composer to enable j/k message navigation.
    // Skip while an IME candidate window is composing — Escape there should
    // dismiss the candidate, not blur the composer (CJK input).
    if(document.activeElement===$('msg') && !e.isComposing && !_imeComposing){
      $('msg').blur();
    }
  }
});
const LARGE_TEXT_PASTE_CHAR_THRESHOLD=4000;
const LARGE_TEXT_PASTE_LINE_THRESHOLD=100;
function _largeTextPasteLineCount(text){
  const value=String(text||'');
  const lines=value.split('\n');
  return value.endsWith('\n')?lines.length-1:lines.length;
}
function _shouldAttachLargePastedText(text){
  if(window._largeTextPasteAsAttachment===false)return false;
  const value=String(text||'');
  if(!value.trim())return false;
  return value.length>=LARGE_TEXT_PASTE_CHAR_THRESHOLD || _largeTextPasteLineCount(value)>=LARGE_TEXT_PASTE_LINE_THRESHOLD;
}
function _largeTextPasteFileName(now){
  const d=new Date(now||Date.now());
  const p=n=>String(n).padStart(2,'0');
  const stamp=`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}-${String(d.getMilliseconds()).padStart(3,'0')}`;
  const existing=new Set((S.pendingFiles||[]).map(f=>f&&f.name).filter(Boolean));
  let name=`pasted-text-${stamp}.md`;
  for(let i=2;existing.has(name);i++)name=`pasted-text-${stamp}-${i}.md`;
  return name;
}
function _largeTextPasteFile(text,now){
  const name=_largeTextPasteFileName(now||Date.now());
  return new File([String(text||'')],name,{type:'text/markdown;charset=utf-8'});
}
function _largeTextPasteFitsUploadLimit(file){
  return !(file&&typeof MAX_UPLOAD_BYTES==='number'&&file.size>MAX_UPLOAD_BYTES);
}
function _attachLargePastedText(file){
  addFiles([file]);
  if(typeof setStatus==='function')setStatus(t('text_pasted')+file.name);
  return file;
}
$('msg').addEventListener('paste',e=>{
  const items=Array.from(e.clipboardData?.items||[]);
  // Extract image items (kind==='file' filter avoids misclassifying text/html
  // with embedded data URIs as images).
  const imageItems=items.filter(i=>i.kind==='file'&&i.type.startsWith('image/'));
  if(imageItems.length){
    // If text is also present (common when copying images from browsers, Notes,
    // Slack, etc.), let the browser paste the text normally AND attach the image.
    // Only preventDefault when the clipboard is image-only (true screenshot paste).
    const hasText=items.some(i=>i.kind==='string'&&(i.type==='text/plain'||i.type==='text/html'));
    if(!hasText)e.preventDefault();
    const pasteTs=Date.now();
    const files=imageItems.map((i,idx)=>{
      const blob=i.getAsFile();
      const ext=i.type.split('/')[1]||'png';
      const suffix=imageItems.length>1?`-${idx+1}`:'';
      return new File([blob],`screenshot-${pasteTs}${suffix}.${ext}`,{type:i.type});
    });
    addFiles(files);
    setStatus(t('image_pasted')+files.map(f=>f.name).join(', '));
    return;
  }
  const plainText=e.clipboardData?.getData('text/plain')||'';
  if(!_shouldAttachLargePastedText(plainText))return;
  const pastedTextFile=_largeTextPasteFile(plainText);
  if(!_largeTextPasteFitsUploadLimit(pastedTextFile))return;
  e.preventDefault();
  _attachLargePastedText(pastedTextFile);
});
document.querySelectorAll('.suggestion').forEach(btn=>{
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
});

function applyEmptyStateSuggestionPref(){
  if(!$('emptyState')) return;
  $('emptyState').classList.toggle('no-suggestions',window._hideEmptyStateSuggestions===true);
}

window.addEventListener('resize',()=>{
  _syncWorkspacePanelInlineWidth();
  syncWorkspacePanelState();
  if(!window.visualViewport) _forceMobileViewportReflow();
});

// On PWAs / mobile browsers that expose visualViewport, keyboard show/hide and
// URL-bar collapse fire visualViewport resize/scroll rather than window resize.
// Debounce a reflow so the phone layout repaints against the new geometry.
if(window.visualViewport){
  _syncKeyboardBottomInset();
  let _mobileViewportReflowTimer=0;
  const _scheduleMobileViewportReflow=()=>{
    if(_mobileViewportReflowTimer) clearTimeout(_mobileViewportReflowTimer);
    _mobileViewportReflowTimer=setTimeout(()=>{
      _mobileViewportReflowTimer=0;
      _forceMobileViewportReflow();
    },60);
  };
  window.visualViewport.addEventListener('resize', _scheduleMobileViewportReflow);
  window.visualViewport.addEventListener('scroll', _scheduleMobileViewportReflow);
}

// Boot: restore last session or start fresh
// ── Resizable panels ──────────────────────────────────────────────────────
let initResizePanels;
(function(){
  const SIDEBAR_MIN=180, SIDEBAR_MAX=420;
  const PANEL_MIN=180,   PANEL_MAX=1200;

  function initResize(handleId, targetEl, edge, minW, maxW, storageKey){
    const handle = $(handleId);
    if(!handle || !targetEl) return;

    // Restore saved width
    if(storageKey === 'hermes-panel-w'){
      _syncWorkspacePanelInlineWidth();
    }else{
      const saved = localStorage.getItem(storageKey);
      if(saved) targetEl.style.width = saved + 'px';
    }

    let startX=0, startW=0;

    handle.addEventListener('mousedown', e=>{
      e.preventDefault();
      startX = e.clientX;
      startW = targetEl.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.classList.add('resizing');

      const onMove = ev=>{
        const delta = edge==='right' ? ev.clientX - startX : startX - ev.clientX;
        const newW = Math.min(maxW, Math.max(minW, startW + delta));
        targetEl.style.width = newW + 'px';
      };
      const onUp = ()=>{
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        localStorage.setItem(storageKey, parseInt(targetEl.style.width));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Run after DOM ready (called from boot)
  initResizePanels = function(){
    const sidebar    = document.querySelector('.sidebar');
    const rightpanel = document.querySelector('.rightpanel');
    initResize('sidebarResize',    sidebar,    'right', SIDEBAR_MIN, SIDEBAR_MAX, 'hermes-sidebar-w');
    initResize('rightpanelResize', rightpanel, 'left',  PANEL_MIN,   PANEL_MAX,   'hermes-panel-w');
  };
})();

export {
  _currentSessionIsReusableEmptyChat,
  _isImeEnter,
  _shouldAttachLargePastedText,
  applyEmptyStateSuggestionPref,
  exportSessionHTML,
  initResizePanels,
};
