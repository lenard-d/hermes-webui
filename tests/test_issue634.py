"""
Tests for #634: CLI sessions not visible when setting is enabled.

Root cause: get_cli_sessions() swallowed all errors silently (bare except → return []).
Users with older hermes-agent versions (missing 'source' column in state.db) got
an empty list with no log output, making diagnosis impossible.

Fixes:
1. Schema introspection: check for 'source' column before querying, log a warning
   if missing and return early.
2. Exception path: log a warning instead of silently returning [].
"""
import pathlib

import api.models as models

MODELS_PY = pathlib.Path(__file__).parent.parent / 'api' / 'models.py'
AGENT_SESSIONS_PY = pathlib.Path(__file__).parent.parent / 'api' / 'agent_sessions.py'
src = MODELS_PY.read_text(encoding='utf-8')
agent_src = AGENT_SESSIONS_PY.read_text(encoding='utf-8')
combined_src = src + "\n" + agent_src
def _exercise_cli_error(monkeypatch, caplog, tmp_path):
    db_path = tmp_path / "state.db"

    def fail_load(*_args, **_kwargs):
        raise RuntimeError("schema exploded")

    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0)
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda _source=None, **_kwargs: (tmp_path, db_path, None, ("issue-634",)),
    )
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", fail_load)
    caplog.set_level("WARNING", logger="api.models")
    result = models.get_cli_sessions()
    return result, db_path, caplog.text


class TestCliSessionsErrorSurface:
    """get_cli_sessions() must log warnings instead of silently returning []."""

    def test_schema_introspection_present(self):
        """The function must check for the 'source' column before querying."""
        assert "PRAGMA table_info(sessions)" in combined_src

    def test_missing_source_column_logs_warning(self):
        """If 'source' column is absent, a warning is logged."""
        # The warning message must mention the missing column and how to fix it
        assert "no 'source' column" in combined_src or "has no 'source' column" in combined_src

    def test_missing_source_column_suggests_upgrade(self):
        """Warning message must suggest upgrading hermes-agent."""
        assert "Upgrade hermes-agent" in combined_src or "upgrade hermes-agent" in combined_src.lower()

    def test_exception_path_logs_warning(self, monkeypatch, caplog, tmp_path):
        """The except clause must call logger.warning, not silently pass."""
        _result, _db_path, log_text = _exercise_cli_error(monkeypatch, caplog, tmp_path)
        assert "get_cli_sessions() failed" in log_text

    def test_exception_path_includes_db_path(self, monkeypatch, caplog, tmp_path):
        """The warning must include the db_path for diagnosability."""
        _result, db_path, log_text = _exercise_cli_error(monkeypatch, caplog, tmp_path)
        assert str(db_path) in log_text

    def test_still_returns_empty_on_error(self, monkeypatch, caplog, tmp_path):
        """Function must still return [] after logging (graceful degradation)."""
        result, _db_path, _log_text = _exercise_cli_error(monkeypatch, caplog, tmp_path)
        assert result == []

    def test_source_column_check_before_sql_query(self):
        """Schema check must happen before the main SQL SELECT."""
        pragma_pos = agent_src.find("PRAGMA table_info(sessions)")
        select_pos = agent_src.find("SELECT s.id, s.title, s.model")
        assert pragma_pos != -1, "PRAGMA check not found"
        assert select_pos != -1, "SELECT query not found"
        assert pragma_pos < select_pos, \
            "Schema introspection must run before the main SQL query"
