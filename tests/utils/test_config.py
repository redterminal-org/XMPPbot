import os
import tempfile
import json
import logging
from pathlib import Path
import pytest

import utils.config as config_mod


def test_load_config_returns_defaults(tmp_path, monkeypatch):
    # config.json does not exist, should return default config
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    result = config_mod.load_config()
    assert result == config_mod.DEFAULT_CONFIG


def test_load_config_loads_json(tmp_path, monkeypatch):
    data = {"prefix": ";", "loglevel": "DEBUG", "custom": "extra"}
    (tmp_path / "config.json").write_text(json.dumps(data))
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    result = config_mod.load_config()
    # Should have all the keys, including custom
    for k, v in data.items():
        assert result[k] == v


def test_load_config_with_partial_override(tmp_path, monkeypatch):
    data = {"prefix": ";"}
    (tmp_path / "config.json").write_text(json.dumps(data))
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    result = config_mod.load_config()
    # Should override prefix but keep default loglevel
    assert result["prefix"] == ";"
    assert result["loglevel"] == "INFO"


def test_load_config_bad_json(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text("{this_is:not:json,]")
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    cfg = config_mod.load_config()
    # Should print warning and use defaults
    out = capsys.readouterr().out
    assert "Failed to load config.json" in out
    assert cfg == config_mod.DEFAULT_CONFIG


def test_setup_logging_creates_log_dir_and_file(tmp_path, monkeypatch):
    # Clean env: ensure logs/ does not exist
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod, "config", {"loglevel": "WARNING"})
    log_dir = tmp_path / "logs"
    log_file = log_dir / "envsbot.log"
    # Remove the logs dir if it already exists for idempotency
    if log_dir.exists():
        for f in log_dir.iterdir():
            f.unlink()
        log_dir.rmdir()
    config_mod.setup_logging()
    assert log_dir.is_dir()
    assert log_file.exists()
    # Check handler levels are set properly
    logger = logging.getLogger()
    assert any(
        h.level == logging.WARNING or h.level == logging.NOTSET
        for h in logger.handlers
    )
