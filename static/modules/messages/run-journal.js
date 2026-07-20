// Owns the replay cursor for one live run journal.

export function createStreamRunJournalCursor(options={}){
  let _lastRunJournalSeq=Math.max(0,Number(options.initialSeq)||0);
  let _lastRunJournalEventId=String(options.initialEventId||'');
  const getInflight=typeof options.getInflight==='function'?options.getInflight:()=>null;
  const persist=typeof options.persist==='function'?options.persist:()=>{};

  function _rememberRunJournalCursor(e){
    const raw=String(e&&e.lastEventId||'').trim();
    if(!raw) return;
    const tail=raw.includes(':')?raw.slice(raw.lastIndexOf(':')+1):raw;
    const seq=Number.parseInt(tail,10);
    if(!Number.isFinite(seq)||seq<=_lastRunJournalSeq) return;
    _lastRunJournalSeq=seq;
    _lastRunJournalEventId=raw;
    // Mirror the advanced cursor onto the persisted INFLIGHT entry. A hard
    // reload resumes after this exact journal event instead of replaying the
    // already-rendered prefix over the restored assistant text.
    const inflight=getInflight();
    if(inflight){
      inflight.lastRunJournalSeq=seq;
      inflight.lastRunJournalEventId=raw;
      persist();
    }
  }

  function _runJournalReplayAfterSeq(){
    return Math.max(0,_lastRunJournalSeq||0);
  }

  function _runJournalReplayParams(){
    // `replay=1` documents frontend intent. `after_event_id` keeps the numeric
    // cursor run-aware when a newer stream starts its sequence at one again.
    return `&replay=1&after_seq=${encodeURIComponent(String(_runJournalReplayAfterSeq()))}&after_event_id=${encodeURIComponent(_lastRunJournalEventId||'')}`;
  }

  return Object.freeze({
    remember: _rememberRunJournalCursor,
    replayAfterSeq: _runJournalReplayAfterSeq,
    replayParams: _runJournalReplayParams,
  });
}
