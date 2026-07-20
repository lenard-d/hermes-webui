// Shared runtime identities for sidebar stream projection and unread
// reconciliation. Behavior lives in the run-state and visit owners.
export const sessionRunRegistry=Object.freeze({
  streamingById:new Map(),
  snapshotById:new Map(),
  sourceById:new Map(),
});

const _sessionStreamingById=sessionRunRegistry.streamingById;
const _sessionListSnapshotById=sessionRunRegistry.snapshotById;

export function _syncSessionListSnapshotOnVisit(sid, messageCount, lastMessageAt){
  if(!sid) return;
  const count=Number(messageCount||0);
  const last=Number(lastMessageAt||0);
  _sessionListSnapshotById.set(sid,{message_count:count,last_message_at:last});
  // The target session's own metadata is authoritative. Global busy state may
  // still describe the session we just left during an asynchronous switch.
  const target=(S.session&&S.session.session_id===sid)?S.session:null;
  const isStreaming=Boolean(target&&(
    target.is_streaming||
    target.active_stream_id||
    target.pending_user_message||
    target.has_pending_user_message
  ));
  _sessionStreamingById.set(sid,isStreaming);
  return isStreaming;
}
