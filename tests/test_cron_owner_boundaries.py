"""Behavioral contracts for the deepened cron domain owners."""

from __future__ import annotations

import threading

import pytest

from api.cron.manual_runs import ManualRunRegistry
from api.cron.output_history import (
    CronOutputStore,
    CronRunNotFound,
    InvalidCronOutputPath,
)
from api.cron.profiles import CronExecutionIdentity


def test_manual_run_admission_is_atomic_across_competing_threads():
    registry = ManualRunRegistry()
    barrier = threading.Barrier(16)
    results = []

    def claim():
        barrier.wait()
        results.append(registry.claim("same-job").started)

    workers = [threading.Thread(target=claim) for _ in range(16)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert results.count(True) == 1
    assert results.count(False) == 15


def test_failed_thread_start_releases_manual_run_claim(monkeypatch):
    from api.cron import manual_runs

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(manual_runs.threading, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="thread start failed"):
        manual_runs.start_manual_run(
            {"id": "thread-start-failure"},
            CronExecutionIdentity(None, None, None),
        )
    assert manual_runs.running_status("thread-start-failure") == (False, 0.0)


def test_output_store_confines_filename_to_selected_job(tmp_path):
    root = tmp_path / "output"
    (root / "job-one").mkdir(parents=True)
    (root / "job-two").mkdir()
    (root / "job-two" / "run.md").write_text("secret sibling", encoding="utf-8")
    store = CronOutputStore(root)

    with pytest.raises(InvalidCronOutputPath):
        store.read_run("job-one", "../job-two/run.md")


def test_output_store_rejects_symlink_swaps_and_reads_regular_files(tmp_path):
    root = tmp_path / "output"
    job_dir = root / "job-one"
    job_dir.mkdir(parents=True)
    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    (job_dir / "linked.md").symlink_to(target)
    (job_dir / "regular.md").write_text("## Response\ninside", encoding="utf-8")
    store = CronOutputStore(root)

    with pytest.raises(CronRunNotFound):
        store.read_run("job-one", "linked.md")
    assert store.read_run("job-one", "regular.md") == "## Response\ninside"
    outputs = store.recent_outputs("job-one", limit=10)
    assert [output["filename"] for output in outputs] == ["regular.md"]
