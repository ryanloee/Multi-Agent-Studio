"""Tests for mas_agent.config — configuration loading and priority."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mas_agent.config import DEFAULTS, USER_CONFIG_PATH, load_config


@pytest.fixture()
def isolated_cwd(tmp_path: Path) -> str:
    """Return a temporary directory that has no mas.json."""
    return str(tmp_path)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MAS_* environment variables for the duration of the test."""
    for key in list(os.environ):
        if key.startswith("MAS_"):
            monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------ #
# 1. Project config — create mas.json, verify config loaded
# ------------------------------------------------------------------ #
def test_project_config(isolated_cwd: str, clean_env: None) -> None:
    project_dir = Path(isolated_cwd)
    (project_dir / "mas.json").write_text(json.dumps({"max_turns": 25, "shell_timeout": 60}))

    config = load_config(cwd=isolated_cwd)

    assert config["max_turns"] == 25
    assert config["shell_timeout"] == 60
    # Other fields should still come from defaults
    assert config["max_tokens"] == DEFAULTS["max_tokens"]


# ------------------------------------------------------------------ #
# 2. User config — mock home dir, verify user-level config loaded
# ------------------------------------------------------------------ #
def test_user_config(isolated_cwd: str, clean_env: None, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_config_dir = fake_home / ".mas"
    user_config_dir.mkdir()
    (user_config_dir / "config.json").write_text(json.dumps({"max_turns": 42}))

    with patch("mas_agent.config.Path.home", return_value=fake_home):
        with patch("mas_agent.config.USER_CONFIG_PATH", user_config_dir / "config.json"):
            config = load_config(cwd=isolated_cwd)

    assert config["max_turns"] == 42
    assert config["max_tokens"] == DEFAULTS["max_tokens"]


# ------------------------------------------------------------------ #
# 3. Priority — project overrides user
# ------------------------------------------------------------------ #
def test_project_overrides_user(isolated_cwd: str, clean_env: None, tmp_path: Path) -> None:
    # Set up user-level config
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_config_dir = fake_home / ".mas"
    user_config_dir.mkdir()
    (user_config_dir / "config.json").write_text(json.dumps({"max_turns": 10}))

    # Set up project-level config with different value
    (Path(isolated_cwd) / "mas.json").write_text(json.dumps({"max_turns": 99}))

    with patch("mas_agent.config.Path.home", return_value=fake_home):
        with patch("mas_agent.config.USER_CONFIG_PATH", user_config_dir / "config.json"):
            config = load_config(cwd=isolated_cwd)

    # Project should win over user
    assert config["max_turns"] == 99


# ------------------------------------------------------------------ #
# 4. Environment variable overrides default
# ------------------------------------------------------------------ #
def test_env_overrides_default(isolated_cwd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAS_MAX_TURNS", "30")

    config = load_config(cwd=isolated_cwd)

    assert config["max_turns"] == 30
    assert config["max_tokens"] == DEFAULTS["max_tokens"]


def test_env_overrides_project_config(isolated_cwd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    (Path(isolated_cwd) / "mas.json").write_text(json.dumps({"max_turns": 99}))
    monkeypatch.setenv("MAS_MAX_TURNS", "15")

    config = load_config(cwd=isolated_cwd)

    assert config["max_turns"] == 15


# ------------------------------------------------------------------ #
# 5. No config files — built-in defaults used
# ------------------------------------------------------------------ #
def test_no_config_returns_defaults(isolated_cwd: str, clean_env: None) -> None:
    # Ensure no user config by mocking home to an empty dir
    fake_home = Path(isolated_cwd) / "empty_home"
    fake_home.mkdir()

    with patch("mas_agent.config.Path.home", return_value=fake_home):
        with patch("mas_agent.config.USER_CONFIG_PATH", fake_home / ".mas" / "config.json"):
            config = load_config(cwd=isolated_cwd)

    assert config == dict(DEFAULTS)


# ------------------------------------------------------------------ #
# 6. Invalid JSON — graceful fallback to defaults
# ------------------------------------------------------------------ #
def test_invalid_json_project_uses_defaults(isolated_cwd: str, clean_env: None) -> None:
    (Path(isolated_cwd) / "mas.json").write_text("{this is not valid json!!!")

    fake_home = Path(isolated_cwd) / "empty_home"
    fake_home.mkdir()

    with patch("mas_agent.config.Path.home", return_value=fake_home):
        with patch("mas_agent.config.USER_CONFIG_PATH", fake_home / ".mas" / "config.json"):
            config = load_config(cwd=isolated_cwd)

    # Should fall back to built-in defaults
    assert config == dict(DEFAULTS)


def test_invalid_json_user_uses_defaults(isolated_cwd: str, clean_env: None, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    user_config_dir = fake_home / ".mas"
    user_config_dir.mkdir()
    (user_config_dir / "config.json").write_text("not json at all")

    with patch("mas_agent.config.Path.home", return_value=fake_home):
        with patch("mas_agent.config.USER_CONFIG_PATH", user_config_dir / "config.json"):
            config = load_config(cwd=isolated_cwd)

    assert config == dict(DEFAULTS)


# ------------------------------------------------------------------ #
# 7. CLI overrides have highest priority
# ------------------------------------------------------------------ #
def test_cli_overrides_env(isolated_cwd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAS_MAX_TURNS", "30")
    (Path(isolated_cwd) / "mas.json").write_text(json.dumps({"max_turns": 99}))

    config = load_config(cli_overrides={"max_turns": 5}, cwd=isolated_cwd)

    assert config["max_turns"] == 5


# ------------------------------------------------------------------ #
# 8. Deep merge for nested dicts
# ------------------------------------------------------------------ #
def test_deep_merge_tools(isolated_cwd: str, clean_env: None) -> None:
    (Path(isolated_cwd) / "mas.json").write_text(
        json.dumps({"tools": {"disabled": ["shell"]}})
    )

    config = load_config(cwd=isolated_cwd)

    assert config["tools"]["disabled"] == ["shell"]
    assert config["tools"]["custom"] == []  # preserved from defaults


# ------------------------------------------------------------------ #
# 9. None values in cli_overrides are ignored
# ------------------------------------------------------------------ #
def test_cli_none_values_ignored(isolated_cwd: str, clean_env: None) -> None:
    (Path(isolated_cwd) / "mas.json").write_text(json.dumps({"max_turns": 77}))

    config = load_config(cli_overrides={"max_turns": None}, cwd=isolated_cwd)

    # The None should be filtered out, so project config wins
    assert config["max_turns"] == 77
