import { _messageHasReasoningPayload, msgContent } from './assistant-turn-presentation.js';
import { _clearRenderCache } from './navigation.js';
import { S, _clearMessageVirtualHeightCache, clearVisibleMessageRowCache } from './state.js';
import { createRenderSignature, createSessionRenderCache } from '../../session_render_cache.js';

// Session render cache: avoids full markdown+DOM rebuild when switching back
// to a session whose rendered transcript inputs are unchanged.
// Keyed by session_id. Only used on cross-session navigation, never for
// in-session updates (new messages, edits, stream events).
const _sessionHtmlCache=createSessionRenderCache({
  maxEntries:8,
  maxEntryBytes:2*1024*1024,
  maxTotalBytes:8*1024*1024,
});
let _sessionHtmlCacheSid=null; // session_id currently rendered in the DOM
// #5966 (Codex F3): persist which capped Transparent-Stream turns the user has
// revealed, keyed by `${session_id}:${ownerRawIdx}`, so a switch-away/back or a
// normal rebuild does NOT silently re-cap a turn the user already expanded. The
// DOM `data-transparent-earlier-revealed` flag alone is lost across the
// _sessionHtmlCache innerHTML round-trip; this survives it. Reveal also
// invalidates that session's cached HTML so the stored markup isn't stale-capped.
const _transparentRevealedTurns=new Set();
function _transparentRevealKey(sessionId, ownerIdx){
  return String(sessionId||(S.session&&S.session.session_id)||'')+':'+String(ownerIdx);
}
function clearMessageRenderCache(){
  _clearRenderCache();
  _sessionHtmlCache.clear();
  _sessionHtmlCacheSid=null;
  clearVisibleMessageRowCache();
  _clearMessageVirtualHeightCache();
}

function _messageRenderCacheSignature(){
  return createRenderSignature({
    messages:S.messages,
    toolCalls:S.toolCalls,
    session:S.session,
    messageContent:msgContent,
    messageHasReasoningPayload:_messageHasReasoningPayload,
  });
}

export {
  _sessionHtmlCache,
  _sessionHtmlCacheSid,
  _transparentRevealedTurns,
  _transparentRevealKey,
  clearMessageRenderCache,
  _messageRenderCacheSignature,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _sessionHtmlCache: { enumerable: true, get: () => _sessionHtmlCache },
  _sessionHtmlCacheSid: { enumerable: true, get: () => _sessionHtmlCacheSid, set: (value) => { _sessionHtmlCacheSid = value; } },
  _transparentRevealedTurns: { enumerable: true, get: () => _transparentRevealedTurns },
  _transparentRevealKey: { enumerable: true, get: () => _transparentRevealKey, set: (value) => { _transparentRevealKey = value; } },
  clearMessageRenderCache: { enumerable: true, get: () => clearMessageRenderCache, set: (value) => { clearMessageRenderCache = value; } },
  _messageRenderCacheSignature: { enumerable: true, get: () => _messageRenderCacheSignature, set: (value) => { _messageRenderCacheSignature = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
