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
const fs = require('fs');
const vm = require('vm');
const sandbox = {{window: {{}}}};
sandbox.globalThis = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(module_path))}, 'utf8'), sandbox);
const createCache = sandbox.window.HermesSessionRenderCache.create;
{script}
"""
    result = subprocess.run(
        [NODE, "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
