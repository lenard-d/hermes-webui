import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "static" / "messages.js",
    ROOT / "static" / "messages_parts" / "markdown_tables.js",
    ROOT / "static" / "messages_parts" / "composer_context.js",
    ROOT / "static" / "messages_parts" / "send.js",
    ROOT / "static" / "messages_parts" / "stream_lifecycle.js",
    ROOT / "static" / "messages_parts" / "stream_anchor_scene.js",
    ROOT / "static" / "messages_parts" / "stream_run_journal.js",
    ROOT / "static" / "messages_parts" / "stream_live_tools.js",
    ROOT / "static" / "messages_parts" / "stream_renderer.js",
    ROOT / "static" / "messages_parts" / "stream.js",
    ROOT / "static" / "messages_parts" / "composer_approvals.js",
    ROOT / "static" / "messages_parts" / "session_events.js",
    ROOT / "static" / "messages_parts" / "clarify.js",
    ROOT / "static" / "messages_parts" / "notifications_background.js",
]


def test_split_modules_are_complete_and_manifest_free():
    part_dir = ROOT / "static" / "messages_parts"
    assert not (part_dir / "manifest.json").exists()
    assert {path.name for path in MODULES[1:]} == {
        path.name for path in part_dir.glob("*.js")
    }


def test_each_module_is_an_independently_valid_classic_script():
    node = shutil.which("node")
    if node is None:
        return
    for path in MODULES:
        result = subprocess.run(
            [node, "--check", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_full_messages_load_order_is_syntax_valid(tmp_path):
    combined_path = tmp_path / "messages-combined.js"
    combined_path.write_text(
        "\n".join(path.read_text(encoding="utf-8") for path in MODULES),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", "--check", str(combined_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_messages_family_loads_in_index_and_service_worker_order():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    worker = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    relative_names = [path.relative_to(ROOT / "static").as_posix() for path in MODULES]
    html_markers = [
        f'src="static/{name}?v=__WEBUI_VERSION__"' for name in relative_names
    ]
    worker_markers = [f"'./static/{name}' + VQ" for name in relative_names]

    html_positions = [html.index(marker) for marker in html_markers]
    worker_positions = [worker.index(marker) for marker in worker_markers]
    assert html_positions == sorted(html_positions)
    assert worker_positions == sorted(worker_positions)


def test_stream_owners_install_in_classic_script_order_with_frozen_interfaces():
    stream_modules = MODULES[4:10]
    runner = r"""
const fs=require('fs');
const vm=require('vm');
const context=vm.createContext({console,setTimeout,clearTimeout,performance:{now:()=>0}});
context.window=context;
for(const path of process.argv.slice(1)){
  vm.runInContext(fs.readFileSync(path,'utf8'),context,{filename:path});
}
const messages=context.HermesMessages;
for(const name of [
  'closeLiveStream',
  'createStreamAnchorSceneSettlement',
  'createStreamRunJournalCursor',
  'createStreamLiveToolTracker',
  'createStreamRenderer',
  'attachLiveStream',
]){
  if(typeof messages[name]!=='function') throw new Error('missing stream owner: '+name);
}
for(const name of [
  'createStreamRunJournalCursor',
  'createStreamLiveToolTracker',
  'createStreamRenderer',
]){
  if(!Object.isFrozen(messages[name]())) throw new Error('unfrozen interface: '+name);
}
"""
    result = subprocess.run(
        ["node", "-e", runner, *(str(path) for path in stream_modules)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_stream_owner_fails_before_state_or_transport_mutation():
    runner = r"""
const fs=require('fs');
const vm=require('vm');
let stateMutations=0;
let transportMutations=0;
const context=vm.createContext({console});
context.window=context;
context._bindStreamHiddenTracker=()=>{ stateMutations+=1; };
context._STREAM_WAS_HIDDEN=new Proxy({}, {
  get(){ stateMutations+=1; return undefined; },
  set(){ stateMutations+=1; return true; },
});
context._STREAM_NOTIFICATION_BACKGROUND=new Proxy({}, {
  get(){ stateMutations+=1; return undefined; },
  set(){ stateMutations+=1; return true; },
});
context.INFLIGHT=new Proxy({}, {
  get(){ stateMutations+=1; return undefined; },
  set(){ stateMutations+=1; return true; },
});
context.S=new Proxy({messages:[]}, {
  get(target,key){ stateMutations+=1; return target[key]; },
  set(target,key,value){ stateMutations+=1; target[key]=value; return true; },
});
context.closeOtherLiveStreams=()=>{ transportMutations+=1; };
context.closeLiveStream=()=>{ transportMutations+=1; };
context._suspendSessionStreamForLiveChat=()=>{ transportMutations+=1; };
context.EventSource=function(){ transportMutations+=1; };

const streamPath=process.argv[1];
vm.runInContext(fs.readFileSync(streamPath,'utf8'),context,{filename:streamPath});
context.HermesMessages.createStreamRunJournalCursor=()=>Object.freeze({});
context.HermesMessages.createStreamAnchorSceneSettlement=()=>Object.freeze({});
context.HermesMessages.createStreamLiveToolTracker=()=>Object.freeze({});
delete context.HermesMessages.createStreamRenderer;

let failure=null;
try{
  context.HermesMessages.attachLiveStream('session-1','stream-1');
}catch(error){
  failure=error;
}
if(!failure) throw new Error('missing renderer owner did not fail closed');
if(!String(failure.message||failure).includes('renderer')){
  throw new Error('failure did not identify the missing renderer owner');
}
if(stateMutations!==0) throw new Error('state mutated before owner validation: '+stateMutations);
if(transportMutations!==0) throw new Error('transport mutated before owner validation: '+transportMutations);
"""
    result = subprocess.run(
        ["node", "-e", runner, str(ROOT / "static" / "messages_parts" / "stream.js")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_modules_use_one_namespace_and_keep_compatibility_declarations():
    for path in MODULES:
        source = path.read_text(encoding="utf-8")
        assert "globalThis.HermesMessages = HermesMessages;" in source

    combined = "\n".join(path.read_text(encoding="utf-8") for path in MODULES)
    for declaration in (
        "async function send(){",
        "function attachLiveStream(activeSid, streamId, uploaded=[], options={}){",
        "function startSessionStream(sid)",
        "async function respondClarify(response)",
    ):
        assert declaration in combined

    exported = set(re.findall(r"^  ([A-Za-z_$][\w$]*),?$", combined, re.MULTILINE))
    assert {
        "send",
        "attachLiveStream",
        "startSessionStream",
        "respondApproval",
        "respondClarify",
        "sendBrowserNotification",
    } <= exported


def test_module_sizes_are_reviewable_with_one_documented_stream_exception():
    sizes = {
        path.relative_to(ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in MODULES
    }
    stream_name = "static/messages_parts/stream.js"
    assert sizes[stream_name] > 1200
    assert sizes[stream_name] < 5000
    assert all(lines <= 1200 for name, lines in sizes.items() if name != stream_name)

    stream_source = (ROOT / stream_name).read_text(encoding="utf-8")
    assert stream_source.count("function attachLiveStream(") == 1
    assert stream_source.rstrip().endswith("});")
    assert "Object.assign(HermesMessages, {\n  attachLiveStream," in stream_source
