r"""Desktop-app configuration stored under the user's AppData directory.

The bundled Windows application deliberately keeps secrets out of the
executable.  Operators provision the Speechmatics API key in a small JSON file
under ``%APPDATA%\SwiftMedics``; the floating UI reads that file when Start is
pressed, so the app can stay open while the configuration is edited.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .realtime import (
    DEFAULT_DOMAIN,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_DELAY_MODE,
    DEFAULT_MODEL,
    MAX_MAX_DELAY,
    MIN_MAX_DELAY,
    VALID_DOMAINS,
    VALID_MAX_DELAY_MODES,
    VALID_MODELS,
)

APP_NAME = "SwiftMedics"
CONFIG_FILENAME = "config.json"


class ConfigError(RuntimeError):
    """Raised when the desktop configuration is missing or invalid."""


@dataclass(frozen=True)
class DesktopConfig:
    """Validated settings consumed by the floating desktop app."""

    speechmatics_api_key: str
    #: Demo builds authenticate through the token broker instead of holding a
    #: long-lived API key. When ``token_broker_url`` is set and the API key is
    #: empty, the app fetches a short-lived JWT from the broker on Start.
    token_broker_url: str | None = None
    #: Optional shared secret the broker may require (NOT the API key).
    demo_token: str | None = None
    language: str = "fa"
    model: str = DEFAULT_MODEL
    max_delay: float = DEFAULT_MAX_DELAY
    max_delay_mode: str = DEFAULT_MAX_DELAY_MODE
    domain: str = DEFAULT_DOMAIN
    inject: bool = True
    medical_layer: bool = True
    medical_vocab: bool = True
    focus_guard: bool = True
    entity_guard: bool = True
    device_index: int | None = None
    save_report: bool = False


@dataclass(frozen=True)
class ConfigLoadResult:
    """Configuration plus the path it was read from."""

    config: DesktopConfig
    path: Path



def app_data_dir() -> Path:
    r"""Return the per-user directory for desktop-app config/logs.

    On Windows this is exactly ``%APPDATA%\SwiftMedics``.  A non-Windows
    fallback keeps the module importable and testable in CI/development.
    """

    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    if sys.platform == "darwin":  # pragma: no cover - Windows app, dev fallback
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME



def config_path() -> Path:
    """Default desktop configuration path."""

    return app_data_dir() / CONFIG_FILENAME



def log_dir() -> Path:
    """Default desktop logging directory."""

    return app_data_dir() / "logs"



def default_config_payload() -> dict[str, Any]:
    """Template written when the config file is missing."""

    return {
        "speechmatics_api_key": "YOUR_SPEECHMATICS_API_KEY",
        "token_broker_url": "",
        "demo_token": "",
        "language": "fa",
        "model": DEFAULT_MODEL,
        "max_delay": DEFAULT_MAX_DELAY,
        "max_delay_mode": DEFAULT_MAX_DELAY_MODE,
        "domain": DEFAULT_DOMAIN,
        "inject": True,
        "medical_layer": True,
        "medical_vocab": True,
        "focus_guard": True,
        "entity_guard": True,
        "device_index": None,
        "save_report": False,
    }



def ensure_template(path: Path | None = None) -> Path:
    """Create a template config file if it does not already exist."""

    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            json.dumps(default_config_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return target



def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file not found. Create {path} and set speechmatics_api_key."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object.")
    return data



def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    raise ConfigError(f"Config key '{key}' must be true or false.")



def _str_choice(data: dict[str, Any], key: str, default: str, choices: tuple[str, ...]) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"Config key '{key}' must be a string.")
    value = value.strip().lower()
    if value not in choices:
        raise ConfigError(f"Config key '{key}' must be one of {choices}; got {value!r}.")
    return value



def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key, None)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Config key '{key}' must be an integer or null.")
    return value



def parse_config(data: dict[str, Any]) -> DesktopConfig:
    """Validate raw JSON and return a ``DesktopConfig``."""

    api_key = data.get("speechmatics_api_key")
    has_key = (
        isinstance(api_key, str)
        and api_key.strip()
        and api_key.strip() != "YOUR_SPEECHMATICS_API_KEY"
    )

    def _optional_str(key: str) -> str | None:
        value = data.get(key, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ConfigError(f"Config key '{key}' must be a string.")
        return value.strip()

    token_broker_url = _optional_str("token_broker_url")
    demo_token = _optional_str("demo_token")
    if not has_key and not token_broker_url:
        raise ConfigError(
            "speechmatics_api_key is missing. Set it in the SwiftMedics config file "
            "(demo builds set token_broker_url instead)."
        )

    language = data.get("language", "fa")
    if not isinstance(language, str):
        raise ConfigError("Config key 'language' must be a string.")
    language = language.strip().lower() or "fa"
    if language != "fa":
        raise ConfigError("The desktop app is currently configured for Persian only: language must be 'fa'.")

    model = _str_choice(data, "model", DEFAULT_MODEL, VALID_MODELS)
    max_delay_mode = _str_choice(
        data, "max_delay_mode", DEFAULT_MAX_DELAY_MODE, VALID_MAX_DELAY_MODES
    )
    domain = _str_choice(data, "domain", DEFAULT_DOMAIN, VALID_DOMAINS)

    raw_delay = data.get("max_delay", DEFAULT_MAX_DELAY)
    if isinstance(raw_delay, bool) or not isinstance(raw_delay, (int, float)):
        raise ConfigError("Config key 'max_delay' must be a number.")
    max_delay = float(raw_delay)
    if not (MIN_MAX_DELAY <= max_delay <= MAX_MAX_DELAY):
        raise ConfigError(
            f"Config key 'max_delay' must be between {MIN_MAX_DELAY} and {MAX_MAX_DELAY}."
        )

    return DesktopConfig(
        speechmatics_api_key=api_key.strip() if has_key else "",
        token_broker_url=token_broker_url,
        demo_token=demo_token,
        language=language,
        model=model,
        max_delay=max_delay,
        max_delay_mode=max_delay_mode,
        domain=domain,
        inject=_bool(data, "inject", True),
        medical_layer=_bool(data, "medical_layer", True),
        medical_vocab=_bool(data, "medical_vocab", True),
        focus_guard=_bool(data, "focus_guard", True),
        entity_guard=_bool(data, "entity_guard", True),
        device_index=_optional_int(data, "device_index"),
        save_report=_bool(data, "save_report", False),
    )



def load_desktop_config(path: Path | None = None, *, create_template: bool = True) -> ConfigLoadResult:
    """Load and validate desktop configuration.

    If ``create_template`` is true and the file is missing, a template is
    created first and ``ConfigError`` still asks the operator to fill in the
    API key.  This gives non-technical users a concrete file to edit.
    """

    target = path or config_path()
    if create_template and not target.exists():
        ensure_template(target)
        raise ConfigError(
            f"Config template created at {target}. Open it, set speechmatics_api_key, then press Start again."
        )
    return ConfigLoadResult(config=parse_config(_read_json(target)), path=target)
