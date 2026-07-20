"""Regression checks for WebUI AIAgent iteration-budget parity.

WebUI streaming agents must honor Hermes' configured agent.max_turns. Otherwise
browser-originated long-running tasks silently fall back to AIAgent's constructor
default and hit the "maximum number of tool-calling iterations" summary path even
when the operator raised the global Hermes budget.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CONFIG_OWNER = (REPO / "api" / "runs" / "local_agent_config.py").read_text(encoding="utf-8")
CACHE_OWNER = (REPO / "api" / "runs" / "local_agent_cache.py").read_text(encoding="utf-8")


def test_streaming_agent_reads_agent_max_turns_from_config():
    assert 'agent_config.get("max_turns", config.get("max_turns"))' in CONFIG_OWNER


def test_streaming_agent_passes_max_iterations_to_aiagent():
    assert 'if "max_iterations" in parameters and max_iterations is not None:' in CONFIG_OWNER
    assert 'kwargs["max_iterations"] = max_iterations' in CONFIG_OWNER


def test_streaming_agent_cache_signature_includes_max_iterations():
    assert "max_iterations or \"\"" in CACHE_OWNER
