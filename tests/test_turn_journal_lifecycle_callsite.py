from pathlib import Path
import inspect

from api.runs.local_failures import LocalFailureOwner


def test_turn_execution_owns_worker_started_before_worker_body_runs():
    execution_src = Path("api/runs/execution.py").read_text(encoding="utf-8")
    streaming_src = Path("api/runs/local.py").read_text(encoding="utf-8")
    gateway_src = Path("api/runs/gateway.py").read_text(encoding="utf-8")

    assert '"event": "worker_started"' in execution_src
    assert "record_worker_started=not ephemeral" in streaming_src
    assert "record_worker_started=True" in gateway_src


def test_streaming_appends_assistant_started_before_final_save():
    src = Path("api/runs/local.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    save_idx = src.index("s.save()", assistant_idx)

    assert block_idx < assistant_idx < save_idx


def test_streaming_assistant_started_uses_latest_assistant_message():
    src = Path("api/runs/local.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    block = src[block_idx:assistant_idx]

    assert "range(len(s.messages) - 1, -1, -1)" in block
    assert '"assistant_message_index": _latest_assistant_idx' in src[assistant_idx:src.index("s.save()", assistant_idx)]


def test_streaming_appends_completed_after_final_save():
    src = Path("api/runs/local.py").read_text(encoding="utf-8")
    assistant_idx = src.index('"event": "assistant_started"')
    save_idx = src.index("s.save()", assistant_idx)
    completed_idx = src.index('"event": "completed"', save_idx)

    assert save_idx < completed_idx


def test_streaming_appends_interrupted_on_provider_error_path():
    exception_src = inspect.getsource(LocalFailureOwner.handle_exception)
    persist_src = inspect.getsource(LocalFailureOwner._persist_error)
    exception_idx = exception_src.index("_sanitize_provider_exception(str(exc))")
    persist_idx = exception_src.index("self._persist_error(", exception_idx)

    assert "append_interrupted=True" in exception_src[persist_idx:]
    interrupted_idx = persist_src.index("if append_interrupted")
    apperror_idx = persist_src.index('self.ctx.publish("apperror", payload)')
    assert interrupted_idx < apperror_idx
