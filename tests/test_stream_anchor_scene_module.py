"""Browser-like regression coverage for the stream anchor-scene module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "modules" / "messages" / "anchor-scene.js"
INDEX = ROOT / "static" / "index.html"
SERVICE_WORKER = ROOT / "static" / "sw.js"
NODE = shutil.which("node")


def _run_module_case(case_script: str) -> dict:
    assert NODE is not None
    script = f"""
global.window = {{
  chatActivityMode() {{ return global.__activityMode; }},
  isFinalAnswerOnlyMode() {{ return global.__activityMode === 'hide_all_activity'; }},
}};
global.__activityMode = 'compact_worklog';
import({json.dumps(MODULE.as_uri())}).then((module) => {{
  global.HermesMessages = {{
    createStreamAnchorSceneSettlement: module.createStreamAnchorSceneSettlement,
  }};
  {case_script}
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_anchor_scene_module_has_explicit_browser_load_order():
    index = INDEX.read_text(encoding="utf-8")
    entry = (MODULE.parent / "index.js").read_text(encoding="utf-8")
    service_worker = SERVICE_WORKER.read_text(encoding="utf-8")
    assert 'type="module" src="static/modules/messages/index.js' in index
    assert entry.index("from './anchor-scene.js'") < entry.index("from './stream.js'")
    assert service_worker.index("modules/messages/anchor-scene.js") < service_worker.index(
        "modules/messages/stream.js"
    )


@pytest.mark.skipif(NODE is None, reason="node required for classic-script module test")
def test_anchor_scene_module_projects_and_persists_one_settled_turn():
    result = _run_module_case(
        r"""
const requests = [];
const messages = [
  {role:'user', content:'Inspect the workspace', timestamp:100},
  {
    role:'assistant',
    content:'The workspace is ready.',
    reasoning:'I should inspect it first.',
    timestamp:101,
    tool_calls:[{
      id:'call-1',
      function:{name:'terminal', arguments:'{"cmd":"pwd"}'},
      snippet:'/workspace',
      started_at:100.5,
    }],
  },
];
const controller = HermesMessages.createStreamAnchorSceneSettlement({
  sessionId:'session-1',
  streamId:'stream-1',
  state:{session:{}, toolCalls:[]},
  anchorRegistry:{anchor:{}},
  activeMode(){ return global.__activityMode; },
  rowDisplayHintForMode(row, mode){
    if(mode==='transparent_stream') return 'chronological_activity';
    if(mode==='hide_all_activity') return 'hidden_activity';
    return row.display_hint||'activity_row';
  },
  projectLiveScene(){
    return {
      version:'activity_scene_v1',
      mode:'compact_worklog',
      identity:{},
      lifecycle:{terminal_state:'completed'},
      final_answer:'The workspace is ready.',
      activity_rows:[],
    };
  },
  request(path, options){ requests.push({path, options}); return Promise.resolve({}); },
  getOldestMessageIndex(){ return 40; },
});
const attached = controller.attachProjectedSceneToLastAssistant(messages);
const scene = messages[1]._anchor_activity_scene;
const payload = JSON.parse(requests[0].options.body);
process.stdout.write(JSON.stringify({
  namespaceType:typeof HermesMessages.createStreamAnchorSceneSettlement,
  frozen:Object.isFrozen(controller),
  attached,
  streamId:messages[1]._anchor_stream_id,
  finalAnswer:scene.final_answer,
  roles:scene.activity_rows.map(row=>row.role),
  toolId:scene.activity_rows.find(row=>row.role==='tool').tool_call_id,
  requestPath:requests[0].path,
  messageIndex:payload.message_index,
  messageWindowIndex:payload.message_window_index,
}));
"""
    )

    assert result == {
        "namespaceType": "function",
        "frozen": True,
        "attached": True,
        "streamId": "stream-1",
        "finalAnswer": "The workspace is ready.",
        "roles": ["thinking", "tool"],
        "toolId": "call-1",
        "requestPath": "/api/session/anchor-scene",
        "messageIndex": 41,
        "messageWindowIndex": 1,
    }


@pytest.mark.skipif(NODE is None, reason="node required for classic-script module test")
def test_anchor_scene_module_keeps_pure_prose_out_of_worklog_and_persists_hidden_mode():
    result = _run_module_case(
        r"""
function makeController(mode, requests){
  global.__activityMode = mode;
  return HermesMessages.createStreamAnchorSceneSettlement({
    sessionId:'session-2',
    streamId:'stream-2',
    state:{session:{}, toolCalls:[]},
    anchorRegistry:{anchor:{}},
    activeMode(){ return global.__activityMode; },
    rowDisplayHintForMode(row, activeMode){
      if(activeMode==='transparent_stream') return 'chronological_activity';
      if(activeMode==='hide_all_activity') return 'hidden_activity';
      return row.display_hint||'activity_row';
    },
    projectLiveScene(){
      return {version:'activity_scene_v1', mode, identity:{}, lifecycle:{}, activity_rows:[]};
    },
    request(path, options){ requests.push({path, options}); return Promise.resolve({}); },
    getOldestMessageIndex(){ return 0; },
  });
}
const proseRequests = [];
const proseMessages = [
  {role:'user', content:'Reply plainly'},
  {role:'assistant', content:'Plain final answer'},
];
const proseAttached = makeController('compact_worklog', proseRequests)
  .attachProjectedSceneToLastAssistant(proseMessages);

const hiddenRequests = [];
const hiddenMessages = [
  {role:'user', content:'Use a tool'},
  {role:'assistant', content:'Done', tool_calls:[{id:'call-hidden', name:'terminal'}]},
];
const hiddenAttached = makeController('hide_all_activity', hiddenRequests)
  .attachProjectedSceneToLastAssistant(hiddenMessages);
process.stdout.write(JSON.stringify({
  proseAttached,
  proseHasScene:Boolean(proseMessages[1]._anchor_activity_scene),
  proseRequestCount:proseRequests.length,
  hiddenAttached,
  hiddenMode:hiddenMessages[1]._anchor_activity_scene.mode,
  hiddenRequestCount:hiddenRequests.length,
}));
"""
    )

    assert result == {
        "proseAttached": False,
        "proseHasScene": False,
        "proseRequestCount": 0,
        "hiddenAttached": False,
        "hiddenMode": "hide_all_activity",
        "hiddenRequestCount": 1,
    }
