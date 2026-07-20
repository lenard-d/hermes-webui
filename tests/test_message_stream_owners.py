import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_node(source: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        return subprocess.CompletedProcess([], 0, "", "")
    return subprocess.run(
        [node, "--input-type=module", "-e", source],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_stream_progress_owner_coalesces_and_cancels_pending_work():
    result = _run_node(
        r"""
import {createStreamProgressOwner} from './static/modules/messages/stream-progress.js';

const inflight={
  messages:[{role:'assistant',content:'partial'}],
  uploaded:['report.txt'],
  toolCalls:[{name:'terminal'}],
  lastAssistantText:'partial',
  lastReasoningText:'reasoning',
  lastRunJournalSeq:7,
  lastRunJournalEventId:'stream-1:7',
  journalReplayFromStart:true,
  anchorActivityScene:{version:'activity_scene_v1'},
  currentActivityBurstId:3,
  currentLiveSegmentSeq:4,
  activityBurstAnchors:[{textEnd:7}],
  todos:[{content:'verify'}],
  todoStateMeta:{ts:10},
};
const scheduled=new Map();
const cancelled=[];
const saved=[];
let snapshots=0;
let nextTimer=1;
const owner=createStreamProgressOwner({
  sessionId:'sid-1',
  streamId:'stream-1',
  getInflight:()=>inflight,
  getTodos:()=>[{content:'fallback'}],
  getTodoStateMeta:()=>({ts:5}),
  save:(sid,payload)=>saved.push([sid,payload]),
  snapshot:()=>{snapshots+=1;},
  schedule:(fn,delay)=>{const id=nextTimer++;scheduled.set(id,{fn,delay});return id;},
  cancel:(id)=>{cancelled.push(id);scheduled.delete(id);},
});
if(!Object.isFrozen(owner)) throw new Error('owner interface must be frozen');

owner.persistSoon();
owner.persistSoon();
if(scheduled.size!==1) throw new Error('persist scheduling was not coalesced');
const persistTimer=[...scheduled.entries()][0];
if(persistTimer[1].delay!==2000) throw new Error('unexpected persist delay');
scheduled.delete(persistTimer[0]);
persistTimer[1].fn();
if(saved.length!==1) throw new Error('scheduled persist did not run once');
const [sid,payload]=saved[0];
if(sid!=='sid-1'||payload.streamId!=='stream-1') throw new Error('wrong stream identity');
if(payload.lastRunJournalSeq!==7||payload.lastRunJournalEventId!=='stream-1:7') throw new Error('cursor lost');
if(payload.anchorActivityScene.version!=='activity_scene_v1') throw new Error('anchor scene lost');
if(payload.todos[0].content!=='verify'||payload.todoStateMeta.ts!==10) throw new Error('todo state lost');

owner.snapshotSoon();
owner.snapshotSoon();
if(scheduled.size!==1) throw new Error('snapshot scheduling was not coalesced');
const snapshotTimer=[...scheduled.entries()][0];
if(snapshotTimer[1].delay!==700) throw new Error('unexpected snapshot delay');
owner.cancelPending();
if(scheduled.size!==0||cancelled.length!==1) throw new Error('snapshot timer was not cancelled');
if(snapshots!==0) throw new Error('cancelled snapshot still ran');
"""
    )
    assert result.returncode == 0, result.stderr


def test_stream_control_event_owner_applies_and_consumes_goal_state():
    result = _run_node(
        r"""
globalThis.document={addEventListener(){},getElementById(){return null}};
globalThis.window=globalThis;
globalThis.addEventListener=()=>{};

const {createStreamControlEventOwner}=await import('./static/modules/messages/control-events.js');

class FakeSource {
  constructor(){this.listeners=new Map();}
  addEventListener(name,handler){this.listeners.set(name,handler);}
  emit(name,payload){const handler=this.listeners.get(name);if(!handler)throw new Error('missing listener '+name);handler({data:JSON.stringify(payload)});}
}
const source=new FakeSource();
const state={activeProfile:'profile-a',session:{session_id:'sid-1'}};
const statuses=[];
const toasts=[];
const queued=[];
const anchors=[];
const persistent=[];
const titles=[];
const background=[];
const owner=createStreamControlEventOwner({
  sessionId:'sid-1',
  state,
  applyToAnchor:(type,data)=>anchors.push([type,data]),
  modelState:()=>({model:'model-a',model_provider:'provider-a'}),
  showPersistentStateToast:(...args)=>persistent.push(args),
  applySessionTitle:(...args)=>titles.push(args),
  handleBackgroundTaskComplete:(...args)=>background.push(args),
  ui:{
    translate:(key)=>({goal_evaluating_progress:'Evaluating',goal_continuing_toast:'Continuing',steer_leftover_queued:'Queued'}[key]||key),
    setComposerStatus:(value)=>statuses.push(value),
    showToast:(...args)=>toasts.push(args),
    queueSessionMessage:(sid,payload)=>queued.push([sid,payload]),
    updateQueueBadge:()=>{},
  },
});
if(!Object.isFrozen(owner)) throw new Error('owner interface must be frozen');
owner.attach(source);

source.emit('state_saved',{session_id:'sid-1',kind:'skill',name:'review',action:'created'});
source.emit('title',{session_id:'sid-1',title:'New title'});
source.emit('bg_task_complete',{session_id:'sid-1',event_id:'event-1'});
if(persistent.length!==1||persistent[0][2].created!==true) throw new Error('persistent state event not applied');
if(titles.length!==1||titles[0][0]!=='sid-1'||titles[0][1]!=='New title') throw new Error('title event not applied');
if(background.length!==1||background[0][1]!=='sid-1') throw new Error('background event not applied');

source.emit('goal',{session_id:'sid-1',state:'evaluating'});
if(statuses.at(-1)!=='Evaluating') throw new Error('evaluating status not applied');
source.emit('goal',{session_id:'sid-1',state:'idle',message:'Goal complete',decision:'done'});
if(owner.latestGoalStatus().message!=='Goal complete') throw new Error('goal status not owned');
source.emit('goal_continue',{session_id:'sid-1',continuation_prompt:'Continue carefully'});
const continuation=owner.takeGoalContinuation();
if(!continuation||continuation.text!=='Continue carefully'||continuation.model!=='model-a'||continuation.profile!=='profile-a') throw new Error('continuation not normalized');
if(owner.takeGoalContinuation()!==null) throw new Error('continuation was not consumed');
source.emit('pending_steer_leftover',{session_id:'sid-1',text:'one more thing'});
if(queued.length!==1||queued[0][1].text!=='one more thing') throw new Error('leftover steer not queued');
if(!anchors.some(([type])=>type==='goal_continue')||!anchors.some(([type])=>type==='pending_steer_leftover')) throw new Error('anchor events missing');

source.emit('goal_continue',{session_id:'other',continuation_prompt:'wrong session'});
source.emit('pending_steer_leftover',{session_id:'other',text:'wrong session'});
if(owner.takeGoalContinuation()!==null||queued.length!==1) throw new Error('cross-session event mutated owner');
"""
    )
    assert result.returncode == 0, result.stderr
