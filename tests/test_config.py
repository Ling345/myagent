"""配置模块测试。"""

from __future__ import annotations

import pytest

from agentcode.config import Settings, find_env_file, mask_secret
from agentcode.core.errors import ConfigError


def test_missing_llm_key_raises_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)
    with pytest.raises(ConfigError):
        settings.validate()


def test_env_file_values_are_loaded(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-from-file\nLLM_BASE_URL=https://from-file.invalid/v1\nLLM_MODEL_ID=file-model\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False).validate()
    assert settings.model == "file-model"
    assert settings.base_url == "https://from-file.invalid/v1"


def test_process_env_wins_over_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL_ID=file-model\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL_ID", "env-model")
    settings = Settings.from_env(env_file=str(env_file), search_parents=False)
    assert settings.model == "env-model"


def test_mask_secret_hides_middle_part():
    assert mask_secret("sk-abcdefghijklmn") == "sk-a******klmn"
    assert mask_secret(None) == "（未配置）"
    assert mask_secret("short") == "*****"


def test_find_env_file_walks_up_parents(tmp_path):
    (tmp_path / ".env").write_text("LLM_API_KEY=sk-x\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_env_file(nested) == tmp_path / ".env"


def test_apply_overrides_keeps_unknown_keys_in_extra(settings):
    merged = settings.apply_overrides({"max_steps": 9, "自定义": "值"})
    assert merged.max_steps == 9
    assert merged.extra["自定义"] == "值"
    assert merged.model == settings.model


def test_masked_hides_api_key(settings):
    masked = settings.masked()
    assert masked["LLM_API_KEY"] == "sk-t******7890"
    assert masked["LLM_MODEL_ID"] == "test-model"


def test_backup_settings_are_read_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("AGENT_BACKUP_INTERVAL_HOURS", "6")
    monkeypatch.setenv("AGENT_BACKUP_KEEP", "3")
    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)
    assert settings.backup_dir == str(tmp_path / "backups")
    assert settings.backup_interval_hours == 6.0
    assert settings.backup_keep == 3


def test_backup_settings_show_up_in_the_masked_summary(settings, tmp_path):
    """自动备份开没开，得能在 agentcode config 里一眼看到。"""
    settings.backup_dir = str(tmp_path / "backups")
    masked = settings.masked()
    assert masked["AGENT_BACKUP_DIR"] == str(tmp_path / "backups")
    assert "AGENT_BACKUP_KEEP" in masked


def test_validate_rejects_a_backup_interval_that_never_fires(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL_ID", "test-model")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"AGENT_BACKUP_DIR={tmp_path / 'backups'}\nAGENT_BACKUP_INTERVAL_HOURS=0\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False)
    with pytest.raises(ConfigError):
        settings.validate()
