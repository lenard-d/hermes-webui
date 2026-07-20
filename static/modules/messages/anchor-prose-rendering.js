// Persistent incremental markdown nodes for live Anchor-scene prose.
// Each cache entry owns its parser; the DOM is a projection target and never
// becomes canonical transcript state.
import {
  _smdBindParserIdentity,
  _smdMediaTailClear,
  _smdMediaTailFlush,
} from './stream-media.js';

export function createAnchorProseRenderer(options={}){
  const createSafeRenderer=options.createSafeRenderer;
  const createFadeRenderer=options.createFadeRenderer;
  const withoutUnderscoreEmphasis=options.withoutUnderscoreEmphasis;
  const shouldFade=typeof options.shouldFade==='function'?options.shouldFade:()=>false;
  const _anchorProseSmdCache=new Map();

  function _disposeAnchorProseState(state){
    if(!state||!state.parser) return;
    try{window.smd&&window.smd.parser_end(state.parser);}catch(_){}
    _smdMediaTailFlush(state.parser);
    _smdMediaTailClear(state.parser);
  }

  function _dropAnchorProseState(key,state){
    _anchorProseSmdCache.delete(key);
    _disposeAnchorProseState(state);
  }

  function _anchorProseIncrementalNode(key,text){
    if(!window.smd||!key||typeof createSafeRenderer!=='function') return null;
    const value=String(text||'');
    const fade=shouldFade();
    try{
      let state=_anchorProseSmdCache.get(key);
      if(state&&((state.writtenText&&!value.startsWith(state.writtenText))||state.fade!==fade)){
        _dropAnchorProseState(key,state);
        state=null;
      }
      if(!state){
        const node=document.createElement('div');
        node.className='assistant-segment';
        node.setAttribute('data-anchor-scene-prose','1');
        const body=document.createElement('div');
        body.className='msg-body';
        if(body.classList) body.classList.toggle('stream-fade-active',fade);
        node.appendChild(body);
        const baseRenderer=fade&&typeof createFadeRenderer==='function'
          ?createFadeRenderer(body)
          :createSafeRenderer(body);
        const renderer=withoutUnderscoreEmphasis(baseRenderer);
        state={node,parser:window.smd.parser(renderer),writtenText:'',fade};
        _smdBindParserIdentity(renderer,state.parser,body);
        _anchorProseSmdCache.set(key,state);
        if(_anchorProseSmdCache.size>32){
          const oldest=_anchorProseSmdCache.keys().next().value;
          if(oldest!==key) _dropAnchorProseState(oldest,_anchorProseSmdCache.get(oldest));
        }
      }
      const body=state.node&&state.node.querySelector&&state.node.querySelector('.msg-body');
      if(body&&body.classList) body.classList.toggle('stream-fade-active',fade);
      const delta=value.slice(state.writtenText.length);
      if(delta){
        window.smd.parser_write(state.parser,delta);
        state.writtenText=value;
      }
      state.node.dataset.rawText=value;
      return state.node;
    }catch(_){
      _dropAnchorProseState(key,_anchorProseSmdCache.get(key));
      return null;
    }
  }

  window.__anchorProseIncrementalNode=_anchorProseIncrementalNode;

  function _clearAnchorProseIncrementalNode(){
    if(typeof window!=='undefined'&&window.__anchorProseIncrementalNode===_anchorProseIncrementalNode){
      window.__anchorProseIncrementalNode=null;
    }
    _anchorProseSmdCache.forEach(state=>_disposeAnchorProseState(state));
    _anchorProseSmdCache.clear();
  }

  return Object.freeze({clear:_clearAnchorProseIncrementalNode});
}
