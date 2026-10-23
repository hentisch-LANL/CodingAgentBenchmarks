"""Configuration loading and management for ABC Bench.

ABC Bench builds on :class:`automaton.AutomatonConfig`, which owns the
provider/model/agent/harness configuration and sandbox execution defaults.
This module extends it with benchmark-orchestration concerns: locating the
package root (harnesses/ and container base/harness templates) and the results
directory, and building the task-group resolver.

Task groups themselves are *not* part of the package root; they are discovered
via :class:`abc_bench.groups.GroupResolver` (CLI ``--task-dir`` > ``TASK_PATH``
> the installed ``~/.abc-bench/tasks`` root populated by ``abc-bench install``).
"""

from __future__ import annotations

import os
from pathlib import Path

from automaton import AutomatonConfig
from automaton.config import initialize_user_config as _automaton_initialize_user_config  # noqa: F401

from .groups import GroupResolver, installed_tasks_root


def get_package_root() -> Path:
    """Return the root holding ``harnesses/`` and ``containers/``.

    These are ABC Bench-owned runtime assets (not benchmarks). abc_bench is not
    a published package, so these always live in a checkout. Resolution order:

    1. ``ABC_BENCH_PACKAGE_ROOT`` environment variable
    2. Parent directory of the ``abc_bench`` package (editable/checkout install)
    """
    env_root = os.environ.get("ABC_BENCH_PACKAGE_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if (root / "harnesses").is_dir() and (root / "containers").is_dir():
            return root

    repo_root = _repo_root_package_root()
    if repo_root is not None:
        return repo_root

    return Path.cwd()


def _repo_root_package_root() -> Path | None:
    """Return the repo root when running from a checkout/editable install."""
    try:
        package_dir = Path(__file__).resolve().parent
        repo_root = package_dir.parent
        if (repo_root / "harnesses").is_dir() and (repo_root / "containers").is_dir():
            return repo_root
    except Exception:
        return None
    return None


DEFAULT_CONFIG_DIR = Path("~/.abc-bench")
DEFAULT_CONFIG_PATH = Path("~/.abc-bench/config.yaml")
DEFAULT_CONFIG_TEMPLATE = """# ABC Bench Configuration
#
# package_root: Path to abc-bench runtime assets (harnesses/ and containers/).
#               Leave as null to auto-detect (recommended for most users).
#
# Task groups (benchmarks) are NOT configured here. They are discovered at
# runtime with the following priority:
#   1. the --task-dir CLI flag (a directory containing task groups)
#   2. the TASK_PATH environment variable (colon-separated dirs, like PATH)
#   3. the installed root ~/.abc-bench/tasks (populate with `abc-bench install`)
package_root: null

providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null
    # api: the upstream API style for this provider. Carried through to the
    #      harness verbatim; each harness defines which values it supports and
    #      how to wire them (opencode: "chat_completions" (default) | "responses").
    #      Can be overridden per-model. Leave unset for the harness default.
    # api: chat_completions

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null
    # Claude Code speaks only the Anthropic Messages API (/v1/messages).
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

  claude-code-default:
    harness: claude-code
    models:
      - anthropic-claude
    # Optional knobs: effort (low|medium|high|xhigh|max), max_turns,
    # max_budget_usd, tools (defaults to the non-web built-in coding tools).
    harness_config: {}

defaults:
  results_dir: results
  timeout_seconds: 1800
  sandbox_network: true

  # Provider-executed ("native") tool types the model proxy may forward, e.g.
  # web_search. These run on the model provider's infrastructure rather than
  # inside the sandbox, so sandbox_network cannot confine them and their use
  # leaves no trace in the harness transcript. Empty (the default) blocks all of
  # them; a blocked run is recorded as HARNESS_ERROR, never graded as a FAIL.
  allowed_provider_executed_tools: []
"""


def initialize_user_config() -> Path:
    """Create the default ABC Bench user config directory and template if missing.

    Returns
    -------
    Path
        Path to the created config file
    """
    config_dir = DEFAULT_CONFIG_DIR.expanduser()
    config_path = DEFAULT_CONFIG_PATH.expanduser()

    config_dir.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE)

    return config_path


class Config(AutomatonConfig):
    """ABC Bench configuration.

    Extends :class:`automaton.AutomatonConfig` (providers/models/agents/harnesses
    and sandbox defaults) with package-root and results-directory resolution and
    task-group resolver construction.

    Examples
    --------
    >>> config = Config()  # doctest: +SKIP
    >>> model = config.get_model("example-model")  # doctest: +SKIP
    >>> model["provider"]
    'example'
    """

    def __init__(self, config_path: Path | None = None):
        """Load configuration from file or use the ABC Bench default location."""
        if config_path is None:
            config_path = initialize_user_config()
        super().__init__(config_path)

    @property
    def results_dir(self) -> Path:
        """Get results directory path."""
        return Path(self.defaults.get("results_dir", "results"))

    @property
    def package_root(self) -> Path:
        """Root containing ``harnesses/`` and ``containers/``."""
        value = self.data.get("package_root")
        if value:
            return Path(value).expanduser().resolve()
        return get_package_root()

    @property
    def installed_root(self) -> Path:
        """Installed task-group root (``~/.abc-bench/tasks``)."""
        return installed_tasks_root()

    def group_resolver(self, task_dir: Path | None = None, warn: bool = True) -> GroupResolver:
        """Build a task-group resolver honoring CLI > TASK_PATH > installed."""
        return GroupResolver(
            task_dir=task_dir,
            installed_root=self.installed_root,
            warn=warn,
        )
