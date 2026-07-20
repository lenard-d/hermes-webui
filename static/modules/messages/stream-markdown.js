// Owns the incremental smd parser lifecycle for one live stream. It keeps
// parser identity, written-prefix recovery, URL safety, MEDIA tails, KaTeX
// scheduling, and terminal sanitization behind one small interface.
import {
  __SMD_PARSER_FALLBACK,
  _smdAppendPlainText,
  _smdBindParserIdentity,
  _smdClearParserIdentity,
  _smdMediaTailClear,
  _smdMediaTailFlush,
  createSmdMediaTextWriter,
} from './stream-media.js';
import { installSafeSmdAttributes, _sanitizeSmdLinks } from './stream-link-policy.js';

export function createStreamingMarkdown(options={}){
  const readState=typeof options.readState==='function'?options.readState:()=>({});
  const createFadeRenderer=typeof options.createFadeRenderer==='function'?options.createFadeRenderer:null;
  const enhanceTables=typeof options.enhanceTables==='function'?options.enhanceTables:()=>{};
  let _smdParser=null;
  let _smdWrittenText='';
  let _streamingKatexTimer=null;
  let _smdReconnect=!!options.reconnecting;

  function _safeSmdRenderer(element){
    const renderer=window.smd.default_renderer(element);
    const baseAddText=renderer.add_text;
    const writePlainText=(parent,data,text)=>_smdAppendPlainText(parent,data,text,baseAddText);
    renderer.add_text=createSmdMediaTextWriter(baseAddText,element,writePlainText);
    return installSafeSmdAttributes(renderer);
  }

  function _smdRendererWithoutUnderscoreEmphasis(renderer){
    if(!renderer||!window.smd) return renderer;
    const baseAddToken=renderer.add_token;
    const baseEndToken=renderer.end_token;
    const baseAddText=renderer.add_text;
    const tokenStack=[];
    renderer.add_token=(data,token)=>{
      if(token===window.smd.ITALIC_UND||token===window.smd.STRONG_UND){
        const marker=token===window.smd.STRONG_UND?'__':'_';
        tokenStack.push(marker);
        baseAddText(data,marker);
        return;
      }
      tokenStack.push(null);
      baseAddToken(data,token);
    };
    renderer.end_token=data=>{
      const marker=tokenStack.pop();
      if(marker){baseAddText(data,marker);return;}
      baseEndToken(data);
    };
    return renderer;
  }

  function _smdNewParser(element,fade=false){
    _smdWrittenText='';
    if(!window.smd){_smdParser=null;return;}
    if(_smdReconnect&&element){element.innerHTML='';_smdReconnect=false;}
    const baseRenderer=fade&&createFadeRenderer?createFadeRenderer(element):_safeSmdRenderer(element);
    const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);
    _smdParser=window.smd.parser(renderer);
    _smdBindParserIdentity(renderer,_smdParser,element);
  }

  function _scheduleStreamingKatex(){
    if(_streamingKatexTimer) return;
    _streamingKatexTimer=setTimeout(()=>{
      _streamingKatexTimer=null;
      const assistantBody=readState().assistantBody;
      if(assistantBody&&typeof renderKatexBlocks==='function') renderKatexBlocks(assistantBody,{streaming:true});
    },150);
  }

  function _smdWrite(displayText,fade=false){
    const assistantBody=readState().assistantBody;
    if(!_smdParser||!window.smd) return;
    const value=String(displayText||'');
    if(_smdWrittenText&&!value.startsWith(_smdWrittenText)){
      const staleParser=_smdParser;
      _smdParser=null;
      _smdWrittenText='';
      _smdMediaTailClear(staleParser);
      _smdClearParserIdentity(assistantBody,staleParser);
      if(assistantBody) assistantBody.innerHTML='';
      _smdNewParser(assistantBody,fade);
      if(!_smdParser) return;
    }
    const delta=value.slice(_smdWrittenText.length);
    if(!delta) return;
    try{window.smd.parser_write(_smdParser,delta);}catch(_){}
    _smdWrittenText=value;
    _scheduleStreamingKatex();
  }

  function _smdEndParser(){
    const assistantBody=readState().assistantBody;
    if(_streamingKatexTimer){clearTimeout(_streamingKatexTimer);_streamingKatexTimer=null;}
    const parser=_smdParser;
    if(parser&&window.smd){
      try{window.smd.parser_end(parser);}catch(_){}
    }
    _smdMediaTailFlush(parser);
    _smdMediaTailFlush(__SMD_PARSER_FALLBACK);
    if(assistantBody){_sanitizeSmdLinks(assistantBody);enhanceTables(assistantBody);}
    _smdMediaTailClear(parser);
    _smdMediaTailClear(__SMD_PARSER_FALLBACK);
    _smdClearParserIdentity(assistantBody,parser);
    _smdParser=null;
    _smdWrittenText='';
  }

  return Object.freeze({
    createSafeRenderer:_safeSmdRenderer,
    end:_smdEndParser,
    hasParser:()=>!!_smdParser,
    newParser:_smdNewParser,
    parser:()=>_smdParser,
    rendererWithoutUnderscoreEmphasis:_smdRendererWithoutUnderscoreEmphasis,
    write:_smdWrite,
  });
}
