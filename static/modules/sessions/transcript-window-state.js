// One owner for the server-backed transcript window and its pagination lifecycle.
// Session switches reset this state; initial loads and older-history requests update it.
export const INITIAL_MESSAGE_LIMIT = 30;
export const MESSAGE_LIMIT_FALLBACK = 500;

let messagesTruncated = false;
export let _msgLimitMax = MESSAGE_LIMIT_FALLBACK;
let loadingOlder = false;
let oldestIdx = 0;
let messagesGeneration = 0;

export const transcriptWindowState=Object.freeze({
  get messagesTruncated(){ return messagesTruncated; },
  set messagesTruncated(value){ messagesTruncated=!!value; },
  get msgLimitMax(){ return _msgLimitMax; },
  set msgLimitMax(value){ _msgLimitMax=Number(value)||MESSAGE_LIMIT_FALLBACK; },
  get loadingOlder(){ return loadingOlder; },
  set loadingOlder(value){ loadingOlder=!!value; },
  get oldestIdx(){ return oldestIdx; },
  set oldestIdx(value){ oldestIdx=Math.max(0,Number(value)||0); },
  get generation(){ return messagesGeneration; },
  bumpGeneration(){
    messagesGeneration=(messagesGeneration+1)|0;
    return messagesGeneration;
  },
  reset(){
    messagesTruncated=false;
    loadingOlder=false;
    oldestIdx=0;
  },
});
