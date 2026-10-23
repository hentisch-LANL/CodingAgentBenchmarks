"""Configuration loading and management for Automaton."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import importlib.resources as resources

import yaml


DEFAULT_CONFIG_DIR = Path("~/.automaton")
DEFAULT_CONFIG_PATH = Path("~/.automaton/config.yaml")
DEFAULT_CONFIG_TEMPLATE = """# Automaton Configuration
#
# This config is content-free: it does not own tasks, harnesses, or container
# assets. A benchmark orchestrator supplies those as inputs and may subclass
# AutomatonConfig to re-add data_dir/results_dir if it needs them.

providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null
    # api: the upstream API style for this provider. Each harness defines how
    #      it wires a style ("chat_completions" (default) | "responses" |
    #      "anthropic_messages"), and the model proxy uses it to pick the
    #      upstream authentication scheme: "anthropic_messages" authenticates
    #      with x-api-key, the others with Authorization: Bearer.
    #      Can be overridden per-model. Leave unset for the harness default.
    # api: chat_completions

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null
    api: anthropic_messages

models:
  example-model:
    provider: example
    model: your-model-id
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0

  anthropic-claude:
    provider: anthropic
    model: anthropic/claude-sonnet-4
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0

agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}

defaults:
  timeout_seconds: 1800
  sandbox_network: true
"""


def initialize_user_config() -> Path:
    """Create the default user config directory and template if missing.

    Returns
    -------
    Path
        Path to the created config file

    Examples
    --------
    >>> initialize_user_config()  # doctest: +SKIP
    PosixPath('/home/user/.automaton/config.yaml')
    """
    config_dir = DEFAULT_CONFIG_DIR.expanduser()
    config_path = DEFAULT_CONFIG_PATH.expanduser()

    config_dir.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

    return config_path


def _normalize_base_url(base_url: str | None) -> str | None:
    """Normalize a base URL by ensuring it ends with /v1.

    Parameters
    ----------
    base_url : str or None
        URL to normalize, or None

    Returns
    -------
    str or None
        Normalized URL, or None if input was None

    Examples
    --------
    >>> _normalize_base_url("https://api.example.com")
    'https://api.example.com/v1'
    >>> _normalize_base_url("https://api.example.com/v1")
    'https://api.example.com/v1'
    >>> _normalize_base_url(None)
    None
    """
    if base_url is None:
        return None

    # Strip trailing slashes
    url = base_url.rstrip("/")

    # Add /v1 if not already present
    if not url.endswith("/v1"):
        url = f"{url}/v1"

    return url


def _normalize_cost(cost_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize model pricing into per-token values."""
    if not cost_config:
        return None

    input_per_1m = float(cost_config.get("input_per_1m", 0) or 0)
    cache_read_input_per_1m = float(
        cost_config.get(
            "cache_read_input_per_1m",
            cost_config.get("cached_input_per_1m", input_per_1m),
        )
        or input_per_1m
    )
    cache_write_input_per_1m = float(cost_config.get("cache_write_input_per_1m", 0) or 0)
    output_per_1m = float(cost_config.get("output_per_1m", 0) or 0)
    currency = str(cost_config.get("currency", "USD"))

    cache_write_input_per_token_by_ttl: dict[str, float] = {}
    for key, value in cost_config.items():
        if not key.startswith("cache_write_input_") or not key.endswith("_per_1m"):
            continue
        if key == "cache_write_input_per_1m":
            continue
        ttl = key[len("cache_write_input_") : -len("_per_1m")]
        cache_write_input_per_token_by_ttl[ttl] = float(value or 0) / 1_000_000

    return {
        "currency": currency,
        "input_per_token": input_per_1m / 1_000_000,
        "cache_read_input_per_token": cache_read_input_per_1m / 1_000_000,
        "cached_input_per_token": cache_read_input_per_1m / 1_000_000,
        "cache_write_input_per_token": cache_write_input_per_1m / 1_000_000,
        "cache_write_input_per_token_by_ttl": dict(sorted(cache_write_input_per_token_by_ttl.items())),
        "output_per_token": output_per_1m / 1_000_000,
    }


class AutomatonConfig:
    """Automaton configuration.

    Loads and manages provider-based model configuration from YAML file.
    Providers define API endpoints and authentication once, then models
    reference providers by name.

    Attributes
    ----------
    config_path : Path
        Path to the loaded configuration file
    data : dict
        Raw configuration data

    Examples
    --------
    >>> config = AutomatonConfig()  # doctest: +SKIP
    >>> model = config.get_model("example-model")  # doctest: +SKIP
    >>> model["provider"]
    'example'
    """

    def __init__(self, config_path: Path | None = None):
        """Load configuration from file or use defaults.

        Parameters
        ----------
        config_path : Path, optional
            Path to YAML config file. If None, uses default.

        Raises
        ------
        FileNotFoundError
            If config file not found
        """
        if config_path is None:
            config_path = initialize_user_config()

        self.config_path = Path(config_path).expanduser().resolve()
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path) as f:
            self.data = yaml.safe_load(f)

    @property
    def providers(self) -> dict[str, Any]:
        """Get configured providers.

        Returns
        -------
        dict[str, Any]
            Dictionary mapping provider names to their configurations
        """
        return self.data.get("providers", {})

    @property
    def models(self) -> dict[str, Any]:
        """Get configured models.

        Returns
        -------
        dict[str, Any]
            Dictionary mapping model names to their configurations
        """
        return self.data.get("models", {})

    @property
    def agents(self) -> dict[str, Any]:
        """Get configured agents."""
        return self.data.get("agents", {})

    @property
    def harnesses(self) -> dict[str, Any]:
        """Get configured harnesses.

        Returns
        -------
        dict[str, Any]
            Dictionary mapping harness names to their configurations
        """
        return self.data.get("harnesses", {})

    @property
    def defaults(self) -> dict[str, Any]:
        """Get default settings.

        Returns
        -------
        dict[str, Any]
            Dictionary of default settings
        """
        return self.data.get("defaults", {})

    def get_provider(self, name: str) -> dict[str, Any]:
        """Get provider configuration by name.

        Parameters
        ----------
        name : str
            Provider name to look up

        Returns
        -------
        dict[str, Any]
            Provider configuration dictionary

        Raises
        ------
        ValueError
            If provider not found in config
        """
        if name not in self.providers:
            raise ValueError(f"Provider '{name}' not found in config")
        return self.providers[name]

    def get_model(self, name: str) -> dict[str, Any]:
        """Get model configuration by name, with provider settings merged in.

        Automatically normalizes base_url to ensure it ends with /v1.

        Parameters
        ----------
        name : str
            Model name to look up in configuration

        Returns
        -------
        dict[str, Any]
            Merged model configuration dictionary with keys:
            - model: str
            - base_url: str
            - api_key_env: str
            - cert: str | None
            - api: str | None (opaque API-style hint, interpreted by the harness)

        Raises
        ------
        ValueError
            If model not found in configuration

        Examples
        --------
        >>> config = AutomatonConfig()  # doctest: +SKIP
        >>> model = config.get_model("example-model")  # doctest: +SKIP
        >>> "base_url" in model
        True
        """
        if name not in self.models:
            raise ValueError(f"Model '{name}' not found in config")

        model_config = self.models[name].copy()

        # If model references a provider, merge provider settings
        if "provider" in model_config:
            provider_name = model_config["provider"]
            provider_config = self.get_provider(provider_name)

            # Merge provider settings, with model-specific overrides taking precedence.
            # `api` is carried through for the harness, which owns how a style is
            # wired. Core reads it for one purpose only: the model proxy needs the
            # style to choose the upstream authentication scheme, since Anthropic
            # Messages requires x-api-key rather than Authorization: Bearer.
            merged = {
                "base_url": _normalize_base_url(provider_config.get("base_url")),
                "api_key_env": provider_config.get("api_key_env"),
                "cert": provider_config.get("cert"),
                "api": provider_config.get("api"),
            }
            merged.update(model_config)
            # Normalize base_url if it was overridden in model config
            if "base_url" in model_config:
                merged["base_url"] = _normalize_base_url(model_config["base_url"])
            merged["cost"] = _normalize_cost(model_config.get("cost"))
            return merged

        # Direct model config (no provider reference)
        # Normalize base_url if present
        if "base_url" in model_config:
            model_config["base_url"] = _normalize_base_url(model_config["base_url"])
        model_config["cost"] = _normalize_cost(model_config.get("cost"))
        return model_config

    def get_agent(self, name: str) -> dict[str, Any]:
        """Get agent configuration by name."""
        if name not in self.agents:
            raise ValueError(f"Agent '{name}' not found in config")
        return self.agents[name]

    def get_harness(self, name: str) -> dict[str, Any]:
        """Get harness configuration by name.

        Parameters
        ----------
        name : str
            Harness name to look up

        Returns
        -------
        dict[str, Any]
            Harness configuration dictionary

        Raises
        ------
        ValueError
            If harness not found in config
        """
        if name not in self.harnesses:
            raise ValueError(f"Harness '{name}' not found in config")
        return self.harnesses[name]

    @property
    def timeout_seconds(self) -> int:
        """Get default timeout in seconds.

        Returns
        -------
        int
            Default timeout in seconds
        """
        return int(self.defaults.get("timeout_seconds", 1800))

    @property
    def sandbox_network(self) -> bool:
        """Return whether harness and verification use loopback-only networking."""
        return bool(self.defaults.get("sandbox_network", True))

    @property
    def allowed_provider_executed_tools(self) -> frozenset[str]:
        """Provider-executed tool types the model proxy is permitted to forward.

        Empty by default, which blocks all of them. These tools run on the model
        provider's infrastructure rather than inside the sandbox, so the sandbox
        network namespace cannot confine them and their use leaves no trace in the
        harness transcript. Allowing one is an explicit, auditable decision.

        Configure under ``defaults`` in config.yaml::

            defaults:
              allowed_provider_executed_tools:
                - web_search

        Returns
        -------
        frozenset[str]
            Lowercased tool type names.
        """
        raw = self.defaults.get("allowed_provider_executed_tools") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise ValueError(
                "defaults.allowed_provider_executed_tools must be a list of tool "
                f"type names, got {type(raw).__name__}"
            )
        return frozenset(str(name).strip().lower() for name in raw if str(name).strip())
