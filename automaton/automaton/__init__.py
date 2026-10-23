"""Automaton: a general-purpose sandboxed autonomous-agent execution engine.

Automaton runs an autonomous agent (a *harness* driving one or more models)
inside a hardened Charliecloud sandbox, brokering model traffic through a
loopback-only model proxy, and leaves the agent's deliverable in a workspace
directory. It also exposes a generic path for running arbitrary (non-agentic)
commands inside the same container image.

Automaton is content-free: it ships no concrete harnesses, container
templates, or tasks. Callers (such as a benchmark orchestrator) supply those
assets as inputs.

Public surface
--------------
- :class:`~automaton.config.AutomatonConfig` -- providers/models/agents/harnesses config
- :class:`~automaton.agent.Agent` / :class:`~automaton.agent.AgentModel`
- :class:`~automaton.harness.Harness`
- :class:`~automaton.container.ContainerManager` / :class:`~automaton.container.ContainerLayout`
- :class:`~automaton.sandbox.Sandbox` / :class:`~automaton.sandbox.ExecutionResult`
- :class:`~automaton.sandbox.BindMount`
- :class:`~automaton.proxy.ModelProxy`
- :class:`~automaton.pool.AgentPool` / :class:`~automaton.pool.AgentJob` /
  :class:`~automaton.pool.JobResult` (MPI implementation in
  :mod:`automaton.pool.mpi_pool`, requires the ``automaton[mpi]`` extra)
- Exceptions: :class:`~automaton.sandbox.CommandRunFailed`,
  :class:`~automaton.proxy.EndpointRetryExhausted`
"""

from __future__ import annotations

from .agent import Agent, AgentModel
from .config import AutomatonConfig
from .container import ContainerLayout, ContainerManager, ContainerRef, ContainerInfo
from .harness import Harness
from .pool import AgentJob, AgentPool, JobResult, JobStatus, SerialAgentPool
from .proxy import EndpointRetryExhausted, ModelProxy
from .sandbox import BindMount, CommandRunFailed, ExecutionResult, Sandbox

__all__ = [
    "Agent",
    "AgentJob",
    "AgentModel",
    "AgentPool",
    "AutomatonConfig",
    "BindMount",
    "CommandRunFailed",
    "ContainerInfo",
    "ContainerLayout",
    "ContainerManager",
    "ContainerRef",
    "EndpointRetryExhausted",
    "ExecutionResult",
    "Harness",
    "JobResult",
    "JobStatus",
    "ModelProxy",
    "Sandbox",
    "SerialAgentPool",
]

__version__ = "0.1.0"
