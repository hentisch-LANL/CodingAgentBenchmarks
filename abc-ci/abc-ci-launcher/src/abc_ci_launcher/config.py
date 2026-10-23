"""Configuration loading for abc-ci-launcher."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_DIR = Path.home() / ".abc-bench" / "abc-ci"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"

CONFIG_TEMPLATE = """# abc-ci-launcher configuration
#
# This file contains only GitLab trigger settings and launcher defaults.
# Do not put model provider API keys here. Provider keys belong in GitLab CI
# variables used by the abc-ci pipeline.

gitlab:
  base_url: "https://gitlab.example.org"
  project_id: "TODO"
  ref: "main"
  # Branch used when triggering with --dev. Pipelines on this branch archive to
  # ABC_BENCH_RESULTS_DIR_DEV and publish a separate Pages site.
  dev_ref: "dev"
  trigger_token_env: "ABC_CI_TRIGGER_TOKEN"

defaults:
  agent: "opencode-default"
  timeout_seconds: null
"""


@dataclass(frozen=True)
class LauncherConfig:
    """Launcher-side GitLab trigger configuration."""

    path: Path
    data: dict[str, Any]

    @property
    def gitlab_base_url(self) -> str:
        return str(self.data.get("gitlab", {}).get("base_url", "")).rstrip("/")

    @property
    def gitlab_project_id(self) -> str:
        return str(self.data.get("gitlab", {}).get("project_id", ""))

    @property
    def gitlab_ref(self) -> str:
        return str(self.data.get("gitlab", {}).get("ref", "main"))

    @property
    def gitlab_dev_ref(self) -> str:
        return str(self.data.get("gitlab", {}).get("dev_ref", "dev"))

    @property
    def trigger_token_env(self) -> str:
        return str(self.data.get("gitlab", {}).get("trigger_token_env", "ABC_CI_TRIGGER_TOKEN"))

    @property
    def trigger_token(self) -> str | None:
        return os.environ.get(self.trigger_token_env)

    @property
    def default_agent(self) -> str | None:
        value = self.data.get("defaults", {}).get("agent")
        return str(value) if value else None

    @property
    def default_timeout_seconds(self) -> int | None:
        value = self.data.get("defaults", {}).get("timeout_seconds")
        if value is None or value == "":
            return None
        return int(value)

    def validate(self, require_token: bool = True) -> list[str]:
        errors: list[str] = []
        if not self.gitlab_base_url:
            errors.append("gitlab.base_url is required")
        if not self.gitlab_project_id or self.gitlab_project_id == "TODO":
            errors.append("gitlab.project_id is required")
        if not self.gitlab_ref:
            errors.append("gitlab.ref is required")
        if not self.trigger_token_env:
            errors.append("gitlab.trigger_token_env is required")
        if require_token and not self.trigger_token:
            errors.append(f"environment variable {self.trigger_token_env} is not set")
        return errors


def init_config(path: Path = DEFAULT_CONFIG_PATH, force: bool = False) -> Path:
    """Create a starter launcher config file."""

    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise FileExistsError(f"config already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path


def load_config(path: Path | None = None) -> LauncherConfig:
    """Load the launcher config, creating a starter file on first use."""

    config_path = (path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    if not config_path.is_file():
        # Auto-initialize config on first run
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"Created config file: {config_path}")
        print("Edit this file to configure GitLab project settings.")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {config_path}")
    return LauncherConfig(path=config_path, data=data)
