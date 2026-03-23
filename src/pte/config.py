"""YAML configuration loader for benchmark scenarios."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ToolScenario:
    """A single tool invocation scenario from the YAML config."""

    tool_name: str
    action: dict[str, Any]
    repeat: int = 1


@dataclass
class ChainConfig:
    """A flat sequence of tool calls to execute through _ActionBatch.

    All calls are sent to a single _ActionBatch.prepare() call.
    The SDK decides how to execute them (all in parallel via ParallelToolExecutor).
    """

    name: str
    calls: list[ToolScenario]


@dataclass
class WorkspaceRepo:
    """A git repository to clone into the working directory."""

    url: str
    branch: str = "main"
    shallow: bool = True


@dataclass
class BenchmarkConfig:
    """Top-level benchmark configuration."""

    sdk_branch: str = "main"
    sdk_repo: str = "https://github.com/OpenHands/software-agent-sdk.git"
    parallelism_levels: list[int] = field(default_factory=lambda: [1, 2, 4, 8])
    warmup_runs: int = 1
    benchmark_runs: int = 3
    working_dir: str = "tmp"
    workspace_repo: WorkspaceRepo | None = None
    chains: list[ChainConfig] = field(default_factory=list)


def _parse_scenario(raw: dict) -> ToolScenario:
    return ToolScenario(
        tool_name=raw["tool_name"],
        action=raw.get("action", {}),
        repeat=raw.get("repeat", 1),
    )


def load_config(path: str | Path) -> BenchmarkConfig:
    """Load benchmark configuration from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    chains = []
    for c in raw.get("chains", []):
        calls = [_parse_scenario(s) for s in c.get("calls", [])]
        chains.append(ChainConfig(name=c["name"], calls=calls))

    workspace_repo = None
    raw_repo = raw.get("workspace_repo")
    if raw_repo:
        workspace_repo = WorkspaceRepo(
            url=raw_repo["url"],
            branch=raw_repo.get("branch", "main"),
            shallow=raw_repo.get("shallow", True),
        )

    return BenchmarkConfig(
        sdk_branch=raw.get("sdk_branch", "main"),
        sdk_repo=raw.get(
            "sdk_repo",
            "https://github.com/OpenHands/software-agent-sdk.git",
        ),
        parallelism_levels=raw.get("parallelism_levels", [1, 2, 4, 8]),
        warmup_runs=raw.get("warmup_runs", 1),
        benchmark_runs=raw.get("benchmark_runs", 3),
        working_dir=raw.get("working_dir", "tmp"),
        workspace_repo=workspace_repo,
        chains=chains,
    )
