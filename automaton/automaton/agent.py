"""Agent configuration for Automaton."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AutomatonConfig
from .harness import Harness


@dataclass(frozen=True)
class AgentModel:
    """Resolved model exposed to a harness through an agent."""

    name: str
    config: dict[str, Any]


class Agent:
    """Represents a configured benchmark agent."""

    def __init__(self, agent_id: str, config: AutomatonConfig):
        self.agent_id = agent_id
        self.config = config
        self.definition = config.get_agent(agent_id)
        self.harness_id = str(self.definition.get("harness", "")).strip()
        self.model_names = [str(name) for name in self.definition.get("models", [])]
        self.harness_config = dict(self.definition.get("harness_config", {}) or {})

        self.models = [
            AgentModel(name=model_name, config=config.get_model(model_name))
            for model_name in self.model_names
        ]

    def validate(self) -> list[str]:
        errors: list[str] = []

        if not self.harness_id:
            errors.append(f"Agent '{self.agent_id}' is missing a harness")
            return errors

        if not self.model_names:
            errors.append(f"Agent '{self.agent_id}' must expose at least one model")

        for model_name in self.model_names:
            try:
                self.config.get_model(model_name)
            except Exception as exc:
                errors.append(str(exc))

        return errors

    @staticmethod
    def list_agents(config: AutomatonConfig) -> list[str]:
        return sorted(config.agents.keys())
