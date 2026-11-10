"""Settings and rule presets.

`chase.yaml` names a preset from `config/presets/` and may override any rule key. Everything is
validated up front so a typo fails loudly at startup instead of silently changing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Invalid or unknown configuration."""


@dataclass(frozen=True)
class RuleConfig:
    name: str
    confidence_threshold: float
    episode_min_minutes: float
    max_gap_seconds: float
    min_separation_minutes: float
    episodes_per_window: int
    violation_window_hours: float
    citation: str
    caveat: str
    addressee: str
    min_loudness_dbfs: float | None = None


@dataclass(frozen=True)
class Settings:
    rules: RuleConfig
    audio_device: str | int | None = None
    capture_rate: int = 48000
    window_seconds: float = 0.96
    hop_seconds: float = 0.48
    pre_roll_seconds: float = 30.0
    post_roll_seconds: float = 30.0
    spool_minutes: float = 120.0
    backlog_days: int = 90
    timezone: str = "America/Chicago"
    data_dir: Path = Path("/data")
    model_path: Path = Path("/models/yamnet.tflite")
    class_map_path: Path = Path("/models/yamnet_class_map.csv")
    metrics_port: int = 9100
    device_label: str = "USB microphone"


_RULE_KEYS = {f.name for f in fields(RuleConfig)}
_SETTING_KEYS = {f.name for f in fields(Settings)} - {"rules"}
_PATH_KEYS = {"data_dir", "model_path", "class_map_path"}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _build_rules(data: dict[str, Any], source: str) -> RuleConfig:
    unknown = set(data) - _RULE_KEYS
    if unknown:
        raise ConfigError(f"{source}: unknown rule key(s): {', '.join(sorted(unknown))}")
    missing = {f.name for f in fields(RuleConfig)} - {"min_loudness_dbfs"} - set(data)
    if missing:
        raise ConfigError(f"{source}: missing rule key(s): {', '.join(sorted(missing))}")
    rc = RuleConfig(**data)
    _validate_rules(rc)
    return rc


def _validate_rules(rc: RuleConfig) -> None:
    if not 0 < rc.confidence_threshold <= 1:
        raise ConfigError("confidence_threshold must be in (0, 1]")
    for key in ("episode_min_minutes", "max_gap_seconds", "violation_window_hours"):
        if getattr(rc, key) <= 0:
            raise ConfigError(f"{key} must be > 0")
    if rc.min_separation_minutes < 0:
        raise ConfigError("min_separation_minutes must be >= 0")
    if not isinstance(rc.episodes_per_window, int) or rc.episodes_per_window < 1:
        raise ConfigError("episodes_per_window must be an integer >= 1")


def load_preset(path: Path) -> RuleConfig:
    return _build_rules(_read_yaml(path), str(path))


def _validate_settings(s: Settings) -> None:
    if s.capture_rate <= 0 or s.capture_rate % 16000 != 0:
        raise ConfigError("capture_rate must be a positive multiple of 16000")
    for key in ("window_seconds", "hop_seconds", "spool_minutes"):
        if getattr(s, key) <= 0:
            raise ConfigError(f"{key} must be > 0")
    if s.hop_seconds > s.window_seconds:
        raise ConfigError("hop_seconds must be <= window_seconds")
    for key in ("pre_roll_seconds", "post_roll_seconds"):
        if getattr(s, key) < 0:
            raise ConfigError(f"{key} must be >= 0")
    if s.backlog_days < 1:
        raise ConfigError("backlog_days must be >= 1")
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"timezone: unknown zone {s.timezone!r}") from e


def load_settings(path: Path, presets_dir: Path) -> Settings:
    data = _read_yaml(path)
    rules_block = data.pop("rules", None)
    if not isinstance(rules_block, dict) or "preset" not in rules_block:
        raise ConfigError(f"{path}: 'rules.preset' is required")
    rules_block = dict(rules_block)
    preset_name = rules_block.pop("preset")
    preset_path = presets_dir / f"{preset_name}.yaml"
    if not preset_path.exists():
        raise ConfigError(f"unknown preset {preset_name!r} (looked for {preset_path})")
    merged = _read_yaml(preset_path) | rules_block
    rules = _build_rules(merged, f"{path} (preset {preset_name})")

    unknown = set(data) - _SETTING_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {k: (Path(v) if k in _PATH_KEYS else v) for k, v in data.items()}
    settings = Settings(rules=rules, **kwargs)
    _validate_settings(settings)
    return settings
