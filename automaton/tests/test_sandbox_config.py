"""Tests for the pure/host-side logic of automaton.sandbox.Sandbox.

These tests do NOT require Charliecloud: they exercise config construction
(``harness_models_config``, ``model_registry``) and the ``_ch_run_prefix``
command-building helper, which only run ``ch-run`` at execution time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path (also covered by conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.agent import Agent, AgentModel
from automaton.config import AutomatonConfig
from automaton.sandbox import Sandbox, DEFAULT_PROXY_API_KEY_ENV


def _write_config(config_path: Path) -> None:
    """Write a minimal temp config consumed by AutomatonConfig.

    Adapted from abc_bench/tests/test_runner_sandboxing.py, with the
    ``results_dir`` default removed (AutomatonConfig has no results_dir).
    """
    config_path.write_text(
        "providers:\n"
        "  demo:\n"
        "    base_url: https://example.com/v1\n"
        "    api_key_env: DEMO_API_KEY\n"
        "    cert: null\n"
        "models:\n"
        "  demo-model:\n"
        "    provider: demo\n"
        "    model: demo/model\n"
        "agents:\n"
        "  demo-agent:\n"
        "    harness: opencode\n"
        "    models:\n"
        "      - demo-model\n"
        "    harness_config: {}\n"
        "defaults:\n"
        "  timeout_seconds: 60\n"
        "  sandbox_network: true\n"
    )


def _make_agent(config_path: Path) -> Agent:
    """Build a real Agent via AutomatonConfig from a temp config file."""
    return Agent("demo-agent", AutomatonConfig(config_path))


def test_harness_models_config_wraps_models_in_sandbox_proxy(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    agent = _make_agent(config_path)

    sandbox = Sandbox()
    config = sandbox.harness_models_config(agent, sandboxed=True)

    assert config["proxy_base_url"] == sandbox.proxy_base_url
    assert config["api_key_env"] == sandbox.proxy_api_key_env
    assert config["proxy_base_url"] is not None

    assert len(config["models"]) == 1
    model_cfg = config["models"][0]
    assert model_cfg["name"] == "demo-model"
    assert model_cfg["base_url"] == sandbox.proxy_base_url
    assert model_cfg["opencode_model"] == f"{sandbox.proxy_provider_id}/demo-model"
    assert model_cfg["proxied_base_url"] == "https://example.com/v1"


def test_harness_models_config_passes_upstream_when_not_sandboxed(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    agent = _make_agent(config_path)

    sandbox = Sandbox()
    config = sandbox.harness_models_config(agent, sandboxed=False)

    assert config["proxy_base_url"] is None
    assert config["api_key_env"] == sandbox.proxy_api_key_env

    model_cfg = config["models"][0]
    assert model_cfg["base_url"] == "https://example.com/v1"
    assert model_cfg["opencode_model"] == f"{sandbox.proxy_provider_id}/demo-model"
    assert model_cfg["proxied_base_url"] == "https://example.com/v1"


def test_harness_models_config_default_api_key_env(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    agent = _make_agent(config_path)

    sandbox = Sandbox()
    config = sandbox.harness_models_config(agent, sandboxed=True)

    assert config["api_key_env"] == DEFAULT_PROXY_API_KEY_ENV
    assert config["api_key_env"] == "AUTOMATON_MODEL_PROXY_API_KEY"


def test_harness_models_config_with_lightweight_stand_in_agent() -> None:
    """A lightweight Agent-like object with .models works directly."""

    class FakeModel:
        def __init__(self, name, config):
            self.name = name
            self.config = config

    class FakeAgent:
        def __init__(self, models):
            self.models = models

    agent = FakeAgent(
        models=[
            FakeModel("alpha", {"model": "provider/alpha", "base_url": "https://alpha.example.com/v1"}),
            FakeModel("beta", {"model": "provider/beta", "base_url": "https://beta.example.com/v1", "api": "responses"}),
        ]
    )

    sandbox = Sandbox()
    config = sandbox.harness_models_config(agent, sandboxed=True)

    assert config["proxy_base_url"] == sandbox.proxy_base_url
    assert len(config["models"]) == 2
    assert config["models"][0]["base_url"] == sandbox.proxy_base_url
    assert config["models"][1]["base_url"] == sandbox.proxy_base_url
    assert config["models"][0]["opencode_model"] == f"{sandbox.proxy_provider_id}/alpha"
    assert config["models"][1]["opencode_model"] == f"{sandbox.proxy_provider_id}/beta"
    assert config["models"][1]["api"] == "responses"


def test_ch_run_prefix_structure(tmp_path: Path) -> None:
    sandbox = Sandbox(host_root=tmp_path / "host", dry_run=False)
    container_path = Path("/containers/opencode/smoke_test")

    cmd = sandbox._ch_run_prefix(container_path)

    assert cmd[:2] == ["ch-run", "--write-fake"]
    assert "--uid=1000" in cmd
    assert "--gid=1000" in cmd
    assert "--unset-env=*" in cmd
    assert any(arg.startswith("--set-env=HOME=/var/tmp/automaton-home") for arg in cmd)
    assert cmd[-1] == str(container_path)


def test_ch_run_prefix_uses_private_tmp(tmp_path: Path) -> None:
    """Every sandboxed command gets a container-private /tmp.

    Without it /tmp is the host node's /tmp, shared by all runs on the node, and
    attempts can see each other's scratch files.
    """
    sandbox = Sandbox(host_root=tmp_path / "host", dry_run=False)

    cmd = sandbox._ch_run_prefix(Path("/containers/opencode/smoke_test"))

    assert "--private-tmp" in cmd


def test_ch_run_prefix_includes_bind_mounts(tmp_path: Path) -> None:
    from automaton.sandbox import BindMount

    sandbox = Sandbox(host_root=tmp_path / "host", dry_run=False)
    container_path = Path("/containers/opencode/smoke_test")
    bind_mounts = [BindMount(tmp_path / "workspace", Path("/workspace"))]

    cmd = sandbox._ch_run_prefix(container_path, bind_mounts=bind_mounts)

    assert any(arg.startswith("--bind=") and "/workspace" in arg for arg in cmd)
    assert cmd[-1] == str(container_path)


def test_model_registry_maps_names_to_config_with_api_key(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    agent = _make_agent(config_path)

    monkeypatch.setenv("DEMO_API_KEY", "dummy-secret")

    sandbox = Sandbox()
    registry = sandbox.model_registry(agent)

    assert "demo-model" in registry
    entry = registry["demo-model"]
    assert entry["api_key"] == "dummy-secret"
    assert entry["opencode_model"] == f"{sandbox.proxy_provider_id}/demo-model"
    assert entry["model"] == "demo/model"
    assert entry["base_url"] == "https://example.com/v1"


def test_model_registry_requires_api_key_env(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    agent = _make_agent(config_path)

    monkeypatch.delenv("DEMO_API_KEY", raising=False)

    sandbox = Sandbox()
    with pytest.raises(RuntimeError, match="DEMO_API_KEY"):
        sandbox.model_registry(agent)
