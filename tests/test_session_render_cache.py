"""Behavioral contract for the bounded browser transcript render cache."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _run_cache_scenario(script: str) -> dict:
    module_path = ROOT / "static" / "session_render_cache.js"
    harness = f"""
import fs from 'node:fs';
const source = fs.readFileSync({json.dumps(str(module_path))}, 'utf8');
const moduleUrl = `data:text/javascript;base64,${{Buffer.from(source).toString('base64')}}`;
globalThis.window = {{}};
const {{createSessionRenderCache, createRenderSignature}} = await import(moduleUrl);
const createCache = createSessionRenderCache;
{script}
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_module_import_does_not_publish_a_browser_global():
    result = _run_cache_scenario(
        """
console.log(JSON.stringify({
  exportedFactory: typeof createCache,
  leakedLegacyInterface: Object.hasOwn(window, 'HermesSessionRenderCache'),
}));
"""
    )

    assert result == {
        "exportedFactory": "function",
        "leakedLegacyInterface": False,
    }


def test_render_signature_changes_for_each_rendered_state_dimension():
    result = _run_cache_scenario(
        """
const base={messages:[{role:'assistant',content:'same'}],toolCalls:[],session:{}};
const signatures={
  base:createRenderSignature(base),
  repeat:createRenderSignature(structuredClone(base)),
  content:createRenderSignature({...base,messages:[{role:'assistant',content:'changed'}]}),
  messageTool:createRenderSignature({...base,messages:[{...base.messages[0],tool_calls:[{id:'1',name:'read'}]}]}),
  partialTool:createRenderSignature({...base,messages:[{...base.messages[0],_partial_tool_calls:[{id:'1',snippet:'partial'}]}]}),
  settledTool:createRenderSignature({...base,toolCalls:[{tid:'1',name:'read',snippet:'done'}]}),
  compression:createRenderSignature({...base,session:{compression_anchor_summary:'summary'}}),
};
console.log(JSON.stringify(signatures));
"""
    )

    assert result["base"] == result.pop("repeat")
    assert len(set(result.values())) == len(result)


def test_cache_enforces_lru_and_memory_limits_through_its_interface():
    result = _run_cache_scenario(
        """
const cache = createCache({maxEntries: 2, maxEntryBytes: 20, maxTotalBytes: 24});
cache.set('a', {html: 'aaaa', signature: 'a'}); // 8 bytes
cache.set('b', {html: 'bbbb', signature: 'b'}); // 8 bytes
cache.get('a'); // a is now most recently used
cache.set('c', {html: 'cccc', signature: 'c'}); // evicts b by entry count
const afterLru = {a: !!cache.get('a'), b: !!cache.get('b'), c: !!cache.get('c')};
const oversizedAccepted = cache.set('huge', {html: 'x'.repeat(11)}); // 22 bytes
console.log(JSON.stringify({
  afterLru,
  oversizedAccepted,
  hugePresent: cache.has('huge'),
  size: cache.size,
  bytes: cache.bytes,
}));
"""
    )

    assert result == {
        "afterLru": {"a": True, "b": False, "c": True},
        "oversizedAccepted": False,
        "hugePresent": False,
        "size": 2,
        "bytes": 16,
    }


def test_cache_replacement_and_clear_keep_accounting_consistent():
    result = _run_cache_scenario(
        """
const cache = createCache({maxEntries: 3, maxEntryBytes: 100, maxTotalBytes: 100});
cache.set('a', {html: 'aaaa'});
cache.set('b', {html: 'bb'});
cache.set('a', {html: 'a'});
const beforeClear = {size: cache.size, bytes: cache.bytes, aBytes: cache.get('a').bytes};
cache.clear();
console.log(JSON.stringify({
  beforeClear,
  afterClear: {size: cache.size, bytes: cache.bytes, a: cache.get('a')},
}));
"""
    )

    assert result == {
        "beforeClear": {"size": 2, "bytes": 6, "aBytes": 2},
        "afterClear": {"size": 0, "bytes": 0, "a": None},
    }
