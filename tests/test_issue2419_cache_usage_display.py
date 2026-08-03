from tests.frontend_asset_contract import family_source

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_webui_backend_prompt_cache_hit_percent_uses_prompt_total_denominator():
    from api.usage import prompt_cache_hit_percent

    assert prompt_cache_hit_percent(100_000, 125_000) == 80
    assert prompt_cache_hit_percent(0, 125_000) is None
    assert prompt_cache_hit_percent(100, 0) is None
    assert prompt_cache_hit_percent(None, None) is None
    assert prompt_cache_hit_percent(200, 100) == 100


def test_session_compact_exposes_prompt_cache_counters():
    from api.sessions.store import Session

    session = Session(
        session_id="issue2419_cache_usage",
        workspace="/tmp",
        input_tokens=125_000,
        output_tokens=5_000,
        estimated_cost=0.44,
        cache_read_tokens=100_000,
        cache_write_tokens=5_000,
    )

    compact = session.compact()

    assert compact["cache_read_tokens"] == 100_000
    assert compact["cache_write_tokens"] == 5_000
    assert compact["cache_hit_percent"] == 80


def test_streaming_usage_payload_includes_prompt_cache_counters():
    """Settlement publishes cumulative and per-turn cache usage to SSE."""
    from api.runs.local_success import LocalSuccessProjection

    class FakeMeter:
        def get_ttft_ms(self, _stream_id):
            return None

        def get_stats(self, _stream_id):
            return {}

    session = SimpleNamespace(
        session_id="issue-2419",
        title="cache test",
        messages=[
            {"role": "user", "content": "hello", "timestamp": 1},
            {"role": "assistant", "content": "world", "timestamp": 2},
        ],
        input_tokens=25_000,
        output_tokens=1_000,
        estimated_cost=0.01,
        cache_read_tokens=5_000,
        cache_write_tokens=1_000,
        tool_calls=[],
        gateway_routing=None,
        gateway_routing_history=[],
        last_prompt_tokens=0,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        post_compression_context_tokens_estimate=None,
    )
    agent = SimpleNamespace(
        session_prompt_tokens=125_000,
        session_completion_tokens=5_000,
        session_estimated_cost_usd=0.44,
        session_cache_read_tokens=100_000,
        session_cache_write_tokens=5_000,
        model="",
        context_compressor=SimpleNamespace(
            context_length=128_000,
            threshold_tokens=100_000,
            last_prompt_tokens=125_000,
        ),
    )
    events = []

    with patch("api.runs.local_success.time.time", return_value=105), \
         patch("api.runs.local_success.meter", return_value=FakeMeter()):
        projection = LocalSuccessProjection.apply(
            session,
            agent=agent,
            result={},
            route_model="",
            resolved_model="",
            resolved_provider=None,
            resolved_base_url=None,
            resolved_api_key=None,
            config={},
            previous_messages=[],
            reasoning_segments={},
            live_tool_calls={},
            attachments=[],
            message_text="hello",
            turn_started_at=100,
            stream_id="stream-2419",
        )
        projection.publish_terminal(
            session,
            session_id=session.session_id,
            stream_id="stream-2419",
            agent=agent,
            publish=lambda event, payload: events.append((event, payload)),
            payload_builder=lambda _session, **_kwargs: {"session_id": session.session_id},
            tool_limit_reached=False,
            maybe_schedule_title_refresh=lambda *_args: None,
            writeback_timings=[],
            writeback_started=100,
            logger=SimpleNamespace(),
        )

    done_payload = next(payload for event, payload in events if event == "done")
    usage = done_payload["usage"]
    assert usage["cache_read_tokens"] == 100_000
    assert usage["cache_write_tokens"] == 5_000
    assert usage["cache_hit_percent"] == 80
    assert usage["turn_cache_hit_percent"] == 95


def test_context_indicator_surfaces_cache_hit_rate():
    src = family_source("ui")

    assert "cacheReadTok=usage.cache_read_tokens||0" in src
    assert "cacheWriteTok=usage.cache_write_tokens||0" in src
    assert "cacheHitPct=usage.cache_hit_percent" in src
    assert "t('usage_cache_hit_detail',cacheHitPct" in src
    assert "Estimated cost: $${cost<0.01?cost.toFixed(4):cost.toFixed(2)}" in src
    assert "cacheHitPct=msg._turnUsage.cache_hit_percent" in src
    assert "t('usage_cached_percent',cacheHitPct)" in src
    assert "cacheHitPct!=null" in src
    assert "cacheReadTok/cacheTotalTok" not in src
    assert "cacheRead/cacheTotal" not in src
    assert "cacheReadTok/promptTok" not in src
    assert "cacheRead/cacheDenom" not in src


def test_cache_usage_labels_are_localized():
    src = family_source("i18n")

    assert src.count("usage_cache_hit_detail:") == 15
    assert src.count("usage_cached_percent:") == 15
    assert "usage_cache_hit_detail: 'Cache: {0}% hit ({1} read / {2} write)'" in src
    assert "usage_cached_percent: '{0}% cached'" in src


def test_done_handler_preserves_per_turn_cache_deltas():
    src = family_source("messages")

    assert "_prevCacheRead=(S.session&&S.session.cache_read_tokens)||0" in src
    assert "curCacheRead=d.usage.cache_read_tokens||0" in src
    assert "cache_read_tokens:Math.max(0,curCacheRead-_prevCacheRead)" in src
    assert "cache_write_tokens:Math.max(0,curCacheWrite-_prevCacheWrite)" in src
    assert "cache_hit_percent:d.usage.turn_cache_hit_percent" in src
