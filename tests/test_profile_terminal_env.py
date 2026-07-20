from pathlib import Path

import yaml


def test_profile_runtime_env_includes_terminal_config_and_dotenv(tmp_path):
    from api.profiles import get_profile_runtime_env

    home = tmp_path / "profiles" / "server-ops"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "terminal": {
                    "backend": "ssh",
                    "cwd": "/home/dso2ng/repos",
                    "timeout": 180,
                    "ssh_host": "pollux",
                    "ssh_user": "dso2ng",
                    "persistent_shell": True,
                    "lifetime_seconds": 300,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "TERMINAL_TIMEOUT=60\n"
        "TERMINAL_SSH_HOST=pollux-from-env\n"
        "HERMES_MAX_ITERATIONS=90\n",
        encoding="utf-8",
    )

    env = get_profile_runtime_env(home)

    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_CWD"] == "/home/dso2ng/repos"
    assert env["TERMINAL_SSH_USER"] == "dso2ng"
    assert env["TERMINAL_PERSISTENT_SHELL"] == "true"
    assert env["TERMINAL_LIFETIME_SECONDS"] == "300"
    # .env remains the final override source, matching CLI/profile behaviour.
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["TERMINAL_SSH_HOST"] == "pollux-from-env"
    assert env["HERMES_MAX_ITERATIONS"] == "90"


def test_streaming_applies_profile_runtime_env_to_agent_run():
    src = Path("api/runs/local.py").read_text(encoding="utf-8")
    env_src = Path("api/runs/local_environment.py").read_text(encoding="utf-8")

    assert "get_profile_runtime_env" in src
    assert "_profile_runtime_env" in src
    assert "_safe_profile_runtime_env" in src
    assert "self._previous" in env_src
    assert "filter_runtime_env_for_gateway_parity" in src
    assert "os.environ.update(safe_profile_runtime_env)" in env_src
    assert "os.environ.update(profile_runtime_env)" not in env_src


def test_filter_runtime_env_for_gateway_parity_blocks_shell_identity_vars():
    from api.profiles import filter_runtime_env_for_gateway_parity

    env = {
        "HOME": "/tmp/fake-home",
        "PATH": "/tmp/fake-bin:/usr/bin",
        "PWD": "/tmp/fake-pwd",
        "SHELL": "/bin/zsh",
        "OPENAI_API_KEY": "test-key",
        "TERMINAL_ENV": "ssh",
        "TERMINAL_CWD": "/workspace",
    }

    filtered = filter_runtime_env_for_gateway_parity(env)

    assert "HOME" not in filtered
    assert "PATH" not in filtered
    assert "PWD" not in filtered
    assert "SHELL" not in filtered
    assert filtered["OPENAI_API_KEY"] == "test-key"
    assert filtered["TERMINAL_ENV"] == "ssh"
    assert filtered["TERMINAL_CWD"] == "/workspace"


def test_profile_background_worker_uses_gateway_parity_runtime_env_filter():
    src = Path("api/profiles/runtime.py").read_text(encoding="utf-8")

    assert "filter_runtime_env_for_gateway_parity" in src
    assert "safe_runtime_env" in src
    assert "os.environ.update(safe_runtime_env)" in src
    assert "os.environ.update(runtime_env)" not in src


def test_streaming_thread_env_allows_profile_terminal_cwd_override():
    from api.streaming import _build_agent_thread_env

    run_src = Path("api/runs/local_environment.py").read_text(encoding="utf-8")

    assert "thread_env = _build_agent_thread_env(" in run_src
    assert "set_thread_env(thread_env)" in run_src
    assert "_set_thread_env(\n            **_profile_runtime_env,\n            TERMINAL_CWD" not in run_src

    env = _build_agent_thread_env(
        {
            "TERMINAL_CWD": "/profile/config/cwd",
            "HERMES_EXEC_ASK": "0",
            "HERMES_SESSION_KEY": "old-session",
            "HERMES_SESSION_ID": "old-session",
            "HERMES_SESSION_PLATFORM": "cli",
            "HERMES_HOME": "/old/profile/home",
            "TERMINAL_ENV": "ssh",
        },
        "/active/workspace",
        "active-session",
        "/active/profile/home",
    )

    assert env["TERMINAL_CWD"] == "/active/workspace"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "active-session"
    assert env["HERMES_SESSION_ID"] == "active-session"
    assert env["HERMES_SESSION_PLATFORM"] == "webui"
    assert env["HERMES_HOME"] == "/active/profile/home"
    assert env["TERMINAL_ENV"] == "ssh"
