import { _followMessagesAfterDomReplace, scrollIfPinned, scrollToBottom } from './activity-and-scroll.js';
import { _deferClearProgrammaticScroll, _firstValidTimestampSeconds, _lastMessageClientHeight, _lastScrollTop, _maybeShowNewMessageScrollCue, _messageUserUnpinned, _nearBottomCount, _programmaticScroll, _programmaticScrollSetAt, _recentMessageScrollIntent, _recentMessageTouchScrollIntent, _scrollPinned } from './composer-controls.js';
import { _stripAttachedFilesMarkerForDisplay } from './composer.js';
import { _cliPatchSnippetFromArgs, _cliToolCardHasDiffSnippet, _cliToolCardSnippet, _cliToolResultSnippet, _toolArgsSnapshot } from './cli-tool-presentation.js';
import { _sessionHtmlCache, _sessionHtmlCacheSid } from './message-render-cache.js';
import { _captureMessageScrollSnapshot, _desktopAnchorRealignDelta, _restoreMessageScrollSnapshot, _restorePinnedMessageScrollSnapshot } from './message-scroll-snapshot.js';
import { _isTouchLikeMessageViewport, _messageViewportIntersectsRenderedRow, _remountMessageViewportAnchor, _restoreMessageViewportAnchor } from './navigation.js';
import { isTransparentStream } from './activity-presentation.js';
import { _assistantAnchorSceneFinalAnswerText, _stripLeadingAssistantThinkingMarkup } from './assistant-turn-presentation.js';
import { renderMessages } from './renderer.js';
import { $, S, _messageVirtualWindowKey, _stripWorkspaceDisplayPrefix } from './state.js';
import { compatibilityBindings as composerControlsBindings } from './composer-controls.js';
import { compatibilityBindings as stateBindings } from './state.js';

function _restoreMessageScrollSnapshotSameFrame(snapshot){
  const el=$('messages');
  if(!el||!snapshot) return;
  // Same-frame live DOM updates (tool/worklog/activity rows) are the hot path for
  // streaming. Pinned followers must stay tail-relative here too; restoring the
  // semantic viewport anchor is only safe for explicitly unpinned readers.
  if(_restorePinnedMessageScrollSnapshot(snapshot)) return;
  let restoredViaAnchor=(snapshot.anchor&&typeof _restoreMessageViewportAnchor==='function')
    ? _restoreMessageViewportAnchor(snapshot.anchor,0)
    : false;
  if(!restoredViaAnchor&&typeof _remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor)){
    restoredViaAnchor=(typeof _restoreMessageViewportAnchor==='function')
      ? _restoreMessageViewportAnchor(snapshot.anchor,0)
      : false;
  }
  if(!restoredViaAnchor){
    const maxTop=Math.max(0,el.scrollHeight-el.clientHeight);
    const bottom=Number(snapshot.bottom);
    // #5637: when the reader has scrolled UP into history (userUnpinned) and the
    // semantic anchor restore failed, do NOT snap scrollTop to the captured
    // ABSOLUTE snapshot.top. During streaming, the live activity-scene refresh
    // fires this every tick; above-viewport height keeps changing, so the old
    // absolute top no longer maps to the same content and the viewport is nudged
    // backward by an amount that grows with scrollHeight. Leaving scrollTop
    // untouched lets the browser's own scroll anchoring hold the reader's
    // position. Pinned / near-bottom readers still get the tail-relative restore
    // below (that path is correct and must run).
    if(snapshot.userUnpinned===true&&snapshot.pinned!==true){
      composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
      composerControlsBindings._messageUserUnpinned=true;
      composerControlsBindings._scrollPinned=false;
      composerControlsBindings._nearBottomCount=0;
      return;
    }
    const target=(snapshot.pinned===true&&Number.isFinite(bottom))
      ? maxTop-Math.max(0,bottom)
      : Number(snapshot.top)||0;
    // Streaming stale-snapshot guard (issue #5637). The userUnpinned check above is
    // defeated when a live stream re-pins the state machine (a scrollHeight-collapse
    // scroll event flips userUnpinned back to false even though the reader is up in
    // history), so this absolute snapshot.top write still fires and yanks a still
    // reader — snapshot.top was captured before the streaming chunk grew above-viewport
    // height, so it is stale. Mirror the realign guard: if content grew since the
    // snapshot AND there is no recent real input intent AND the write would move
    // scrollTop non-trivially, refuse it and let the browser overflow-anchor hold.
    // Pinned tail-followers (target is bottom-relative, not snapshot.top) are
    // unaffected; an actively scrolling reader has intent and keeps the restore.
    //
    // Desktop guard (issue #5637 gate cert): like the realign guard, this refusal
    // only holds where the browser's native overflow-anchor layer is active (touch
    // viewports, `.messages` computes to `overflow-anchor:auto`). Desktop `.messages`
    // is `overflow-anchor:none`, so refusing the absolute fallback write there would
    // leave the reader unheld AND latch `_messageUserUnpinned=true`. Gate on
    // `_isTouchLikeMessageViewport` so desktop keeps its absolute snapshot.top restore.
    const _snapSH=Number(snapshot.scrollHeight);
    const _grewSinceSnap=Number.isFinite(_snapSH)&&_snapSH>0&&(el.scrollHeight-_snapSH)>4;
    const _fbActiveIntent=(typeof _recentMessageScrollIntent==='function' && _recentMessageScrollIntent())
      || (typeof _recentMessageTouchScrollIntent==='function' && _recentMessageTouchScrollIntent());
    const _fbTouchHold=(typeof _isTouchLikeMessageViewport==='function' && _isTouchLikeMessageViewport(el));
    if(_fbTouchHold && snapshot.pinned!==true && _grewSinceSnap && !_fbActiveIntent
       && Math.abs((Math.max(0,Math.min(target,maxTop)))-el.scrollTop)>8){
      composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
      composerControlsBindings._messageUserUnpinned=true;
      composerControlsBindings._scrollPinned=false;
      composerControlsBindings._nearBottomCount=0;
      return;
    }
    // Desktop stale-snapshot residue fix (issue #5637 follow-up, PR #5742 round-3).
    // On desktop (overflow-anchor:none) the touch refusal above does NOT apply — the
    // reader must be actively held, so we write scrollTop. The ABSOLUTE snapshot.top is
    // stale once above-viewport content grew since capture. Use the app's own realign
    // idiom instead: shift the CURRENT scrollTop by how far the anchor row moved since
    // capture. `scrollTop += (currentOffset - capturedOffset)` holds the row put no
    // matter where scrollTop was carried (a row's offset is scroll-relative), which the
    // staged `snapshot.top + delta` cannot. No arbiter: the realign is a no-op when
    // already aligned (delta ~ 0) and heals when not. Only when the anchor row is
    // genuinely gone (per-tier lookup concedes, no rawIdx degradation) do we fall back
    // to the topPad-delta idiom, then to raw. Pinned/near-bottom readers took the
    // bottom-relative target above and never reach here as unpinned.
    let _fbTarget=Math.max(0,Math.min(target,maxTop));
    if(!_fbTouchHold && snapshot.pinned!==true){
      const _realign=_desktopAnchorRealignDelta(el, snapshot.anchor);
      if(_realign!==null){
        // Anchor row measurable: realign from the LIVE scrollTop (app idiom).
        _fbTarget=Math.max(0,Math.min(el.scrollTop+_realign, maxTop));
      }else{
        // Anchor row genuinely gone. Mirror the topPad-delta idiom the anchor already
        // carries (topPadBefore): shift by the growth of the virtual top spacer since
        // capture so the reader is held by the same amount the content above moved.
        const _padNow=(function(){
          const s=el.querySelector('[data-virtual-spacer="before"]');
          return s?(parseFloat(s.style.height||'0')||0):NaN;
        })();
        const _padBeforeRaw=snapshot.anchor&&snapshot.anchor.topPadBefore;
        const _padBefore=Number(_padBeforeRaw);
        // Require an ACTUAL captured topPadBefore (not null/undefined): Number(null) is 0,
        // which would otherwise add the ENTIRE current spacer height to scrollTop and fling
        // the reader far from their content (greptile P1). Only apply when it was really
        // captured; else keep the raw fallback target.
        if(_padBeforeRaw!=null&&Number.isFinite(_padNow)&&Number.isFinite(_padBefore)){
          _fbTarget=Math.max(0,Math.min(el.scrollTop+(_padNow-_padBefore), maxTop));
        }
        // else: no measurable anchor and no topPad geometry -> keep raw target.
      }
    }
    composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
    el.scrollTop=_fbTarget;
  }
  composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
  if(snapshot.pinned===true){
    composerControlsBindings._messageUserUnpinned=false;
    composerControlsBindings._scrollPinned=true;
    composerControlsBindings._nearBottomCount=2;
  }else if(snapshot.userUnpinned===true){
    composerControlsBindings._messageUserUnpinned=true;
    composerControlsBindings._scrollPinned=false;
    composerControlsBindings._nearBottomCount=0;
  }
  if(!restoredViaAnchor){
    if(typeof _deferClearProgrammaticScroll==='function') _deferClearProgrammaticScroll();
    else requestAnimationFrame(()=>{ setTimeout(()=>{ composerControlsBindings._programmaticScroll=false; },0); });
  }
}
function _renderMessagesWithScrollSnapshot(options){
  const scrollSnapshot=_captureMessageScrollSnapshot();
  renderMessages({...(options||{}),preserveScroll:true});
  _restoreMessageScrollSnapshotSameFrame(scrollSnapshot);
}
let _assistantTurnAnchorSettledFinalAnswerWarned=false;
function _transparentStreamOrderedParts(message){
  if(typeof isTransparentStream==='function'&&!isTransparentStream()) return null;
  if(!message||message.role!=='assistant'||message._live||!Array.isArray(message.content)) return null;
  if(message._anchor_activity_scene) return null;
  const ordered=[];
  const messageTs=typeof _firstValidTimestampSeconds==='function'
    ? _firstValidTimestampSeconds(message._ts, message.timestamp, message.created_at)
    : (message._ts||message.timestamp||message.created_at);
  let hasText=false;
  let hasTool=false;
  for(const part of message.content){
    if(!part||typeof part!=='object') continue;
    if(part.type==='text'){
      const text=typeof part.text==='string'?part.text:(typeof part.content==='string'?part.content:'');
      if(!String(text||'').trim()) continue;
      ordered.push({kind:'text', text});
      hasText=true;
      continue;
    }
    if(part.type==='tool_use'){
      const toolUseId=String(part.id||'').trim();
      if(!toolUseId) return null;
      ordered.push({
        kind:'tool',
        toolUseId,
        name:part.name||'tool',
        input:(part.input&&typeof part.input==='object')?part.input:{},
        ts:part.ts,
        timestamp:part.timestamp,
        created_at:part.created_at,
        message_ts:messageTs,
      });
      hasTool=true;
    }
  }
  return hasText&&hasTool?ordered:null;
}
function _legacySettledFallbackHasToolMetadata(message){
  if(!message||message.role!=='assistant'||message._anchor_activity_scene) return false;
  return !!(
    (Array.isArray(message.tool_calls)&&message.tool_calls.length>0)||
    (Array.isArray(message._partial_tool_calls)&&message._partial_tool_calls.length>0)||
    (Array.isArray(message.content)&&message.content.some(part=>part&&typeof part==='object'&&part.type==='tool_use'))
  );
}
function _transparentOrderedDisplayText(text){
  return _stripWorkspaceDisplayPrefix(
    _stripAttachedFilesMarkerForDisplay(
      _stripLeadingAssistantThinkingMarkup(String(text||''))
    )
  );
}
function _collectToolResultSnippetsByTid(messages){
  const resultsByTid={};
  for(const message of (messages||[])){
    if(!message) continue;
    if(message.role==='tool'){
      const tid=message.tool_call_id||message.tool_use_id||'';
      if(tid) resultsByTid[tid]=_cliToolResultSnippet(message.content);
      continue;
    }
    if(!Array.isArray(message.content)) continue;
    for(const part of message.content){
      if(!part||typeof part!=='object'||part.type!=='tool_result') continue;
      const tid=part.tool_use_id||'';
      if(!tid) continue;
      const raw=typeof part.content==='string'
        ? part.content
        : Array.isArray(part.content)
          ? part.content.map(c=>c&&c.text?c.text:'').join('')
          : '';
      resultsByTid[tid]=_cliToolResultSnippet(raw);
    }
  }
  return resultsByTid;
}
function _transparentOrderedToolCall(part, rawIdx, toolCallsByTid, resultsByTid, persistedByTid, messageTs){
  const tid=String(part&&part.toolUseId||'').trim();
  const firstValidTimestampSeconds=typeof _firstValidTimestampSeconds==='function'
    ? _firstValidTimestampSeconds
    : function(...values){
        for(const value of values){
          const stamp=Number(value);
          if(Number.isFinite(stamp)&&stamp>0) return stamp>1e12?stamp/1000:stamp;
        }
        return null;
      };
  const messageStamp=firstValidTimestampSeconds(messageTs, part&&part.message_ts);
  const partStamp=firstValidTimestampSeconds(part&&part.ts, part&&part.timestamp, part&&part.created_at);
  const liveTool=tid&&toolCallsByTid&&toolCallsByTid.get(tid);
  if(liveTool){
    const next={...liveTool};
    const hasEventStamp=firstValidTimestampSeconds(next.ts, next.timestamp, next.created_at, next.started_at, next.completed_at);
    const fallbackStamp=partStamp||messageStamp;
    if(!hasEventStamp&&fallbackStamp){
      next.ts=fallbackStamp;
      next.timestamp=fallbackStamp;
      next.created_at=fallbackStamp;
    }
    const liveSnip=(resultsByTid&&resultsByTid[tid])||(persistedByTid&&persistedByTid[tid])||'';
    if(liveSnip){
      const patchSnippet=_cliPatchSnippetFromArgs(next.name||part.name||'tool', next.args||part.input||{});
      next.snippet=_cliToolCardSnippet(liveSnip,patchSnippet);
      next.is_diff=_cliToolCardHasDiffSnippet(liveSnip,patchSnippet);
    }
    if(next.done===undefined) next.done=true;
    return next;
  }
  const name=part&&part.name||'tool';
  const args=(part&&part.input&&typeof part.input==='object')?part.input:{};
  const patchSnippet=_cliPatchSnippetFromArgs(name,args);
  const resultSnippet=(resultsByTid&&tid&&resultsByTid[tid])||(persistedByTid&&tid&&persistedByTid[tid])||'';
  const fallbackStamp=partStamp||messageStamp;
  const primaryStamp=firstValidTimestampSeconds(part&&part.ts, part&&part.timestamp, part&&part.created_at, fallbackStamp);
  return {
    name,
    tid,
    id:tid,
    assistant_msg_idx:rawIdx,
    args:_toolArgsSnapshot(args),
    snippet:_cliToolCardSnippet(resultSnippet,patchSnippet),
    is_diff:_cliToolCardHasDiffSnippet(resultSnippet,patchSnippet),
    done:true,
    ts:primaryStamp||undefined,
    timestamp:primaryStamp||undefined,
    created_at:primaryStamp||undefined,
  };
}
function _assistantTurnAnchorSettledFinalAnswer(message, content, context){
  const sceneFinal=_assistantAnchorSceneFinalAnswerText(message);
  const effectiveContent=String(content||'').trim()?content:sceneFinal;
  try{
    const api=(typeof window!=='undefined')?window.HermesAssistantTurnAnchors:null;
    if(!api||typeof api.projectAssistantTurnAnchorSettledMessageFinalAnswer!=='function') return String(sceneFinal||'').trim()?sceneFinal:null;
    const result=api.projectAssistantTurnAnchorSettledMessageFinalAnswer(message,{
      session_id:context&&context.session_id,
      raw_idx:context&&context.raw_idx,
      content:effectiveContent,
    });
    const finalAnswer=result&&typeof result.final_answer==='string'?result.final_answer:'';
    return finalAnswer?finalAnswer:(String(sceneFinal||'').trim()?sceneFinal:null);
  }catch(err){
    if(!_assistantTurnAnchorSettledFinalAnswerWarned&&typeof console!=='undefined'&&console.warn){
      _assistantTurnAnchorSettledFinalAnswerWarned=true;
      console.warn('assistant turn anchor settled-final projection failed',err);
    }
    return null;
  }
}
// Re-anchor a pinned/tail-following reader to the settled bottom after a full
// renderMessages() rebuild, eliminating the one-frame mid-stream jitter. MUST be scheduled
// in a MICROTASK from the end of renderMessages (see the queueMicrotask call site), NOT run
// synchronously. Why: the mid-stream re-render bug is that renderMessages wipes #msgInner
// then rebuilds, and the pinned tail-follow path (scrollIfPinned → scrollToBottom) writes
// scrollTop while still INSIDE the render sync stack, where the browser reports a TRANSIENT
// scrollHeight a few px short of the settled value (layout is batched — every geometry read
// inside the sync stack returns the mid-settle height). So scrollToBottom lands scrollTop a
// little HIGH (short of the true tail); that intermediate is painted this frame and the
// settle rAF corrects it the next frame → a fast ~1-row back-and-forth bounce (~82px). A
// microtask runs AFTER the sync stack unwinds (layout has flushed, so scrollHeight/clientHeight
// are the settled values) but BEFORE the browser paints — so writing the now-correct settled
// max here lands the tail exactly and the short intermediate never reaches the screen. Only
// fires for a pre-wipe tail-follower left short of the settled max, so an unpinned reader
// parked in history is never moved (orthogonal to the unpinned jump-back class). The
// _programmaticScroll latch (armed at the wipe) keeps the scroll listener from misreading
// this write as a manual unpin. Idempotent: a no-op once scrollTop already equals the max.
function _reanchorPinnedTailAfterRender(wasNearTail){
  if(!wasNearTail) return;
  const el=$('messages');
  if(!el) return;
  const settledMax=Math.max(0, el.scrollHeight-el.clientHeight);
  if(el.scrollTop < settledMax-1){
    composerControlsBindings._programmaticScroll=true;composerControlsBindings._programmaticScrollSetAt=performance.now();
    el.scrollTop=settledMax;
    composerControlsBindings._lastScrollTop=el.scrollTop;composerControlsBindings._lastMessageClientHeight=el.clientHeight;
    composerControlsBindings._nearBottomCount=2;
    composerControlsBindings._scrollPinned=true;
  }
}
function _scrollAfterMessageRender(preserveScroll, scrollSnapshot){
  // Terminal stream renders can happen after S.activeStreamId is cleared.
  // In that case, preserveScroll asks the normal pin-state helper to decide:
  // pinned users stay at bottom; users who manually scrolled up get their
  // pre-render scrollTop restored after the DOM replacement.
  if(preserveScroll){
    const readerAwayFromBottom=!!(
      scrollSnapshot &&
      Number.isFinite(Number(scrollSnapshot.bottom)) &&
      Number(scrollSnapshot.bottom)>250
    );
    // Keep master's follow heuristic for pinned / still-near-bottom users:
    // _followMessagesAfterDomReplace() does a FORCED scrollToBottom() (synchronous
    // bottom write + forced settle), so the final settled response can't leave a
    // pinned reader a few lines short. Only genuinely-scrolled-up (unpinned, not
    // near bottom) users fall through to keep their position and get the
    // new-message cue. (Using scrollIfPinned() here instead would skip the forced
    // write unless distance>500 and let the DOM-rebuild scroll event cancel the
    // delayed settles — Codex CORE catch on #3631.)
    if(!readerAwayFromBottom && !_messageUserUnpinned && _followMessagesAfterDomReplace()) return;
    _restoreMessageScrollSnapshot(scrollSnapshot);
    _maybeShowNewMessageScrollCue(scrollSnapshot);
    return;
  }
  if(S.activeStreamId){
    // Mid-stream re-render (tool completion, activity-scene refresh, clarify echo).
    // renderMessages() wipes #msgInner (inner.innerHTML='') then rebuilds; that wipe
    // collapses scrollHeight toward the empty-table height, and the browser is FORCED
    // to clamp #messages.scrollTop down to the new (near-zero) max. For a reader who
    // scrolled UP into history (unpinned), scrollIfPinned() is a no-op — so it does NOT
    // undo that clamp, and the reader is stranded at the top (the scroll jump-back). The
    // wipe-to-empty clamp is a browser primitive (device-agnostic; JS never writes the
    // scrollTop), so the passive no-op cannot preserve position here. renderMessages()
    // captured a pre-wipe snapshot for exactly this case (its scrollSnapshot init fires
    // when _messageUserUnpinned), so restore the unpinned reader's viewport instead of
    // the no-op. Pinned/tail-following readers keep scrollIfPinned() (correct live-follow).
    if(_messageUserUnpinned && scrollSnapshot){
      _restoreMessageScrollSnapshot(scrollSnapshot);
      _maybeShowNewMessageScrollCue(scrollSnapshot);
      return;
    }
    scrollIfPinned();
    return;
  }
  // Manual unpin is sticky: once the reader scrolls away, automatic idle/non-
  // preserve re-renders must restore their viewport rather than clearing the
  // unpin state with scrollToBottom(). A fresh session load (not unpinned) still
  // lands at the bottom as expected. (Codex #4006 follow-up.)
  // renderMessages() captures the pre-wipe snapshot for this case too (see its
  // scrollSnapshot init), so restoring here lands the reader where they were.
  if(_messageUserUnpinned){
    _restoreMessageScrollSnapshot(scrollSnapshot);
    _maybeShowNewMessageScrollCue(scrollSnapshot);
    return;
  }
  scrollToBottom();
}

function _maybeRecoverVirtualizedBlankViewport(options, preserveScroll, virtualWindow){
  if(!preserveScroll||!virtualWindow||!virtualWindow.virtualized||!!(options&&options._virtualFallback)) return false;
  if(_messageViewportIntersectsRenderedRow()) return false;
  if(_sessionHtmlCacheSid&&S.session&&S.session.session_id===_sessionHtmlCacheSid){
    _sessionHtmlCache.delete(_sessionHtmlCacheSid);
  }
  stateBindings._messageVirtualWindowKey='';
  renderMessages({preserveScroll:true,_virtualFallback:true});
  return true;
}



export {
  _restoreMessageScrollSnapshotSameFrame,
  _renderMessagesWithScrollSnapshot,
  _transparentStreamOrderedParts,
  _legacySettledFallbackHasToolMetadata,
  _transparentOrderedDisplayText,
  _collectToolResultSnippetsByTid,
  _transparentOrderedToolCall,
  _assistantTurnAnchorSettledFinalAnswer,
  _reanchorPinnedTailAfterRender,
  _scrollAfterMessageRender,
  _maybeRecoverVirtualizedBlankViewport,
  _assistantTurnAnchorSettledFinalAnswerWarned,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _restoreMessageScrollSnapshotSameFrame: { enumerable: true, get: () => _restoreMessageScrollSnapshotSameFrame, set: (value) => { _restoreMessageScrollSnapshotSameFrame = value; } },
  _renderMessagesWithScrollSnapshot: { enumerable: true, get: () => _renderMessagesWithScrollSnapshot, set: (value) => { _renderMessagesWithScrollSnapshot = value; } },
  _transparentStreamOrderedParts: { enumerable: true, get: () => _transparentStreamOrderedParts, set: (value) => { _transparentStreamOrderedParts = value; } },
  _legacySettledFallbackHasToolMetadata: { enumerable: true, get: () => _legacySettledFallbackHasToolMetadata, set: (value) => { _legacySettledFallbackHasToolMetadata = value; } },
  _transparentOrderedDisplayText: { enumerable: true, get: () => _transparentOrderedDisplayText, set: (value) => { _transparentOrderedDisplayText = value; } },
  _collectToolResultSnippetsByTid: { enumerable: true, get: () => _collectToolResultSnippetsByTid, set: (value) => { _collectToolResultSnippetsByTid = value; } },
  _transparentOrderedToolCall: { enumerable: true, get: () => _transparentOrderedToolCall, set: (value) => { _transparentOrderedToolCall = value; } },
  _assistantTurnAnchorSettledFinalAnswer: { enumerable: true, get: () => _assistantTurnAnchorSettledFinalAnswer, set: (value) => { _assistantTurnAnchorSettledFinalAnswer = value; } },
  _reanchorPinnedTailAfterRender: { enumerable: true, get: () => _reanchorPinnedTailAfterRender, set: (value) => { _reanchorPinnedTailAfterRender = value; } },
  _scrollAfterMessageRender: { enumerable: true, get: () => _scrollAfterMessageRender, set: (value) => { _scrollAfterMessageRender = value; } },
  _maybeRecoverVirtualizedBlankViewport: { enumerable: true, get: () => _maybeRecoverVirtualizedBlankViewport, set: (value) => { _maybeRecoverVirtualizedBlankViewport = value; } },
  _assistantTurnAnchorSettledFinalAnswerWarned: { enumerable: true, get: () => _assistantTurnAnchorSettledFinalAnswerWarned, set: (value) => { _assistantTurnAnchorSettledFinalAnswerWarned = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
