// Composes live content projection, incremental markdown, optional fade,
// Anchor prose projection, and the render scheduler for one owned stream.
import { createAnchorProseRenderer } from './anchor-prose-rendering.js';
import { enhanceMarkdownTables } from './markdown-tables.js';
import { createStreamContentProjection } from './stream-content.js';
import { createStreamFadeController } from './stream-fade.js';
import { createStreamingMarkdown } from './stream-markdown.js';

export function createStreamRenderer(options={}){
  const readState=typeof options.readState==='function'?options.readState:()=>({});
  const updateLiveThinking=typeof options.updateLiveThinking==='function'?options.updateLiveThinking:()=>{};
  const upsertAnchorProse=typeof options.upsertAnchorProse==='function'?options.upsertAnchorProse:()=>null;
  const syncWorklogReasons=typeof options.syncWorklogReasons==='function'?options.syncWorklogReasons:()=>{};
  const scrollPinned=typeof options.scrollPinned==='function'?options.scrollPinned:()=>{};
  const snapshotLiveTurn=typeof options.snapshotLiveTurn==='function'?options.snapshotLiveTurn:()=>{};
  const resetSegmentState=typeof options.resetSegmentState==='function'?options.resetSegmentState:()=>{};
  const content=createStreamContentProjection(readState);
  let markdown=null;
  const fade=createStreamFadeController({
    readState,
    getMarkdown:()=>markdown,
    upsertAnchorProse,
    scrollPinned,
    currentDisplayText:()=>content.segment(),
  });
  markdown=createStreamingMarkdown({
    readState,
    reconnecting:!!options.reconnecting,
    createFadeRenderer:fade.createRenderer,
    enhanceTables:enhanceMarkdownTables,
  });
  const anchorProse=createAnchorProseRenderer({
    createSafeRenderer:markdown.createSafeRenderer,
    createFadeRenderer:fade.createRenderer,
    withoutUnderscoreEmphasis:markdown.rendererWithoutUnderscoreEmphasis,
    shouldFade:fade.shouldUse,
  });

  let _pendingRafHandle=null;
  let _renderPending=false;
  let _lastRenderMs=0;
  let _cachedParsed=null;
  let _cachedParsedText='';
  let _cachedParsedReasoning='';

  function _renderLiveThinking(parsed){
    if(window._showThinking===false){removeThinking();return;}
    const text=(parsed&&parsed.thinkingText)||'';
    if(text||(parsed&&parsed.inThinking)){
      updateLiveThinking(text||'Thinking…');
      return;
    }
    if(!readState().reasoningText) removeThinking();
  }

  function _cancelAnimationFramePendingStreamRender(){
    if(_pendingRafHandle===null) return;
    cancelAnimationFrame(_pendingRafHandle);
    clearTimeout(_pendingRafHandle);
    _pendingRafHandle=null;
    _renderPending=false;
  }

  function _flushPendingSegmentRender(options={}){
    const force=!!options.force;
    const skipAnchorProcessProse=!!options.skipAnchorProcessProse;
    const state=readState();
    const assistantBody=state.assistantBody;
    if(!assistantBody||(!force&&!_renderPending)) return;
    if(_renderPending) _cancelAnimationFramePendingStreamRender();
    const displayText=content.segment(state);
    if(markdown.hasParser()){
      markdown.write(displayText);
    }else if(window.smd){
      assistantBody.innerHTML='';
      markdown.newParser(assistantBody,false);
      if(markdown.hasParser()) markdown.write(displayText);
    }else if(renderMd){
      assistantBody.innerHTML=renderMd(displayText);
    }else{
      assistantBody.innerHTML=esc(displayText);
    }
    if(!skipAnchorProcessProse) upsertAnchorProse(displayText,{sealed:force});
    syncWorklogReasons(state.assistantRow,displayText);
  }

  function _resetAssistantSegment(){
    resetSegmentState();
    markdown.end();
    fade.reset();
  }

  function _scheduleRender(parsed){
    const initialState=readState();
    if(parsed){
      _cachedParsed=parsed;
      _cachedParsedText=initialState.assistantText;
      _cachedParsedReasoning=initialState.liveReasoningText;
    }
    if(_renderPending||initialState.streamFinalized) return;
    _renderPending=true;
    const sinceLastMs=performance.now()-_lastRenderMs;
    const doRender=()=>{
      _pendingRafHandle=null;
      _renderPending=false;
      const state=readState();
      if(state.streamFinalized) return;
      const assistantText=String(state.assistantText||'');
      const liveReasoningText=String(state.liveReasoningText||'');
      const segmentStart=Number(state.segmentStart)||0;
      const assistantBody=state.assistantBody;
      if(typeof window._fixMobileScrollJank==='function') window._fixMobileScrollJank();
      _lastRenderMs=performance.now();
      const currentParsed=_cachedParsed
        &&_cachedParsedText===assistantText
        &&_cachedParsedReasoning===liveReasoningText
        ?_cachedParsed
        :content.parse();
      _cachedParsed=null;
      _renderLiveThinking(currentParsed);
      const displayText=segmentStart===0
        ?currentParsed.displayText
        :content.stripXmlToolCalls(assistantText.slice(segmentStart));
      let anchorProcessText=displayText;
      if(assistantBody){
        if(fade.shouldUse()){
          const caughtUp=fade.renderMarkdown(displayText);
          anchorProcessText=fade.domText()||'';
          if(!caughtUp&&!readState().streamFinalized) setTimeout(()=>_scheduleRender(),33);
        }else{
          assistantBody.classList.remove('stream-fade-active');
          fade.reset();
          if(!markdown.hasParser()&&window.smd) markdown.newParser(assistantBody,false);
          if(markdown.hasParser()) markdown.write(displayText);
          else assistantBody.innerHTML=renderMd?renderMd(displayText):esc(displayText);
        }
        syncWorklogReasons(state.assistantRow,displayText);
      }
      if(anchorProcessText) upsertAnchorProse(anchorProcessText);
      scrollPinned();
      snapshotLiveTurn();
    };
    const frameIntervalMs=fade.shouldUse()?33:66;
    if(sinceLastMs>=frameIntervalMs){
      _pendingRafHandle=requestAnimationFrame(doRender);
    }else{
      _pendingRafHandle=setTimeout(()=>requestAnimationFrame(doRender),frameIntervalMs-sinceLastMs);
    }
  }

  return Object.freeze({
    stripXmlToolCalls:content.stripXmlToolCalls,
    streamDisplay:content.display,
    parseStreamState:content.parse,
    renderLiveThinking:_renderLiveThinking,
    endParser:markdown.end,
    resetFadeState:fade.reset,
    cancelPendingRender:_cancelAnimationFramePendingStreamRender,
    shouldUseLiveProseFade:fade.shouldUse,
    cleanupReduceMotion:fade.cleanupReduceMotion,
    drainFadeBeforeDone:fade.drainBeforeDone,
    flushPendingSegment:_flushPendingSegmentRender,
    resetAssistantSegment:_resetAssistantSegment,
    scheduleRender:_scheduleRender,
    clearAnchorProseIncrementalNode:anchorProse.clear,
  });
}
