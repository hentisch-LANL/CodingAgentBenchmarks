"""GitLab trigger API client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from .config import LauncherConfig


@dataclass(frozen=True)
class TriggerRequest:
    """Normalized request payload for a single GitLab benchmark trigger."""

    task: str
    agent: str
    task_group: str | None = None
    timeout_seconds: int | None = None
    run_group: str | None = None
    run_label: str | None = None
    key_env_names: list[str] | None = None
    attempts: int = 1
    nodes: int | None = None
    dev: bool = False
    ref: str | None = None

    def variables(self) -> dict[str, str]:
        """Return GitLab CI variables for this trigger request."""

        variables = {
            "AGENT": self.agent,
        }
        if self.task_group is not None:
            variables["TASK_GROUP"] = self.task_group
        else:
            variables["TASK"] = self.task
        if self.timeout_seconds is not None:
            variables["TIMEOUT_SECONDS"] = str(self.timeout_seconds)
        if self.run_group:
            variables["RUN_GROUP"] = self.run_group
        if self.run_label:
            variables["RUN_LABEL"] = self.run_label
        if self.attempts and self.attempts != 1:
            variables["ATTEMPTS"] = str(self.attempts)
        if self.nodes is not None:
            variables["NODES"] = str(self.nodes)
        if self.dev:
            variables["DEV"] = "1"

        # Add API keys from environment
        if self.key_env_names:
            for env_name in self.key_env_names:
                value = os.environ.get(env_name)
                if value:
                    variables[env_name] = value

        return variables


class GitLabTriggerClient:
    """Small client for the GitLab pipeline trigger endpoint."""

    def __init__(self, config: LauncherConfig):
        self.config = config

    @property
    def trigger_url(self) -> str:
        return f"{self.config.gitlab_base_url}/api/v4/projects/{self.config.gitlab_project_id}/trigger/pipeline"

    def build_form_data(self, request: TriggerRequest, dry_run: bool = False) -> dict[str, str]:
        """Build GitLab trigger form data from a normalized request."""

        token = self.config.trigger_token
        if not token:
            if not dry_run:
                raise ValueError(f"environment variable {self.config.trigger_token_env} is not set")
            token = "<unset>"

        data = {
            "token": token,
            "ref": request.ref or self.config.gitlab_ref,
        }

        # Check for missing API keys before building request
        if request.key_env_names and not dry_run:
            missing = [name for name in request.key_env_names if not os.environ.get(name)]
            if missing:
                raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        for key, value in request.variables().items():
            data[f"variables[{key}]"] = value
        return data

    def trigger(self, request: TriggerRequest, dry_run: bool = False) -> dict[str, Any]:
        """Submit a trigger request or return the masked dry-run payload."""

        try:
            form_data = self.build_form_data(request, dry_run=dry_run)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
            }

        # Hide sensitive values in logs
        secret_fields = {f"variables[{name}]" for name in request.key_env_names or []}
        safe_data = {
            key: ("<hidden>" if (key == "token" or key in secret_fields or "KEY" in key.upper() or "SECRET" in key.upper()) else value)
            for key, value in form_data.items()
        }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "url": self.trigger_url,
                "form_data": safe_data,
            }

        response = requests.post(self.trigger_url, data=form_data, timeout=30)
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text}

        if not response.ok:
            return {
                "success": False,
                "status_code": response.status_code,
                "error": payload,
            }

        return {
            "success": True,
            "status_code": response.status_code,
            "pipeline_id": payload.get("id") if isinstance(payload, dict) else None,
            "web_url": payload.get("web_url") if isinstance(payload, dict) else None,
            "api_response": payload,
        }
