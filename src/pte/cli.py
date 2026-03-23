"""CLI entry point for the parallel tool execution benchmark."""

import argparse
import logging
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

console = Console()


def _setup_sdk(sdk_repo: str, branch: str, base_dir: str, skip: bool = False) -> None:
    """Clone/checkout the SDK repo and add to sys.path."""
    sdk_dir = Path(base_dir) / "software-agent-sdk"

    if not skip:
        if sdk_dir.exists() and (sdk_dir / ".git").exists():
            subprocess.run(
                ["git", "fetch", "origin", branch],
                cwd=sdk_dir,
                capture_output=True,
            )
            result = subprocess.run(
                ["git", "checkout", branch],
                cwd=sdk_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                subprocess.run(
                    ["git", "checkout", "-B", branch, f"origin/{branch}"],
                    cwd=sdk_dir,
                    capture_output=True,
                )
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=sdk_dir,
                capture_output=True,
            )
        else:
            console.print(f"Cloning SDK from {sdk_repo} (branch={branch})...")
            cmd = ["git", "clone", "--branch", branch, sdk_repo, str(sdk_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                console.print(f"[red]git clone failed:[/red]\n{result.stderr}")
                sys.exit(1)

    # Add SDK packages to sys.path
    for sub in ("openhands-sdk", "openhands-tools"):
        p = sdk_dir / sub
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))

    try:
        import openhands.sdk  # noqa: F401
    except ImportError as e:
        console.print(f"[red]SDK import failed: {e}[/red]")
        console.print(f"[dim]SDK dir: {sdk_dir}[/dim]")
        sys.exit(1)

    if not skip:
        rev = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=sdk_dir,
            capture_output=True,
            text=True,
        )
        console.print(f"[dim]SDK: {branch} ({rev.stdout.strip()})[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark parallel tool execution in OpenHands software-agent-sdk",
    )
    parser.add_argument("config", help="Path to YAML benchmark config")
    parser.add_argument(
        "--branch", default=None, help="SDK branch (default: from YAML)"
    )
    parser.add_argument(
        "--chain",
        nargs="*",
        metavar="NAME",
        default=None,
        help="Run named chain(s) or prefix group (tool/cross/impl/...). No args = list.",
    )
    parser.add_argument(
        "--save",
        nargs="?",
        const="results",
        default=None,
        help="Save answers for debugging (default dir: results/)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip SDK clone/checkout (use existing)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from pte.config import load_config

    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    if args.branch:
        config.sdk_branch = args.branch

    base_dir = Path(config.working_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    _setup_sdk(config.sdk_repo, config.sdk_branch, str(base_dir), skip=args.skip_install)

    from pte.runner import print_results, run_benchmark, save_results

    # Filter chains
    if args.chain is not None:
        if len(args.chain) == 0:
            groups: dict[str, list] = defaultdict(list)
            for c in config.chains:
                groups[c.name.split("-")[0]].append(c)
            for prefix in sorted(groups):
                console.print(f"[bold]{prefix}[/bold] chains (--chain {prefix}):")
                for c in groups[prefix]:
                    console.print(f"  {c.name} ({len(c.calls)} calls)")
            sys.exit(0)

        available = {c.name for c in config.chains}
        selectors: set[str] = set()
        for name in args.chain:
            if name in available:
                selectors.add(name)
            else:
                matches = {n for n in available if n.startswith(f"{name}-")}
                if matches:
                    selectors.update(matches)
                else:
                    console.print(
                        f"[red]'{name}' is not a chain or prefix. "
                        f"Available: {', '.join(sorted(available))}[/red]"
                    )
                    sys.exit(1)

        config.chains = [c for c in config.chains if c.name in selectors]

    results, ground_truth = run_benchmark(config)
    print_results(results)

    if args.save is not None:
        # Build unique run directory: <save>/<timestamp>_<command_slug>/
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        cmd_slug = "_".join(sys.argv[1:]).replace("/", "_").replace(".", "_")
        # Truncate to keep path reasonable
        cmd_slug = cmd_slug[:80] if len(cmd_slug) > 80 else cmd_slug
        run_id = f"{timestamp}_{cmd_slug}"
        save_dir = config_path.parent / args.save / run_id
        save_results(results, ground_truth, str(save_dir))

        # Save console summary
        summary_file = save_dir / "summary.txt"
        file_console = Console(file=open(summary_file, "w"), width=120)
        print_results(results, output=file_console)
        file_console.file.close()
        console.print(f"[dim]Summary saved to {summary_file}[/dim]")


if __name__ == "__main__":
    main()
