"""Regression tests for the /background task tracker.

Covers two bugs caught in review of PR #932:

1. `get_results()` was calling `_BACKGROUND_TASKS.pop(parent_sid, [])`, which
   removed EVERY task (including still-running ones) on the first poll. Once
   popped, `complete_background()` could no longer find the task to mark done,
   so the final answer was silently lost.

2. The `_handle_background` worker thread called `_run_agent_streaming` but
   never invoked `complete_background()` after it returned. With no completion
   hook, every background task stayed in `status="running"` forever —
   `get_results()` filtered them out of its "done" list, and the user never
   saw the result.

These two bugs together made the `/background` command completely
non-functional as originally shipped.  The fix in api/background.py +
api/routes.py wires the completion hook and keeps running tasks in the
tracker until they resolve.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch


# Ensure the repo root is importable without relying on CWD.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestGetResultsKeepsRunningTasks(unittest.TestCase):
    """get_results() MUST NOT drop still-running tasks from _BACKGROUND_TASKS."""

    def setUp(self):
        import api.background as bg
        bg._BACKGROUND_TASKS.clear()
        self.bg = bg

    def test_running_tasks_survive_get_results_call(self):
        """A running task must remain in the tracker so complete_background()
        can still find it after the first poll returns."""
        parent = "parent-session-1"
        self.bg.track_background(
            parent_sid=parent, bg_sid="bg-a", stream_id="s-a",
            task_id="task-a", prompt="long task",
        )

        # First poll: task is still running, no done results to return
        results = self.bg.get_results(parent)
        self.assertEqual(results, [], "no done tasks yet — nothing to return")

        # The running task MUST still be tracked — otherwise the worker
        # thread's complete_background call cannot find it.
        remaining = self.bg.get_background_tasks(parent)
        self.assertEqual(len(remaining), 1, (
            "get_results dropped the still-running task — subsequent "
            "complete_background() calls will silently no-op and the "
            "result will be lost forever"
        ))
        self.assertEqual(remaining[0]["status"], "running")
        self.assertEqual(remaining[0]["task_id"], "task-a")

    def test_done_tasks_are_returned_and_removed(self):
        """Done tasks are returned and popped; running tasks stay."""
        parent = "parent-session-2"
        self.bg.track_background(parent, "bg-done", "s-d", "task-done", "p1")
        self.bg.track_background(parent, "bg-run", "s-r", "task-run", "p2")
        self.bg.complete_background(parent, "task-done", "42")

        results = self.bg.get_results(parent)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "task-done")
        self.assertEqual(results[0]["answer"], "42")

        # Done one is gone; running one is still tracked
        remaining = self.bg.get_background_tasks(parent)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["task_id"], "task-run")
        self.assertEqual(remaining[0]["status"], "running")

    def test_complete_after_poll_still_reaches_tracker(self):
        """Regression for the original bug: poll → complete → poll must surface
        the result.  Before the fix, the first poll popped the running task and
        complete_background()'s loop iterated over an empty list."""
        parent = "parent-session-3"
        self.bg.track_background(parent, "bg-x", "s-x", "task-x", "slow task")

        # Frontend polls before the task finishes
        first = self.bg.get_results(parent)
        self.assertEqual(first, [])

        # Worker thread finishes and calls complete_background
        self.bg.complete_background(parent, "task-x", "answer!")

        # Next poll must surface the answer
        second = self.bg.get_results(parent)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["task_id"], "task-x")
        self.assertEqual(second[0]["answer"], "answer!")

    def test_empty_parent_is_cleaned_up(self):
        """When all tasks are done and returned, the parent key is removed from the dict."""
        parent = "parent-session-4"
        self.bg.track_background(parent, "bg-1", "s-1", "task-1", "p")
        self.bg.complete_background(parent, "task-1", "ok")
        self.bg.get_results(parent)
        self.assertNotIn(parent, self.bg._BACKGROUND_TASKS)


class TestBackgroundCompletionOwner(unittest.TestCase):
    """The run owner must settle tracking after execution returns."""

    def test_worker_completes_with_the_persisted_assistant_answer(self):
        import api.runs.background as owner

        completed = []
        with (
            patch.object(owner, "run_agent_streaming", return_value=None),
            patch.object(owner, "_last_assistant_answer", return_value="answer!"),
            patch.object(
                owner,
                "complete_background",
                side_effect=lambda parent, task, answer: completed.append(
                    (parent, task, answer)
                ),
            ),
        ):
            owner._run_background_and_complete(
                parent_session_id="parent",
                background_session_id="background",
                task_id="task",
                prompt="slow task",
                model="model",
                model_provider="provider",
                workspace="/tmp",
                stream_id="stream",
            )

        self.assertEqual(completed, [("parent", "task", "answer!")])


if __name__ == "__main__":
    unittest.main()
