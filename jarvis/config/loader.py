"""Load and validate YAML configs from a directory."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jarvis.config.schema import ChannelsConfig, JarvisConfig, MCPServersConfig

_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigLoadError(Exception):
    """Raised for any failure to load / validate config."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    jarvis: JarvisConfig
    channels: ChannelsConfig
    mcp_servers: MCPServersConfig


def expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references. Missing vars raise ConfigLoadError."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigLoadError(f"environment variable {name!r} is not set")
            return os.environ[name]

        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _load_yaml_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigLoadError(f"required config file not found: {path.name}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigLoadError(f"{path.name}: YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"{path.name}: top-level must be a mapping")
    return raw


def load_config(config_dir: Path | str) -> LoadedConfig:
    config_dir = Path(config_dir)

    jarvis_raw = expand_env(_load_yaml_file(config_dir / "jarvis.yaml"))
    # channels.yaml and mcp-servers.yaml are optional — loader tolerates
    # missing files and produces empty defaults so partial deployments work.
    channels_path = config_dir / "channels.yaml"
    mcp_path = config_dir / "mcp-servers.yaml"
    channels_raw = expand_env(_load_yaml_file(channels_path)) if channels_path.exists() else {}
    mcp_raw = expand_env(_load_yaml_file(mcp_path)) if mcp_path.exists() else {}

    try:
        jarvis_cfg = JarvisConfig.model_validate(jarvis_raw)
        channels_cfg = ChannelsConfig.model_validate(channels_raw)
        mcp_cfg = MCPServersConfig.model_validate(mcp_raw)
    except Exception as e:  # pydantic ValidationError or similar
        raise ConfigLoadError(str(e)) from e

    return LoadedConfig(jarvis=jarvis_cfg, channels=channels_cfg, mcp_servers=mcp_cfg)
