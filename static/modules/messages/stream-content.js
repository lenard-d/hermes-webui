// Projects raw provider text into the visible live-assistant content model.
// Tool-call markup is transport noise: the server already emits the matching
// tool lifecycle events, so it must never leak into assistant prose.
import { _extractInlineThinkingFromContent } from './core.js';

export function _stripXmlToolCalls(value){
  let text=String(value||'');
  if(!text||!/function_calls|dsml/i.test(text)) return text;
  text=text.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>[\s\S]*?<\/(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls>/gi,'');
  text=text.replace(/<(?:\s*｜\s*DSML\s*[｜|]\s*)?function_calls(?:>|$)[\s\S]*$/i,'');
  text=text.replace(/<\s*｜\s*DSML\s*[｜|]\s*/gi,'');
  return text.trim();
}

export function createStreamContentProjection(readState){
  const read=typeof readState==='function'?readState:()=>({});

  function _parseStreamState(){
    const state=read();
    return _extractInlineThinkingFromContent(
      _stripXmlToolCalls(state.assistantText),
      state.liveReasoningText,
      {streaming:true},
    );
  }

  function _streamDisplay(){
    return _parseStreamState().content;
  }

  function segment(state=read()){
    return Number(state.segmentStart)===0
      ? _parseStreamState().displayText
      : _stripXmlToolCalls(String(state.assistantText||'').slice(Number(state.segmentStart)||0));
  }

  return Object.freeze({
    display:_streamDisplay,
    parse:_parseStreamState,
    segment,
    stripXmlToolCalls:_stripXmlToolCalls,
  });
}
