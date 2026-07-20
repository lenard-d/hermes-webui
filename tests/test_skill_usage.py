"""Tests for api/skill_usage.py — .usage.json reader.

Covers:
  - read_skill_usage with various .usage.json states
  - GET /api/skills/usage route presence (read-only, agent writes the file)
"""

import json
from collections import defaultdict
from types import SimpleNamespace

from api.skill_usage import read_skill_usage


def _usage_route_context(skills_dir, response):
    def unused(*_args, **_kwargs):
        return None

    context = defaultdict(lambda: unused)
    context.update(
        {
            "_active_skills_dir": lambda: skills_dir,
            "_skills_list_from_dir": lambda _root: {"skills": []},
            "j": lambda _handler, payload, **_kwargs: response.update(payload) or True,
        }
    )
    return context


class TestReadSkillUsage:
    def test_read_empty(self, tmp_path):
        """File does not exist -> returns {}."""
        assert read_skill_usage(tmp_path) == {}

    def test_read_valid(self, tmp_path):
        """Well-formed .usage.json with nested entries is returned as-is."""
        data = {
            "research-arxiv": {"use_count": 12, "view_count": 5},
            "hermes-agent": {"use_count": 8, "view_count": 3},
        }
        (tmp_path / ".usage.json").write_text(json.dumps(data), encoding="utf-8")
        assert read_skill_usage(tmp_path) == data

    def test_read_agent_format(self, tmp_path):
        """Agent-side format (ISO timestamps) is accepted."""
        data = {
            "dev-workflow": {
                "use_count": 77,
                "view_count": 77,
                "last_used_at": "2024-04-05T20:54:38Z",
                "state": "active",
            },
        }
        (tmp_path / ".usage.json").write_text(json.dumps(data), encoding="utf-8")
        assert read_skill_usage(tmp_path) == data

    def test_read_corrupt_json(self, tmp_path):
        """Corrupt JSON returns {} without raising."""
        (tmp_path / ".usage.json").write_text("not json", encoding="utf-8")
        assert read_skill_usage(tmp_path) == {}

    def test_read_wrong_type(self, tmp_path):
        """Non-dict top-level value returns {}."""
        (tmp_path / ".usage.json").write_text("42", encoding="utf-8")
        assert read_skill_usage(tmp_path) == {}


class TestApiSkillsUsageRoute:
    def test_route_handler_present(self, tmp_path):
        """The automation query owner handles GET /api/skills/usage."""
        from api.http.routes import automation_queries

        response = {}

        assert automation_queries.handle_get(
            object(),
            SimpleNamespace(path="/api/skills/usage"),
            _usage_route_context(tmp_path, response),
        ) is True
        assert response == {
            "usage": {},
            "skill_names": [],
            "total_invocations": 0,
            "unique_skills_used": 0,
        }

    def test_route_returns_usage_structure(self, tmp_path):
        """The observable response includes normalized usage and aggregate fields."""
        from api.http.routes import automation_queries

        (tmp_path / ".usage.json").write_text(
            json.dumps(
                {
                    "alpha": {
                        "use_count": "2",
                        "view_count": 1,
                        "state": "active",
                    },
                    "broken": "not-a-mapping",
                }
            ),
            encoding="utf-8",
        )
        response = {}
        context = _usage_route_context(tmp_path, response)
        context["_skills_list_from_dir"] = lambda _root: {
            "skills": [{"name": "zeta"}, {"name": "alpha"}]
        }

        assert automation_queries.handle_get(
            object(),
            SimpleNamespace(path="/api/skills/usage"),
            context,
        ) is True
        assert response == {
            "usage": {
                "alpha": {
                    "use_count": 2,
                    "view_count": 1,
                    "patch_count": 0,
                    "state": "active",
                },
                "broken": {"use_count": 0, "view_count": 0, "patch_count": 0},
            },
            "skill_names": ["alpha", "zeta"],
            "total_invocations": 3,
            "unique_skills_used": 1,
        }
