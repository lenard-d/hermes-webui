// ─── Todo state: single source of truth + render scheduling ─────────────────
//
import { INFLIGHT, S, esc } from './state.js';

// Three concerns live together so they can share state cleanly:
//
//   1. _todosHash(items)  — cheap content fingerprint; skips re-render when
//      a snapshot would paint the same DOM.  Used both as a short-circuit
//      and as the hash that compares "rendered vs current" snapshots.
//
//   2. scheduleTodosRefresh() — coalesces multiple `todo_state` events that
//      land in the same animation frame into a single refresh pass. It keeps
//      the left sidebar Todos behavior unchanged, and also lets the workspace
//      Todos tab repaint when that tab is enabled and currently visible.
//
//   3. _hydrateTodosFromSession(session) — applies cold-load todo_state
//      from the session GET payload, or clears the panel when neither a
//      cold-load nor an INFLIGHT signal is available.  Called at every
//      `S.session = ...` settle point so cross-session navigation never
//      leaves a stale list visible.
//
// The hash is keyed on (id, content/text, status); the render itself uses
// `esc()` for any user-controlled string, so XSS surface is the same as
// any other innerHTML path in this file.
let _todosLastRenderedHash=null;
let _todosRenderRafId=0;

function _todosHash(items){
  if(!Array.isArray(items)) return '';
  // String concat outperforms JSON.stringify on small arrays in V8 (no
  // intermediate object allocation) and is exact enough — the field set
  // matches what the renderer reads, so any visible change in DOM
  // implies a hash change.  Field separators (\x1f, \x1e) are control
  // chars unlikely to appear in real todo content, so collisions across
  // boundaries are not realistic.
  let h=items.length+'|';
  for(let i=0;i<items.length;i++){
    const t=items[i]||{};
    const content=t.content==null?(t.text==null?'':t.text):t.content;
    h+=String(t.id==null?'':t.id)+'\x1f'+String(content)+'\x1f'+String(t.status==null?'':t.status)+'\x1e';
  }
  return h;
}

const TODO_STATUS_RENDERING=Object.freeze({
  pending:Object.freeze({icon:'square',color:'var(--muted)'}),
  in_progress:Object.freeze({icon:'loader',color:'var(--blue)'}),
  completed:Object.freeze({icon:'check',color:'rgba(100,200,100,.8)'}),
  cancelled:Object.freeze({icon:'x',color:'rgba(200,100,100,.5)'}),
});

function todoStatusKey(status){
  const key=String(status||'pending');
  return Object.prototype.hasOwnProperty.call(TODO_STATUS_RENDERING,key)?key:'pending';
}

function todoStatusVisual(status){
  const key=todoStatusKey(status);
  return TODO_STATUS_RENDERING[key];
}

function renderTodoStatusIcon(status,size=14){
  const visual=todoStatusVisual(status);
  return typeof li==='function'?li(visual.icon,size):'';
}

function todoContent(todo){
  if(!todo) return '';
  return todo.content==null?(todo.text==null?'':todo.text):todo.content;
}

function renderTodoEmptyState(options={}){
  const centered=!!(options&&options.centered);
  const style=centered
    ? 'padding:24px 12px;text-align:center;color:var(--muted);font-size:12px'
    : 'color:var(--muted);font-size:12px;padding:4px 0';
  return `<div style="${style}">${esc(t('todos_no_active'))}</div>`;
}

function renderTodoRow(todo,options={}){
  const td=todo||{};
  const status=todoStatusKey(td.status);
  const visual=todoStatusVisual(status);
  const showMetadata=!(options&&options.metadata===false);
  const isCompleted=status==='completed';
  const isCancelled=status==='cancelled';
  const contentColor=(isCompleted||isCancelled)?'var(--muted)':'var(--text)';
  const completedStyle=(isCompleted||isCancelled)?'text-decoration:line-through;opacity:.5':'';
  const metadata=showMetadata
    ? `<div style="font-size:10px;color:var(--muted);margin-top:2px;opacity:.6">${esc(td.id)} · ${esc(status)}</div>`
    : '';
  return `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:6px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:14px;display:inline-flex;align-items:center;flex-shrink:0;margin-top:1px;color:${visual.color}">${renderTodoStatusIcon(status,14)}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;color:${contentColor};${completedStyle};line-height:1.4">${esc(todoContent(td))}</div>
        ${metadata}
      </div>
    </div>`;
}

function renderTodoRows(todos,options={}){
  const items=Array.isArray(todos)?todos:[];
  return items.map(td=>renderTodoRow(td,options)).join('');
}

function _todosPanelIsActive(){
  if(typeof document==='undefined') return false;
  const panel=document.getElementById('panelTodos');
  return !!(panel&&panel.classList&&panel.classList.contains('active'));
}

function scheduleTodosRefresh(){
  // Idempotent: many `todo_state` events fire on each tool result, but
  // only the latest snapshot needs to paint.  RAF lets us coalesce
  // without timer drift.
  if(_todosRenderRafId) return;
  if(typeof requestAnimationFrame!=='function'){
    if(typeof loadTodos==='function') loadTodos();
    if(typeof _refreshWorkspacePanelTodos==='function') _refreshWorkspacePanelTodos();
    return;
  }
  _todosRenderRafId=requestAnimationFrame(()=>{
    _todosRenderRafId=0;
    const sidebarActive=_todosPanelIsActive();
    if(sidebarActive&&typeof loadTodos==='function') loadTodos();
    if(typeof _refreshWorkspacePanelTodos==='function') _refreshWorkspacePanelTodos();
  });
}

function _resetTodosRenderCache(){
  // Clear after every cross-session navigation so the next render is
  // never short-circuited against a hash from a different session.
  _todosLastRenderedHash=null;
  if(typeof _resetWorkspaceTodosRenderCache==='function') _resetWorkspaceTodosRenderCache();
}

function _hydrateTodosFromSession(session){
  // Three input cases, three deterministic outcomes:
  //   a) cold-load AND inflight both present  → pick newer by ts so a
  //      stale cold-load from the session GET cannot regress a fresher
  //      INFLIGHT snapshot persisted from a still-running stream
  //      (avoids visible rollback on reload).
  //   b) only one of cold-load / inflight is present  → use it.
  //   c) neither  → reset to empty + sentinel so loadTodos() falls
  //      through to the legacy reverse-scan or paints the empty state.
  const sid=(session&&session.session_id)||'';
  const inflight=(typeof INFLIGHT==='object'&&INFLIGHT&&sid)?INFLIGHT[sid]:null;
  const cold=session&&session.todo_state;
  const coldOk=!!(cold&&Array.isArray(cold.todos));
  const inflightOk=!!(inflight&&Array.isArray(inflight.todos)&&inflight.todoStateMeta);
  const coldTs=coldOk?(Number(cold.ts)||0):0;
  const inflightTs=inflightOk?(Number(inflight.todoStateMeta&&inflight.todoStateMeta.ts)||0):0;
  // Whether a live stream currently owns this session. This is the signal
  // that disambiguates a ts-less cold-load (see below); it comes from the
  // session GET payload (mirrors sessions.js `S.session.active_stream_id`).
  const streamActive=!!(session&&session.active_stream_id);
  if(coldOk&&inflightOk){
    // Reconcile the server's settled cold-load snapshot against the
    // locally-persisted INFLIGHT snapshot.
    //
    // coldTs===0 means the cold-load carries NO usable timestamp, so we
    // cannot order it against INFLIGHT by recency. A todo tool message can
    // legitimately lose its `timestamp` during context compression/rebuild
    // (the on-disk message ends up timestamp=None), and derive_todo_state
    // (api/todo_state.py) then returns the correct latest-by-POSITION todos
    // but omits `ts`. The tie-break depends on who owns the INFLIGHT tail:
    //
    //   - stream ACTIVE → INFLIGHT is the live tail. The most recent todo
    //     write may still be in flight and not yet settled into the message
    //     list derive_todo_state scans, so a ts-less cold-load can be an
    //     OLDER (pre-latest-write) view. Letting cold win here rolls the
    //     panel back to a stale list, and since the stream may have just
    //     ended on that very write there is no guaranteed forward SSE event
    //     to self-heal. So prefer INFLIGHT. If cold is in fact newer, the
    //     reattach replay (sessions.js attachLiveStream, reconnecting) re-
    //     emits the journaled `todo_state` events which reconcile forward by
    //     ts, so any transient discrepancy corrects itself.
    //
    //   - stream IDLE → INFLIGHT is leftover from a finished/crashed stream
    //     (idle sessions purge it shortly after, sessions.js), and there is
    //     no replay to correct anything. The settled cold-load is the
    //     authoritative latest-by-position view, so prefer cold. This also
    //     preserves the original fix for the "shows an old todo list" bug,
    //     where a stale prior-turn INFLIGHT must not beat a ts-less cold-load.
    //
    // When coldTs>0 the original recency rule stands: strict ">", and on a
    // tie prefer INFLIGHT for the freshest in-tab edits.
    const coldWins=(coldTs===0)?(!streamActive):(coldTs>inflightTs);
    if(coldWins){
      S.todos=cold.todos;
      S.todoStateMeta={
        ts:coldTs,
        source:'cold-load',
        version:Number(cold.version)||1,
      };
    }else{
      S.todos=inflight.todos;
      S.todoStateMeta=inflight.todoStateMeta;
    }
  }else if(coldOk){
    S.todos=cold.todos;
    S.todoStateMeta={
      ts:coldTs,
      source:'cold-load',
      version:Number(cold.version)||1,
    };
  }else if(inflightOk){
    S.todos=inflight.todos;
    S.todoStateMeta=inflight.todoStateMeta;
  }else{
    S.todos=[];
    S.todoStateMeta=null;
  }
  _resetTodosRenderCache();
  if(typeof scheduleTodosRefresh==='function') scheduleTodosRefresh();
}

export {
  TODO_STATUS_RENDERING,
  _todosLastRenderedHash,
  _todosRenderRafId,
  _todosHash,
  todoStatusKey,
  todoStatusVisual,
  renderTodoStatusIcon,
  todoContent,
  renderTodoEmptyState,
  renderTodoRow,
  renderTodoRows,
  _todosPanelIsActive,
  scheduleTodosRefresh,
  _resetTodosRenderCache,
  _hydrateTodosFromSession,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  TODO_STATUS_RENDERING: { enumerable: true, get: () => TODO_STATUS_RENDERING },
  _todosLastRenderedHash: { enumerable: true, get: () => _todosLastRenderedHash, set: value => { _todosLastRenderedHash = value; } },
  _todosRenderRafId: { enumerable: true, get: () => _todosRenderRafId, set: value => { _todosRenderRafId = value; } },
  _todosHash: { enumerable: true, get: () => _todosHash, set: value => { _todosHash = value; } },
  todoStatusKey: { enumerable: true, get: () => todoStatusKey, set: value => { todoStatusKey = value; } },
  todoStatusVisual: { enumerable: true, get: () => todoStatusVisual, set: value => { todoStatusVisual = value; } },
  renderTodoStatusIcon: { enumerable: true, get: () => renderTodoStatusIcon, set: value => { renderTodoStatusIcon = value; } },
  todoContent: { enumerable: true, get: () => todoContent, set: value => { todoContent = value; } },
  renderTodoEmptyState: { enumerable: true, get: () => renderTodoEmptyState, set: value => { renderTodoEmptyState = value; } },
  renderTodoRow: { enumerable: true, get: () => renderTodoRow, set: value => { renderTodoRow = value; } },
  renderTodoRows: { enumerable: true, get: () => renderTodoRows, set: value => { renderTodoRows = value; } },
  _todosPanelIsActive: { enumerable: true, get: () => _todosPanelIsActive, set: value => { _todosPanelIsActive = value; } },
  scheduleTodosRefresh: { enumerable: true, get: () => scheduleTodosRefresh, set: value => { scheduleTodosRefresh = value; } },
  _resetTodosRenderCache: { enumerable: true, get: () => _resetTodosRenderCache, set: value => { _resetTodosRenderCache = value; } },
  _hydrateTodosFromSession: { enumerable: true, get: () => _hydrateTodosFromSession, set: value => { _hydrateTodosFromSession = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
