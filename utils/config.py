import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import available_timezones

import slixmpp


# project root
BASE_DIR = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG = {
    "prefix": ",",
    "loglevel": "INFO",
    "db": "bot.db",
}

REQUIRED_CONFIG_KEYS = {
    "jid": str,
    "password": str,
    "owner": str,
    "nick": str,
}

OPTIONAL_CONFIG_TYPES = {
    "prefix": str,
    "loglevel": str,
    "db": str,
    "stop_cmd": list,
    "admins": list,
    "avatar": str,
    "avatar_type": str,
    "timezone": str,
    "host": str,
    "port": int,
    "rss_global_query_interval": int,
    "max_new_feed_entries": int,
}


class ConfigError(Exception):
    """Raised when config.json is invalid or incomplete."""


def _format_json_error(error: json.JSONDecodeError) -> str:
    return (
        "Failed to parse config.json at "
        f"line {error.lineno}, column {error.colno}: {error.msg}"
    )


def _validate_string(value, key, errors, allow_empty=False):
    if not isinstance(value, str):
        errors.append(f"{key}: expected string, got {type(value).__name__}")
        return

    if not allow_empty and not value.strip():
        errors.append(f"{key}: must not be empty")


def _validate_jid(value, key, errors):
    """Validate a config value as a user JID with localpart and domain."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key}: must be a non-empty JID string")
        return

    try:
        jid = slixmpp.JID(value)
    except Exception as e:
        errors.append(f"{key}: invalid JID ({e})")
        return

    if not jid.user or not jid.domain:
        errors.append(
            f"{key}: must include localpart and domain, e.g. user@example.org")


def _validate_numeric_ranges(cfg, errors):
    if "rss_global_query_interval" in cfg:
        value = cfg["rss_global_query_interval"]
        if isinstance(value, int) and value <= 0:
            errors.append("rss_global_query_interval: must be greater than 0")

    if "max_new_feed_entries" in cfg:
        value = cfg["max_new_feed_entries"]
        if isinstance(value, int) and value < 0:
            errors.append("max_new_feed_entries: must be 0 or greater")

    if "port" in cfg:
        value = cfg["port"]
        if isinstance(value, int) and not (1 <= value <= 65535):
            errors.append("port: must be between 1 and 65535")


def _validate_timezone(cfg, errors):
    if "timezone" not in cfg:
        return

    timezone = cfg["timezone"]
    if not isinstance(timezone, str):
        return

    if timezone not in available_timezones():
        errors.append(
            "timezone: must be a valid IANA timezone, e.g. Europe/Berlin")


def _validate_avatar(cfg, errors, warnings):
    avatar = cfg.get("avatar")
    avatar_type = cfg.get("avatar_type")

    if avatar_type and avatar_type not in ("image/png", "image/jpeg"):
        errors.append("avatar_type: must be image/png or image/jpeg")

    if avatar and avatar_type:
        suffix = Path(avatar).suffix.lower()

        if avatar_type == "image/png" and suffix != ".png":
            warnings.append(
                "avatar: file extension does not match avatar_type image/png")

        if avatar_type == "image/jpeg" and suffix not in (".jpg", ".jpeg"):
            warnings.append(
                "avatar: file extension does not match avatar_type image/jpeg")

    if avatar:
        avatar_path = Path(avatar)
        if not avatar_path.is_absolute():
            avatar_path = BASE_DIR / avatar_path

        if not avatar_path.exists():
            warnings.append(f"avatar: file does not exist: {avatar_path}")


def collect_config_warnings(cfg):
    """Return non-fatal config warnings."""
    warnings = []

    if not isinstance(cfg, dict):
        return warnings

    _validate_avatar(cfg, [], warnings)
    return warnings


def check_required_keys(cfg):
    errors = []
    for key, expected_type in REQUIRED_CONFIG_KEYS.items():
        if key not in cfg:
            errors.append(f"Missing required key: {key}")
            continue

        if expected_type is str:
            _validate_string(cfg[key], key, errors)
        elif not isinstance(cfg[key], expected_type):
            errors.append(
                f"{key}: expected {expected_type.__name__}, "
                f"got {type(cfg[key]).__name__}"
            )
    return errors


def check_optional_keys(cfg):
    errors = []
    for key, expected_type in OPTIONAL_CONFIG_TYPES.items():
        if key not in cfg:
            continue

        value = cfg[key]

        if expected_type is str:
            _validate_string(value, key, errors)
        elif not isinstance(value, expected_type):
            errors.append(
                f"{key}: expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
    return errors


def validate_config(cfg, require_required_keys=False):
    """
    Validate envsbot configuration.

    Parameters
    ----------
    cfg : dict
        Configuration dictionary to validate.
    require_required_keys : bool
        If True, require runtime keys such as jid/password/owner/nick.
        Tests and helper imports may keep this False, while the real bot
        startup should use True.

    Raises
    ------
    ConfigError
        If the configuration is invalid.
    """
    errors = []
    warnings = []

    if not isinstance(cfg, dict):
        raise ConfigError(
            "config.json must contain a JSON object at top level")

    if require_required_keys:
        errors = check_required_keys(cfg)

        if "jid" in cfg:
            _validate_jid(cfg["jid"], "jid", errors)

        if "owner" in cfg:
            _validate_jid(cfg["owner"], "owner", errors)

    errors.extend(check_optional_keys(cfg))

    if ("prefix" in cfg and isinstance(cfg["prefix"], str) and
            not cfg["prefix"]):
        errors.append("prefix: must not be empty")

    if "loglevel" in cfg and isinstance(cfg["loglevel"], str):
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if cfg["loglevel"].upper() not in valid_levels:
            errors.append(
                "loglevel: must be one of "
                f"{', '.join(sorted(valid_levels))}"
            )

    if "admins" in cfg and isinstance(cfg["admins"], list):
        for idx, admin in enumerate(cfg["admins"]):
            if not isinstance(admin, str) or not admin.strip():
                errors.append(f"admins[{idx}]: must be a non-empty string")
                continue

            _validate_jid(admin, f"admins[{idx}]", errors)

    _validate_timezone(cfg, errors)
    _validate_avatar(cfg, errors, warnings)
    _validate_numeric_ranges(cfg, errors)

    if errors:
        raise ConfigError(
            "Invalid config.json:\n- " + "\n- ".join(errors)
        )


def load_config(require_required_keys=False):
    """
    Load config.json and validate it.

    Missing config.json keeps the historical default behavior so tests and
    helper imports still work. A present but broken config.json is always
    fatal because continuing with defaults can make the bot crash later in
    confusing ways.
    """
    cfg = DEFAULT_CONFIG.copy()
    config_path = BASE_DIR / "config.json"

    if not config_path.exists():
        if require_required_keys:
            raise ConfigError(f"Missing config file: {config_path}")

        validate_config(cfg, require_required_keys=False)
        return cfg

    try:
        with open(config_path, encoding="utf-8") as f:
            loaded = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(_format_json_error(e)) from e
    except Exception as e:
        raise ConfigError(f"Failed to load config.json: {e}") from e

    if not isinstance(loaded, dict):
        raise ConfigError(
            "config.json must contain a JSON object at top level")

    cfg.update(loaded)
    validate_config(cfg, require_required_keys=require_required_keys)
    return cfg


def validate_startup_config(cfg=None):
    """
    Validate the effective runtime config before starting the bot.

    This should be called by envsbot.py before Bot() is constructed so
    configuration mistakes produce a clear error instead of a restart loop.
    """
    if cfg is None:
        cfg = config

    validate_config(cfg, require_required_keys=True)

    for warning in collect_config_warnings(cfg):
        print(f"[CONFIG] Warning: {warning}", file=sys.stderr)


def exit_on_config_error(error):
    """Print a readable config error and terminate startup."""
    print(f"[CONFIG] {error}", file=sys.stderr)
    raise SystemExit(1) from error


# global config object (backwards compatible)
try:
    config = load_config(require_required_keys=False)
except ConfigError as e:
    exit_on_config_error(e)


def setup_logging():
    """
    Initialize the logging system.
    """
    log_level = getattr(logging, config.get(
        "loglevel", "INFO").upper(), logging.INFO)

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    log_file = log_dir / "envsbot.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,  # 2 MB
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=log_level,
        handlers=[console, file_handler],
    )


if __name__ == "__main__":
    try:
        validate_startup_config()
    except ConfigError as e:
        exit_on_config_error(e)

    print("[CONFIG] config.json is valid")
