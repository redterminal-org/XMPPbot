import json
import logging

import pytest

import utils.config as config_mod


def test_load_config_returns_defaults_when_missing_and_not_strict(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    result = config_mod.load_config()

    assert result == config_mod.DEFAULT_CONFIG


def test_load_config_missing_file_strict_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load_config(require_required_keys=True)

    assert "Missing config file" in str(exc.value)


def test_load_config_loads_json(tmp_path, monkeypatch):
    data = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "prefix": ";",
        "loglevel": "DEBUG",
        "custom": "extra",
    }
    (tmp_path / "config.json").write_text(json.dumps(data))

    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    result = config_mod.load_config(require_required_keys=True)

    for k, v in data.items():
        assert result[k] == v


def test_load_config_with_partial_override_when_not_strict(tmp_path, monkeypatch):
    data = {"prefix": ";"}
    (tmp_path / "config.json").write_text(json.dumps(data))

    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    result = config_mod.load_config()

    assert result["prefix"] == ";"
    assert result["loglevel"] == "INFO"
    assert result["db"] == "bot.db"


def test_load_config_bad_json_raises(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{this_is:not:json,]")

    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load_config()

    msg = str(exc.value)
    assert "Failed to parse config.json" in msg
    assert "line" in msg
    assert "column" in msg


def test_load_config_top_level_must_be_object(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(["not", "an", "object"]))

    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load_config()

    assert "must contain a JSON object" in str(exc.value)


def test_validate_startup_config_requires_runtime_keys():
    cfg = {
        "prefix": ",",
        "loglevel": "INFO",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_startup_config(cfg)

    msg = str(exc.value)
    assert "Missing required key: jid" in msg
    assert "Missing required key: password" in msg
    assert "Missing required key: owner" in msg
    assert "Missing required key: nick" in msg


def test_validate_startup_config_accepts_valid_config():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "prefix": ",",
        "loglevel": "INFO",
        "db": "bot.db",
    }

    config_mod.validate_startup_config(cfg)


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("jid", "", "jid: must not be empty"),
        ("password", "", "password: must not be empty"),
        ("owner", "", "owner: must not be empty"),
        ("nick", "", "nick: must not be empty"),
    ],
)
def test_validate_startup_config_rejects_empty_required_strings(key, value, expected):
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
    }
    cfg[key] = value

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_startup_config(cfg)

    assert expected in str(exc.value)


def test_validate_config_rejects_invalid_loglevel():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "loglevel": "VERBOSE",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "loglevel: must be one of" in str(exc.value)


def test_validate_config_rejects_invalid_avatar_type():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "avatar_type": "image/gif",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "avatar_type: must be image/png or image/jpeg" in str(exc.value)


def test_validate_config_rejects_invalid_admins_type():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "admins": "admin@example.org",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "admins: expected list" in str(exc.value)


def test_validate_config_rejects_invalid_admin_entry():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "admins": ["admin@example.org", ""],
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "admins[1]: must be a non-empty string" in str(exc.value)


def test_validate_config_rejects_wrong_optional_types():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "rss_global_query_interval": "1200",
        "max_new_feed_entries": "5",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    msg = str(exc.value)
    assert "rss_global_query_interval: expected int" in msg
    assert "max_new_feed_entries: expected int" in msg


def test_exit_on_config_error_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        config_mod.exit_on_config_error(config_mod.ConfigError("broken config"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "[CONFIG] broken config" in err


def test_setup_logging_creates_log_dir_and_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod, "config", {"loglevel": "WARNING"})

    log_dir = tmp_path / "logs"
    log_file = log_dir / "envsbot.log"

    if log_dir.exists():
        for f in log_dir.iterdir():
            f.unlink()
        log_dir.rmdir()

    config_mod.setup_logging()

    assert log_dir.is_dir()
    assert log_file.exists()

    logger = logging.getLogger()
    assert any(
        h.level == logging.WARNING or h.level == logging.NOTSET
        for h in logger.handlers
    )

def test_validate_startup_config_rejects_invalid_bot_jid():
    cfg = {
        "jid": "not-a-jid",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_startup_config(cfg)

    assert "jid:" in str(exc.value)


def test_validate_startup_config_rejects_invalid_owner_jid():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "not-a-jid",
        "nick": "envsbot",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_startup_config(cfg)

    assert "owner:" in str(exc.value)


def test_validate_config_rejects_invalid_admin_jid():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "admins": ["admin@example.org", "not-a-jid"],
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "admins[1]:" in str(exc.value)


def test_validate_config_accepts_host_and_port():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "host": "xmpp.example.org",
        "port": 5222,
    }

    config_mod.validate_config(cfg, require_required_keys=True)


def test_validate_config_rejects_invalid_host_type():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "host": 123,
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "host: expected string" in str(exc.value)


def test_validate_config_rejects_invalid_port_type():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "port": "5222",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "port: expected int" in str(exc.value)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_validate_config_rejects_invalid_port_range(port):
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "port": port,
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "port: must be between 1 and 65535" in str(exc.value)


def test_validate_config_rejects_invalid_timezone():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "timezone": "Mars/Olympus_Mons",
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "timezone: must be a valid IANA timezone" in str(exc.value)


def test_validate_config_accepts_valid_timezone():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "timezone": "Europe/Berlin",
    }

    config_mod.validate_config(cfg, require_required_keys=True)


def test_validate_config_rejects_non_positive_rss_interval():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "rss_global_query_interval": 0,
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "rss_global_query_interval: must be greater than 0" in str(exc.value)


def test_validate_config_rejects_negative_max_new_feed_entries():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "max_new_feed_entries": -1,
    }

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.validate_config(cfg, require_required_keys=True)

    assert "max_new_feed_entries: must be 0 or greater" in str(exc.value)


def test_validate_config_accepts_zero_max_new_feed_entries():
    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "max_new_feed_entries": 0,
    }

    config_mod.validate_config(cfg, require_required_keys=True)


def test_collect_config_warnings_for_missing_avatar(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    cfg = {
        "avatar": "missing.png",
        "avatar_type": "image/png",
    }

    warnings = config_mod.collect_config_warnings(cfg)

    assert any("avatar: file does not exist" in warning for warning in warnings)


def test_collect_config_warnings_for_avatar_extension_mismatch():
    cfg = {
        "avatar": "avatar.jpg",
        "avatar_type": "image/png",
    }

    warnings = config_mod.collect_config_warnings(cfg)

    assert any("file extension does not match avatar_type image/png" in warning for warning in warnings)


def test_validate_startup_config_prints_avatar_warnings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)

    cfg = {
        "jid": "bot@example.org",
        "password": "secret",
        "owner": "owner@example.org",
        "nick": "envsbot",
        "avatar": "missing.png",
        "avatar_type": "image/png",
    }

    config_mod.validate_startup_config(cfg)

    captured = capsys.readouterr()
    assert "[CONFIG] Warning: avatar: file does not exist" in captured.err
