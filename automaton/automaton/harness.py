"""Harness management for Automaton.

A harness defines how an autonomous agent is invoked: a run script, prompts,
and harness-specific metadata. Container recipes are not owned by the harness
directory; they are supplied to :class:`~automaton.container.ContainerManager`
through a :class:`~automaton.container.ContainerLayout`.

Automaton is content-free: it does not assume any particular on-disk layout
for harnesses. A :class:`Harness` is constructed from an explicit harness
directory supplied by the caller.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class Harness:
    """Represents an Automaton harness loaded from an explicit directory.

    Parameters
    ----------
    harness_id : str
        Harness identifier.
    harness_dir : Path
        Path to the harness directory containing ``harness.yaml``, the run
        script, and prompt assets.
    """

    def __init__(self, harness_id: str, harness_dir: Path):
        self.harness_id = harness_id
        self.harness_dir = Path(harness_dir).resolve()

        if not self.harness_dir.exists():
            raise FileNotFoundError(f"Harness directory not found: {self.harness_dir}")

        config_file = self.harness_dir / "harness.yaml"
        if not config_file.exists():
            raise FileNotFoundError(f"Harness config not found: {config_file}")

        with open(config_file) as f:
            self.config = yaml.safe_load(f) or {}

    @property
    def container_template(self) -> str:
        """Container template name for this harness."""
        return str(self.config.get("container_template", self.harness_id))

    @property
    def run_script(self) -> Path:
        """Path to run_benchmark.sh script."""
        return self.harness_dir / "run_benchmark.sh"

    @property
    def system_prompt_file(self) -> Path:
        """Path to system_prompt.md file."""
        return self.harness_dir / "system_prompt.md"

    def validate(self) -> list[str]:
        """Validate that all required harness components exist."""
        errors = []
        if not self.run_script.exists():
            errors.append(f"Missing run_benchmark.sh: {self.run_script}")
        if not self.system_prompt_file.exists():
            errors.append(f"Missing system_prompt.md: {self.system_prompt_file}")
        return errors
