# PTE — Parallel Tool Execution Benchmark

Benchmarks concurrency safety of [OpenHands software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) parallel tool execution. Uses the SDK's `_ActionBatch` and `ParallelToolExecutor` against a real codebase (CPython). No LLMs needed.

Context: [#2526](https://github.com/OpenHands/software-agent-sdk/issues/2526) (safety analysis), [#2527](https://github.com/OpenHands/software-agent-sdk/issues/2527) (implementation plan).

## Quick start

```bash
uv sync
uv run pte benchmark.yaml                          # all chains
uv run pte benchmark.yaml --skip-install            # reuse existing SDK clone
uv run pte benchmark.yaml --chain tool              # single-tool only
uv run pte benchmark.yaml --chain impl-p0           # P0 verification only
uv run pte benchmark.yaml --save                    # save answers to results/<run_id>/
uv run pte benchmark.yaml --chain                   # list chains
```

## CLI flags

| Flag | Description |
|---|---|
| `--branch` | SDK branch to checkout (default: `main`) |
| `--chain [NAME ...]` | Run named chains or prefix groups (`tool`, `cross`, `impl`, `impl-p0`, ...) |
| `--save [DIR]` | Save answers + diffs + summary (default: `results/`) |
| `--compare B A` | Compare two `summary.txt` files and print deltas |
| `--skip-install` | Skip SDK clone/checkout |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Terminal backends

Terminal chains run with **both** subprocess and tmux backends to compare behavior:

- `terminal_subprocess` — PTY-based subprocess (always available)
- `terminal_tmux` — tmux session (auto-skipped if tmux is not installed)

Each terminal chain is duplicated with a `-subprocess` and `-tmux` suffix (e.g. `tool-terminal-subprocess`, `tool-terminal-tmux`).

## Chains

Each chain sends all its calls to a single `_ActionBatch.prepare()`. Worker=1 is ground truth.

| Group | Prefix | What it tests |
|---|---|---|
| Single-tool | `tool-*` | One tool type under concurrency stress |
| Cross-tool | `cross-*` | Interactions between different tool types |
| Write-conflict | `write-conflict-*` | Concurrent writes to same/different files |
| Implementation | `impl-*` | Verifies [#2527 Section 10](https://github.com/OpenHands/software-agent-sdk/issues/2527#issuecomment-4104045362) priorities |

## Output

```
Benchmarking: 100%|████████████| 120/120 [02:15<00:00] w=8 tool-glob [3/3]

                          Benchmark Results
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ Chain                      ┃  W ┃ Wall (s) ┃ Speedup ┃ Mem(MB) ┃ Errors ┃ Unique ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ tool-terminal-subprocess   │  1 │    0.104 │   1.00x │     0.1 │      0 │    1/3 │
│ tool-terminal-subprocess   │  2 │    0.052 │   2.00x │     0.2 │      0 │    1/3 │
└────────────────────────────┴────┴──────────┴─────────┴─────────┴────────┴────────┘

Time vs w=1: w=2: -1.23s | w=4: -2.50s
Total errors: 24
```

| Column | Meaning |
|---|---|
| **W** | Worker threads |
| **Wall** | Mean wall-clock time |
| **Speedup** | `wall_w1 / wall_wN` — always `1.00x` at w=1 |
| **Errors** | Tool exceptions + ground truth mismatches |
| **Unique** | Distinct answer sets / total runs. `1/N` = deterministic |

## Saving results

With `--save`, results are written to a unique timestamped directory:

```
results/
  20260323_141500_benchmark_yaml_--chain_tool/
    summary.txt              # console table output
    tool-terminal-subprocess/
      ground_truth.txt
      workers_1_run_1.txt
      workers_2_run_1.txt
      diff_workers_2_run_1.txt
    ...
```

## Comparing runs

Use `--compare` to diff two `summary.txt` files side by side:

```bash
uv run pte --compare results/run_baseline/summary.txt results/run_feature/summary.txt
```

This prints a delta table showing per-chain, per-worker-level changes:

```
                                  Comparison (B → A)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┓
┃ Chain                          ┃ W ┃  Δ Speedup ┃ Δ Mem (MB) ┃  Δ Errors ┃ Unique B ┃ Unique A ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━┩
│ lock-same-resource-subprocess  │ 4 │      +0.93 │       +0.0 │       -12 │      3/3 │      1/3 │
└────────────────────────────────┴───┴────────────┴────────────┴───────────┴──────────┴──────────┘

Δ = compare − baseline. Speedup: + = faster. Mem/Errors: − = better.
Total errors: 195 → 2 (-193)
```

| Column | Meaning |
|---|---|
| **Δ Speedup** | Change in speedup factor (green = faster) |
| **Δ Mem (MB)** | Change in memory usage (green = less) |
| **Δ Errors** | Change in error count (green = fewer) |
| **Unique B/A** | Determinism before and after — `1/N` = deterministic |

Only chains present in both summaries are compared.

## How it works

1. Clones SDK into `tmp/software-agent-sdk/`, adds to `sys.path` (no pip install)
2. Clones CPython (shallow) into `tmp/cpython/`
3. Creates real SDK `ToolDefinition` objects with executors
4. Runs each chain at parallelism levels `[1, 2, 4, 8]`
5. Compares all answers against worker=1 ground truth

## Project structure

```
src/pte/
  cli.py        CLI + SDK setup
  config.py     YAML loader
  executor.py   _ActionBatch.prepare() + answer capture
  metrics.py    BatchRecord, ChainRecord, ConcurrencyTracker
  runner.py     Workspace, tool registry, benchmark loop, output
```
