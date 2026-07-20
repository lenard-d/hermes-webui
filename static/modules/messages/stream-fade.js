// Optional word-fade playout for live prose. This module owns motion-policy
// listeners, reveal pacing, fade DOM spans, and the bounded terminal drain.
import {
  _SMD_MEDIA_TAIL,
  _smdAppendPlainText,
  _smdMediaAwareAddText,
  _smdMediaPrefixTail,
  _smdParserKey,
} from './stream-media.js';
import { installSafeSmdAttributes, _sanitizeSmdLinks } from './stream-link-policy.js';

const _STREAM_FADE_MS=620;
const _STREAM_FADE_MAX_MS=900;
const _STREAM_FADE_DONE_MAX_MS=1000;
const _STREAM_FADE_DONE_DRAIN_MAX_MS=1400;

export function createStreamFadeController(options={}){
  const readState=typeof options.readState==='function'?options.readState:()=>({});
  const getMarkdown=typeof options.getMarkdown==='function'?options.getMarkdown:()=>null;
  const isTransparent=typeof options.isTransparent==='function'
    ?options.isTransparent
    :()=>typeof isTransparentStream==='function'&&isTransparentStream();
  const upsertAnchorProse=typeof options.upsertAnchorProse==='function'?options.upsertAnchorProse:()=>{};
  const scrollPinned=typeof options.scrollPinned==='function'?options.scrollPinned:()=>{};
  const currentDisplayText=typeof options.currentDisplayText==='function'?options.currentDisplayText:()=>'';

  let _streamFadeVisibleText='';
  let _streamFadeLastTickMs=0;
  let _streamFadeWordCarry=0;
  let _streamFadeStartedAt=0;
  let _streamFadeLastTargetWords=0;
  let _streamFadeLastArrivalMs=0;
  let _streamFadeArrivalWps=0;
  let _streamFadeLatestAnimationEndAt=0;
  let _streamFadeVisibleWords=0;
  let _streamFadeHoldUntilMs=0;
  let _streamFadeCurrentMs=_STREAM_FADE_MS;
  let _streamFadeDomText='';
  let _streamFadeReduceMotionMql=null;
  let _streamFadeReduceMotion=false;
  let _streamFadeReduceMotionOnChange=null;

  function _resetStreamFadeState(){
    _streamFadeVisibleText='';
    _streamFadeLastTickMs=0;
    _streamFadeWordCarry=0;
    _streamFadeStartedAt=0;
    _streamFadeLastTargetWords=0;
    _streamFadeLastArrivalMs=0;
    _streamFadeArrivalWps=0;
    _streamFadeLatestAnimationEndAt=0;
    _streamFadeVisibleWords=0;
    _streamFadeHoldUntilMs=0;
    _streamFadeCurrentMs=_STREAM_FADE_MS;
    _streamFadeDomText='';
  }

  function _shouldUseStreamFade(){
    return window._fadeTextEffect===true;
  }

  function _shouldUseTransparentStreamFade(){
    return isTransparent();
  }

  function _shouldUseLiveProseFade(){
    return !_streamFadeReduceMotionEnabled()&&(_shouldUseStreamFade()||_shouldUseTransparentStreamFade());
  }

  function _streamFadeSkipNode(node){
    if(!node||node.nodeType!==1) return false;
    const tag=(node.tagName||'').toLowerCase();
    return tag==='pre'||tag==='code'||tag==='script'||tag==='style'||tag==='textarea'||tag==='svg'||tag==='math';
  }

  function _streamFadeReduceMotionEnabled(){
    if(!window.matchMedia) return false;
    if(!_streamFadeReduceMotionMql){
      _streamFadeReduceMotionMql=window.matchMedia('(prefers-reduced-motion: reduce)');
      _streamFadeReduceMotion=!!_streamFadeReduceMotionMql.matches;
      _streamFadeReduceMotionOnChange=event=>{_streamFadeReduceMotion=!!event.matches;};
      try{_streamFadeReduceMotionMql.addEventListener('change',_streamFadeReduceMotionOnChange);}
      catch(_){try{_streamFadeReduceMotionMql.addListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    }
    return _streamFadeReduceMotion;
  }

  function _streamFadeCleanupReduceMotionListener(){
    if(!_streamFadeReduceMotionMql||!_streamFadeReduceMotionOnChange) return;
    try{_streamFadeReduceMotionMql.removeEventListener('change',_streamFadeReduceMotionOnChange);}
    catch(_){try{_streamFadeReduceMotionMql.removeListener(_streamFadeReduceMotionOnChange);}catch(_){}}
    _streamFadeReduceMotionMql=null;
    _streamFadeReduceMotionOnChange=null;
  }

  function _streamFadeBindCleanup(element){
    if(!element||element._streamFadeCleanupBound) return;
    element._streamFadeCleanupBound=true;
    element.addEventListener('animationend',event=>{
      const span=event.target;
      if(!span||!span.classList||!span.classList.contains('stream-fade-word')) return;
      span.replaceWith(document.createTextNode(span.textContent||''));
    });
  }

  function _streamFadeAppendText(element,text){
    if(!element) return;
    const value=String(text||'');
    if(!value) return;
    const reduceMotion=_streamFadeReduceMotionEnabled();
    const fragment=document.createDocumentFragment();
    const wordRe=/(\S+)(\s*)/g;
    const appendStartedAt=performance.now();
    let last=0;
    let match;
    let changed=false;
    while((match=wordRe.exec(value))){
      if(match.index>last) fragment.appendChild(document.createTextNode(value.slice(last,match.index)));
      if(reduceMotion){
        fragment.appendChild(document.createTextNode(match[1]));
      }else{
        const span=document.createElement('span');
        span.className='stream-fade-word is-new';
        const fadeMs=_streamFadeCurrentMs||_STREAM_FADE_MS;
        if(fadeMs!==_STREAM_FADE_MS) span.style.setProperty('--stream-fade-ms',fadeMs+'ms');
        span.textContent=match[1];
        fragment.appendChild(span);
        _streamFadeLatestAnimationEndAt=Math.max(_streamFadeLatestAnimationEndAt,appendStartedAt+fadeMs);
      }
      if(match[2]) fragment.appendChild(document.createTextNode(match[2]));
      last=match.index+match[0].length;
      changed=true;
    }
    if(!changed) fragment.appendChild(document.createTextNode(value));
    else if(last<value.length) fragment.appendChild(document.createTextNode(value.slice(last)));
    element.appendChild(fragment);
  }

  function _streamFadeRenderer(element){
    _streamFadeBindCleanup(element);
    const renderer=window.smd.default_renderer(element);
    const baseAddText=renderer.add_text;
    const writeFadeText=(parent,data,text)=>{
      if(!parent||_streamFadeSkipNode(parent)){
        _smdAppendPlainText(parent,data,text,baseAddText);
        return;
      }
      _streamFadeAppendText(parent,text);
    };
    renderer.add_text=(data,text)=>{
      const parent=data&&data.nodes&&data.nodes[data.index];
      if(!parent||_streamFadeSkipNode(parent)){baseAddText(data,text);return;}
      const parser=_smdParserKey(data,element);
      const value=String(text||'');
      const hasMediaTail=!!(_SMD_MEDIA_TAIL&&parser&&_SMD_MEDIA_TAIL.has&&_SMD_MEDIA_TAIL.has(parser));
      if(/MEDIA:/.test(value)||hasMediaTail||_smdMediaPrefixTail(value)){
        _smdMediaAwareAddText(baseAddText,parent,data,text,undefined,parser,writeFadeText);
        return;
      }
      _streamFadeAppendText(parent,value);
    };
    return installSafeSmdAttributes(renderer);
  }

  function _streamFadeWordCountOf(text){
    const matches=String(text||'').match(/\S+/g);
    return matches?matches.length:0;
  }

  function _streamFadePauseAfter(text,paragraphBreakIndex){
    if(paragraphBreakIndex>=0) return 90;
    const trimmed=String(text||'').trimEnd();
    if(/[.!?]["\x27)\]]*$/.test(trimmed)) return 45;
    if(/[:;]["\x27)\]]*$/.test(trimmed)) return 30;
    return 0;
  }

  function _streamFadeNextText(targetText){
    targetText=String(targetText||'');
    const now=performance.now();
    if(!targetText){
      const hadVisible=!!_streamFadeVisibleText;
      _resetStreamFadeState();
      return {text:'',caughtUp:true,changed:hadVisible};
    }
    if(!_streamFadeVisibleText||!targetText.startsWith(_streamFadeVisibleText)) _resetStreamFadeState();
    if(!_streamFadeLastTickMs){_streamFadeLastTickMs=now;_streamFadeStartedAt=now;}
    if(_streamFadeVisibleText===targetText) return {text:_streamFadeVisibleText,caughtUp:true,changed:false};

    const remaining=targetText.slice(_streamFadeVisibleText.length);
    const backlogWords=_streamFadeWordCountOf(remaining);
    const targetWords=_streamFadeVisibleWords+backlogWords;
    const elapsedMs=Math.max(16,Math.min(120,now-_streamFadeLastTickMs));
    _streamFadeLastTickMs=now;
    if(!_streamFadeLastArrivalMs){
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    }else if(targetWords>_streamFadeLastTargetWords){
      const arrivalElapsedMs=Math.max(16,now-_streamFadeLastArrivalMs);
      const instantArrivalWps=(targetWords-_streamFadeLastTargetWords)*1000/arrivalElapsedMs;
      _streamFadeArrivalWps=_streamFadeArrivalWps?_streamFadeArrivalWps*0.65+instantArrivalWps*0.35:instantArrivalWps;
      _streamFadeLastArrivalMs=now;
      _streamFadeLastTargetWords=targetWords;
    }else if(targetWords<_streamFadeLastTargetWords){
      _streamFadeLastTargetWords=targetWords;
      _streamFadeLastArrivalMs=now;
      _streamFadeArrivalWps=0;
    }
    if(now<_streamFadeHoldUntilMs) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};

    const streamAgeSeconds=Math.max(0,(now-(_streamFadeStartedAt||now))/1000);
    const baseWps=22+Math.min(streamAgeSeconds*2.5,28);
    const arrivalWps=_streamFadeArrivalWps?Math.min(_streamFadeArrivalWps*1.05+8,160):0;
    const backlogWps=backlogWords>0?Math.min(22+backlogWords*1.1,160):0;
    const wordsPerSecond=Math.min(160,Math.max(baseWps,arrivalWps,backlogWps));
    const speedFadeRatio=Math.max(0,Math.min(1,(wordsPerSecond-50)/110));
    _streamFadeCurrentMs=Math.round(_STREAM_FADE_MS+(_STREAM_FADE_MAX_MS-_STREAM_FADE_MS)*speedFadeRatio);
    _streamFadeWordCarry+=elapsedMs*wordsPerSecond/1000;
    if(!_streamFadeVisibleText) _streamFadeWordCarry=Math.max(_streamFadeWordCarry,1);
    let wordsToReveal=Math.floor(_streamFadeWordCarry);
    wordsToReveal=Math.min(wordsToReveal,backlogWords>=160?3:2,backlogWords);
    if(wordsToReveal<1) return {text:_streamFadeVisibleText,caughtUp:false,changed:false};
    _streamFadeWordCarry=Math.max(0,_streamFadeWordCarry-wordsToReveal);

    let cut=0;
    const wordRe=/(\s*\S+\s*)/g;
    let match;
    while(wordsToReveal>0&&(match=wordRe.exec(remaining))){cut=wordRe.lastIndex;wordsToReveal-=1;}
    if(cut<=0) cut=Math.min(remaining.length,4);
    const chunk=remaining.slice(0,cut);
    const paragraphMatch=chunk.match(/\n\s*\n/);
    const paragraphBreak=paragraphMatch?paragraphMatch.index:-1;
    if(paragraphMatch) cut=paragraphBreak+paragraphMatch[0].length;
    const revealed=remaining.slice(0,cut);
    _streamFadeVisibleText+=revealed;
    _streamFadeVisibleWords+=_streamFadeWordCountOf(revealed);
    const pauseMs=_streamFadePauseAfter(revealed,paragraphBreak);
    if(pauseMs) _streamFadeHoldUntilMs=now+pauseMs;
    if(_streamFadeVisibleText.length>targetText.length) _streamFadeVisibleText=targetText;
    return {text:_streamFadeVisibleText,caughtUp:_streamFadeVisibleText===targetText,changed:true};
  }

  function _renderStreamingFadeMarkdown(displayText){
    const assistantBody=readState().assistantBody;
    if(!assistantBody) return true;
    const next=_streamFadeNextText(displayText);
    if(!next.changed) return next.caughtUp;
    assistantBody.classList.add('stream-fade-active');
    const markdown=getMarkdown();
    if(!_shouldUseTransparentStreamFade()){
      if(markdown&&!markdown.hasParser()&&window.smd) markdown.newParser(assistantBody,true);
      if(markdown&&markdown.hasParser()) markdown.write(next.text,true);
      else{
        assistantBody.innerHTML=renderMd?renderMd(next.text||''):esc(next.text||'');
        _sanitizeSmdLinks(assistantBody);
      }
      _streamFadeDomText=String(next.text||'');
      return next.caughtUp;
    }
    if(markdown&&markdown.hasParser()){
      markdown.end();
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    if(!_streamFadeDomText&&assistantBody.textContent) assistantBody.textContent='';
    if(!String(next.text||'').startsWith(_streamFadeDomText)){
      assistantBody.textContent='';
      _streamFadeDomText='';
    }
    const delta=String(next.text||'').slice(_streamFadeDomText.length);
    if(delta) assistantBody.appendChild(document.createTextNode(delta));
    _streamFadeDomText=String(next.text||'');
    return next.caughtUp;
  }

  function _drainStreamFadeBeforeDone(onDone){
    const drainStartedAt=performance.now();
    let forcedDone=false;
    const step=()=>{
      const assistantBody=readState().assistantBody;
      if(!assistantBody){onDone();return;}
      const target=currentDisplayText();
      const caughtUp=_renderStreamingFadeMarkdown(target);
      const anchorProcessText=_streamFadeDomText||target;
      if(anchorProcessText) upsertAnchorProse(anchorProcessText);
      scrollPinned();
      const markdown=getMarkdown();
      if(caughtUp){
        if(markdown&&markdown.hasParser()) markdown.end();
        const remainingAnimationMs=Math.max(_STREAM_FADE_MS,_streamFadeLatestAnimationEndAt-performance.now());
        setTimeout(onDone,Math.min(remainingAnimationMs,_STREAM_FADE_DONE_MAX_MS));
        return;
      }
      if(!forcedDone&&performance.now()-drainStartedAt>=_STREAM_FADE_DONE_DRAIN_MAX_MS){
        forcedDone=true;
        if(markdown&&markdown.hasParser()) markdown.end();
        onDone();
        return;
      }
      setTimeout(()=>requestAnimationFrame(step),33);
    };
    step();
  }

  return Object.freeze({
    cleanupReduceMotion:_streamFadeCleanupReduceMotionListener,
    createRenderer:_streamFadeRenderer,
    drainBeforeDone:_drainStreamFadeBeforeDone,
    domText:()=>_streamFadeDomText,
    renderMarkdown:_renderStreamingFadeMarkdown,
    reset:_resetStreamFadeState,
    shouldUse:_shouldUseLiveProseFade,
  });
}
