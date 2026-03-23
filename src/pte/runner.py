"""Benchmark runner: orchestrates chain execution across parallelism levels."""

import importlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from tqdm import tqdm

from pte.config import BenchmarkConfig, ChainConfig, ToolScenario, WorkspaceRepo

logger = logging.getLogger(__name__)
console = Console()

# Shared resources (cleaned up at end of benchmark)
_browser_executor = None
_mcp_client = None


# ── workspace setup ───────────────────────────────────────────────────


def _setup_workspace(working_dir: str, repo: WorkspaceRepo | None) -> str:
    os.makedirs(working_dir, exist_ok=True)
    if repo is None:
        return working_dir

    repo_name = repo.url.rstrip("/").split("/")[-1].removesuffix(".git")
    repo_dir = Path(working_dir) / repo_name

    if repo_dir.exists() and (repo_dir / ".git").exists():
        console.print(f"[dim]Workspace already cloned at {repo_dir}[/dim]")
        subprocess.run(
            ["git", "checkout", repo.branch],
            cwd=repo_dir,
            capture_output=True,
        )
        return str(repo_dir)

    console.print(
        f"[bold]Cloning {repo.url} (branch={repo.branch}) into {repo_dir}...[/bold]"
    )
    cmd = ["git", "clone", "--branch", repo.branch]
    if repo.shallow:
        cmd += ["--depth", "1"]
    cmd += [repo.url, str(repo_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]git clone failed:[/red]\n{result.stderr}")
        sys.exit(1)

    console.print(f"[green]Cloned into {repo_dir}[/green]")
    return str(repo_dir)


# ── tool creation (registry-driven) ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    pkg: str
    impl: str = "impl"
    executor_kwarg: str = "workspace_root"
    readonly: bool = False
    extra_kwargs: tuple[tuple[str, Any], ...] = ()


def _is_tmux_available() -> bool:
    """Check if tmux is available on the system."""
    try:
        result = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True, timeout=5.0
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


_TOOL_SPECS: dict[str, _ToolSpec] = {
    "terminal_subprocess": _ToolSpec(
        "openhands.tools.terminal",
        executor_kwarg="working_dir",
        extra_kwargs=(("terminal_type", "subprocess"),),
    ),
    "terminal_tmux": _ToolSpec(
        "openhands.tools.terminal",
        executor_kwarg="working_dir",
        extra_kwargs=(("terminal_type", "tmux"),),
    ),
    "file_editor": _ToolSpec("openhands.tools.file_editor"),
    "glob": _ToolSpec(
        "openhands.tools.glob", executor_kwarg="working_dir", readonly=True
    ),
    "grep": _ToolSpec(
        "openhands.tools.grep", executor_kwarg="working_dir", readonly=True
    ),
    "apply_patch": _ToolSpec("openhands.tools.apply_patch", impl="definition"),
    "task_tracker": _ToolSpec(
        "openhands.tools.task_tracker", impl="definition", executor_kwarg="save_dir"
    ),
    "read_file": _ToolSpec("openhands.tools.gemini.read_file", readonly=True),
    "write_file": _ToolSpec("openhands.tools.gemini.write_file"),
    "edit": _ToolSpec("openhands.tools.gemini.edit"),
    "list_directory": _ToolSpec("openhands.tools.gemini.list_directory", readonly=True),
    "planning_file_editor": _ToolSpec("openhands.tools.planning_file_editor"),
}

_BASE_CLASSES = {
    "Action",
    "Observation",
    "Tool",
    "ToolDefinition",
    "Executor",
    "ToolExecutor",
}


def _find_class(module: Any, suffix: str) -> type:
    """Find the most specific class in module whose name ends with suffix."""
    candidates = []
    for name in dir(module):
        if name in _BASE_CLASSES:
            continue
        obj = getattr(module, name)
        if isinstance(obj, type) and name.endswith(suffix):
            candidates.append((name, obj))
    if not candidates:
        raise ValueError(f"No class *{suffix} in {module.__name__}")
    # Longest name wins (PlanningFileEditorExecutor > FileEditorExecutor)
    candidates.sort(key=lambda x: len(x[0]), reverse=True)
    return candidates[0][1]


def _create_tool(tool_name: str, working_dir: str) -> Any:
    if tool_name.startswith("browser_"):
        return _create_browser_tool(tool_name, working_dir)
    if tool_name.startswith("mcp_"):
        raise ValueError(
            f"MCP tool '{tool_name}' must be resolved via _resolve_mcp_tools()"
        )

    spec = _TOOL_SPECS.get(tool_name)
    if spec is None:
        raise ValueError(f"Unknown tool: {tool_name}")

    def_mod = importlib.import_module(f"{spec.pkg}.definition")
    impl_mod = importlib.import_module(f"{spec.pkg}.{spec.impl}")

    action_cls = _find_class(def_mod, "Action")
    tool_cls = _find_class(def_mod, "Tool")
    obs_cls = _find_class(def_mod, "Observation")
    executor_cls = _find_class(impl_mod, "Executor")

    kwargs: dict[str, Any] = {spec.executor_kwarg: working_dir}
    kwargs.update(dict(spec.extra_kwargs))
    if tool_name == "planning_file_editor":
        kwargs["plan_path"] = os.path.join(working_dir, "PLAN.md")

    from openhands.sdk.tool.tool import ToolAnnotations

    return tool_cls(
        description=tool_name,
        action_type=action_cls,
        observation_type=obs_cls,
        annotations=ToolAnnotations(readOnlyHint=spec.readonly),
        executor=executor_cls(**kwargs),
    )


def _filter_chains_by_availability(
    chains: list[ChainConfig],
) -> list[ChainConfig]:
    """Skip chains requiring unavailable backends (e.g. terminal_tmux without tmux)."""
    tmux_available: bool | None = None  # lazy check

    filtered = []
    for chain in chains:
        needs_tmux = any(s.tool_name == "terminal_tmux" for s in chain.calls)
        if needs_tmux:
            if tmux_available is None:
                tmux_available = _is_tmux_available()
            if not tmux_available:
                console.print(
                    f"  [yellow]Skipping '{chain.name}' (tmux not available)[/yellow]"
                )
                continue
        filtered.append(chain)
    return filtered


def _resolve_tools(chains: list[ChainConfig], working_dir: str) -> dict[str, Any]:
    needed: set[str] = {s.tool_name for chain in chains for s in chain.calls}
    tool_map: dict[str, Any] = {}

    if any(n.startswith(("browser_", "mcp_")) for n in needed):
        for name in ("bubus", "BrowserSession", "browser_use", "httpcore", "httpx"):
            logging.getLogger(name).setLevel(logging.ERROR)

    mcp_names = {n for n in needed if n.startswith("mcp_")}
    if mcp_names:
        for tool in _resolve_mcp_tools():
            prefixed = f"mcp_{tool.name}"
            if prefixed in mcp_names:
                tool_map[prefixed] = tool

    for name in needed - mcp_names:
        tool_map[name] = _create_tool(name, working_dir)

    return tool_map


def _create_browser_tool(tool_name: str, working_dir: str) -> Any:
    global _browser_executor
    from openhands.tools.browser_use.impl import BrowserToolExecutor
    from openhands.tools.browser_use import definition as bdef
    from openhands.sdk.tool.tool import ToolAnnotations

    browser_tools = {
        "browser_navigate": (bdef.BrowserNavigateAction, bdef.BrowserNavigateTool),
        "browser_click": (bdef.BrowserClickAction, bdef.BrowserClickTool),
        "browser_type": (bdef.BrowserTypeAction, bdef.BrowserTypeTool),
        "browser_get_state": (bdef.BrowserGetStateAction, bdef.BrowserGetStateTool),
        "browser_get_content": (
            bdef.BrowserGetContentAction,
            bdef.BrowserGetContentTool,
        ),
        "browser_scroll": (bdef.BrowserScrollAction, bdef.BrowserScrollTool),
        "browser_go_back": (bdef.BrowserGoBackAction, bdef.BrowserGoBackTool),
        "browser_list_tabs": (bdef.BrowserListTabsAction, bdef.BrowserListTabsTool),
        "browser_switch_tab": (bdef.BrowserSwitchTabAction, bdef.BrowserSwitchTabTool),
        "browser_close_tab": (bdef.BrowserCloseTabAction, bdef.BrowserCloseTabTool),
    }

    if tool_name not in browser_tools:
        raise ValueError(f"Unknown browser tool: {tool_name}")

    action_cls, tool_cls = browser_tools[tool_name]

    if _browser_executor is None:
        _browser_executor = BrowserToolExecutor(
            headless=True,
            full_output_save_dir=os.path.join(working_dir, ".browser_output"),
        )

    is_readonly = tool_name in (
        "browser_get_state",
        "browser_get_content",
        "browser_list_tabs",
    )
    return tool_cls(
        description=tool_name,
        action_type=action_cls,
        observation_type=bdef.BrowserObservation,
        annotations=ToolAnnotations(readOnlyHint=is_readonly),
        executor=_browser_executor,
    )


def _resolve_mcp_tools() -> list:
    global _mcp_client
    from openhands.sdk.mcp import create_mcp_tools

    _mcp_client = create_mcp_tools(
        {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}},
        timeout=60.0,
    )
    return list(_mcp_client.tools)


# ── action event building ────────────────────────────────────────────


def _build_action_events(
    scenarios: list[ToolScenario],
    tools_map: dict[str, Any],
    working_dir: str,
) -> list:
    from pte.executor import make_action_event

    events = []
    for scenario in scenarios:
        tool = tools_map.get(scenario.tool_name)
        if tool is None:
            logger.warning("Tool '%s' not in tools_map — skipping", scenario.tool_name)
            continue

        action_kwargs = dict(scenario.action)
        for key in ("path", "file_path", "dir_path"):
            if key in action_kwargs and not os.path.isabs(action_kwargs[key]):
                action_kwargs[key] = os.path.join(working_dir, action_kwargs[key])

        for _ in range(scenario.repeat):
            if scenario.tool_name.startswith("mcp_"):
                action = tool.action_from_arguments(action_kwargs)
            else:
                action = tool.action_type.model_validate(action_kwargs)
            events.append(
                make_action_event(
                    tool_name=scenario.tool_name,
                    action=action,
                    action_kwargs=action_kwargs,
                )
            )
    return events


# ── result types ──────────────────────────────────────────────────────


class ChainRunResult:
    """Aggregated result for one chain at one parallelism level."""

    def __init__(self, name: str):
        self.name = name
        self.wall_seconds: list[float] = []
        self.speedups: list[float] = []
        self.peak_memory_bytes: list[int] = []
        self.mismatches = 0
        self.tool_errors = 0
        self.run_answers: list[list[str]] = []
        self.tool_names: list[str] = []

    @property
    def mean_wall(self) -> float:
        return (
            sum(self.wall_seconds) / len(self.wall_seconds) if self.wall_seconds else 0
        )

    @property
    def mean_speedup(self) -> float:
        return sum(self.speedups) / len(self.speedups) if self.speedups else 0

    @property
    def mean_peak_memory_mb(self) -> float:
        if not self.peak_memory_bytes:
            return 0.0
        return sum(self.peak_memory_bytes) / len(self.peak_memory_bytes) / (1024 * 1024)

    @property
    def unique_runs(self) -> int:
        """Distinct answer sets across runs. 1 = deterministic."""
        if not self.run_answers:
            return 0
        return len({tuple(a) for a in self.run_answers})


def _count_mismatches(baseline: list[str], current: list[str]) -> int:
    return sum(a != b for a, b in zip_longest(baseline, current, fillvalue=""))


# ── benchmark execution ──────────────────────────────────────────────


def run_benchmark(
    config: BenchmarkConfig,
) -> tuple[dict[int, dict[str, ChainRunResult]], dict[str, list[str]]]:
    global _browser_executor, _mcp_client
    from pte.executor import BenchmarkExecutor

    working_dir = str(Path(config.working_dir).resolve())
    actual_dir = _setup_workspace(working_dir, config.workspace_repo)
    console.print(f"[dim]Workspace: {actual_dir}[/dim]")

    config.chains = _filter_chains_by_availability(config.chains)
    tools_map = _resolve_tools(config.chains, actual_dir)

    if not config.chains:
        console.print("[yellow]No chains configured.[/yellow]")
        return {}, {}

    levels = sorted(set(config.parallelism_levels))
    if 1 not in levels:
        levels = [1] + levels

    ground_truth: dict[str, list[str]] = {}
    results: dict[int, dict[str, ChainRunResult]] = {}

    chain_table = Table(
        title=f"Benchmarking {len(config.chains)} chains",
        min_width=40,
    )
    chain_table.add_column("Chain", style="magenta", no_wrap=True)
    chain_table.add_column("Calls", justify="right", style="cyan")
    for c in config.chains:
        chain_table.add_row(c.name, str(len(c.calls)))
    console.print()
    console.print(chain_table)
    console.print(
        f"[dim]Workers: {levels} | "
        f"{config.warmup_runs} warmup + {config.benchmark_runs} runs each[/dim]"
    )

    bench_start = time.perf_counter()

    total_steps = len(levels) * len(config.chains) * (
        config.warmup_runs + config.benchmark_runs
    )

    pbar = tqdm(
        total=total_steps,
        desc="Benchmarking",
        unit="run",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}",
    )

    for level in levels:
        level_results: dict[str, ChainRunResult] = {}

        for chain in config.chains:
            result = ChainRunResult(chain.name)

            for _ in range(config.warmup_runs):
                pbar.set_postfix_str(f"w={level} {chain.name} (warmup)")
                events = _build_action_events(chain.calls, tools_map, actual_dir)
                BenchmarkExecutor(max_workers=level).run_chain(
                    chain.name,
                    events,
                    tools_map,
                )
                pbar.update(1)

            for i in range(config.benchmark_runs):
                pbar.set_postfix_str(
                    f"w={level} {chain.name} [{i + 1}/{config.benchmark_runs}]"
                )
                events = _build_action_events(chain.calls, tools_map, actual_dir)
                chain_result = BenchmarkExecutor(max_workers=level).run_chain(
                    chain.name,
                    events,
                    tools_map,
                )
                rec = chain_result.record

                result.wall_seconds.append(rec.wall_clock_seconds)
                result.speedups.append(rec.speedup)
                result.tool_errors += rec.tool_errors
                result.peak_memory_bytes.append(chain_result.peak_memory_bytes)
                result.run_answers.append(chain_result.answers)

                if not result.tool_names:
                    result.tool_names = chain_result.tool_names

                if level == 1 and i == 0:
                    ground_truth[chain.name] = chain_result.answers

                if chain.name in ground_truth:
                    result.mismatches += _count_mismatches(
                        ground_truth[chain.name],
                        chain_result.answers,
                    )

                pbar.update(1)

            level_results[chain.name] = result

        results[level] = level_results

    pbar.close()

    # Cleanup shared resources
    if _browser_executor is not None:
        try:
            _browser_executor.close()
        except Exception:
            pass
        _browser_executor = None
    if _mcp_client is not None:
        try:
            _mcp_client.sync_close()
        except Exception:
            pass
        _mcp_client = None

    console.print(
        f"\n[dim]Total benchmark time: {time.perf_counter() - bench_start:.1f}s[/dim]"
    )
    return results, ground_truth


# ── output ────────────────────────────────────────────────────────────


def _print_summary(
    results: dict[int, dict[str, ChainRunResult]],
    levels: list[int],
    chain_names: list[str],
) -> None:
    console.print()

    total_errors = sum(
        r.tool_errors + r.mismatches
        for level in levels
        for r in results[level].values()
    )

    if 1 in results:
        time_lines = []
        for level in levels:
            if level == 1:
                continue
            total_w1 = sum(results[1][n].mean_wall for n in chain_names)
            total_wn = sum(results[level][n].mean_wall for n in chain_names)
            saved = total_w1 - total_wn
            sign = "+" if saved < 0 else "-"
            time_lines.append(f"w={level}: {sign}{abs(saved):.2f}s")
        if time_lines:
            console.print(f"[bold]Time vs w=1:[/bold] {' | '.join(time_lines)}")

    console.print(f"[bold]Total errors:[/bold] {total_errors}")


def print_results(results: dict[int, dict[str, ChainRunResult]]) -> None:
    if not results:
        return

    table = Table(title="Benchmark Results", min_width=90)
    table.add_column("Chain", style="magenta", no_wrap=True)
    table.add_column("W", justify="right", style="cyan")
    table.add_column("Wall (s)", justify="right", style="bold", min_width=8)
    table.add_column("Speedup", justify="right", style="green", min_width=7)
    table.add_column("Mem (MB)", justify="right", min_width=8)
    table.add_column("Errors", justify="right", style="red", min_width=6)
    table.add_column("Unique", justify="right", style="yellow", min_width=7)

    num_runs = None
    chain_names = list(next(iter(results.values())).keys())
    levels = sorted(results)

    for name in chain_names:
        for level in levels:
            r = results[level][name]
            if num_runs is None:
                num_runs = len(r.run_answers)
            errors = r.tool_errors + r.mismatches
            error_str = str(errors) if errors == 0 else f"[red]{errors}[/red]"
            unique = r.unique_runs
            unique_str = (
                f"{unique}/{num_runs}"
                if unique <= 1
                else f"[yellow]{unique}/{num_runs}[/yellow]"
            )
            table.add_row(
                name,
                str(level),
                f"{r.mean_wall:.3f}",
                f"{r.mean_speedup:.2f}x",
                f"{r.mean_peak_memory_mb:.1f}",
                error_str,
                unique_str,
            )
        if name != chain_names[-1]:
            table.add_section()

    console.print()
    console.print(table)
    console.print(
        "\n[dim]Speedup = sum(tool_durations) / wall_clock. "
        "1.0x = sequential, higher = more parallel.\n"
        "Errors = tool exceptions + answer mismatches vs worker=1 ground truth.\n"
        f"Unique = distinct answer sets out of {num_runs or '?'} runs. "
        "1/N = deterministic.[/dim]"
    )

    _print_summary(results, levels, chain_names)


def save_results(
    results: dict[int, dict[str, ChainRunResult]],
    ground_truth: dict[str, list[str]],
    save_dir: str,
) -> None:
    """Save all tool answers to disk for debugging mismatches."""
    import difflib

    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    for level in sorted(results):
        for chain_name, r in results[level].items():
            chain_dir = save_path / chain_name
            chain_dir.mkdir(exist_ok=True)
            baseline = ground_truth.get(chain_name, [])

            gt_file = chain_dir / "ground_truth.txt"
            if not gt_file.exists() and baseline:
                with open(gt_file, "w") as f:
                    for i, (tool, answer) in enumerate(zip(r.tool_names, baseline)):
                        f.write(f"=== Call {i}: {tool} ===\n{answer}\n\n")

            for run_idx, answers in enumerate(r.run_answers):
                run_file = chain_dir / f"workers_{level}_run_{run_idx + 1}.txt"
                with open(run_file, "w") as f:
                    for i, answer in enumerate(answers):
                        tool = r.tool_names[i] if i < len(r.tool_names) else "?"
                        f.write(f"=== Call {i}: {tool} ===\n{answer}\n\n")

                if baseline and answers != baseline:
                    diff_file = (
                        chain_dir / f"diff_workers_{level}_run_{run_idx + 1}.txt"
                    )
                    with open(diff_file, "w") as f:
                        has_diffs = False
                        for i in range(max(len(baseline), len(answers))):
                            bl = baseline[i] if i < len(baseline) else "<MISSING>"
                            cur = answers[i] if i < len(answers) else "<MISSING>"
                            if bl != cur:
                                has_diffs = True
                                tool = r.tool_names[i] if i < len(r.tool_names) else "?"
                                f.write(f"=== MISMATCH Call {i}: {tool} ===\n")
                                f.writelines(
                                    difflib.unified_diff(
                                        bl.splitlines(keepends=True),
                                        cur.splitlines(keepends=True),
                                        fromfile=f"ground_truth[{i}]",
                                        tofile=f"workers_{level}_run_{run_idx + 1}[{i}]",
                                    )
                                )
                                f.write("\n\n")
                        if not has_diffs:
                            diff_file.unlink()

    console.print(f"\n[green]Results saved to {save_path}[/green]")
