# PTE — Parallel Tool Execution Benchmark

Benchmarks concurrency safety of [OpenHands software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) parallel tool execution. Uses the SDK's `_ActionBatch` and `ParallelToolExecutor` against a real codebase (CPython). No LLMs needed.

Context: [#2526](https://github.com/OpenHands/software-agent-sdk/issues/2526) (safety analysis), [#2527](https://github.com/OpenHands/software-agent-sdk/issues/2527) (implementation plan).

## Quick start

```bash
uv sync
uv run pte benchmark.yaml                          # all 39 chains
uv run pte benchmark.yaml --skip-install            # reuse existing SDK clone
uv run pte benchmark.yaml --chain tool              # single-tool only
uv run pte benchmark.yaml --chain impl-p0           # P0 verification only
uv run pte benchmark.yaml --save                    # save answers to results/
uv run pte benchmark.yaml --chain                   # list chains
```

## CLI flags

| Flag | Description |
|---|---|
| `--branch` | SDK branch to checkout (default: `main`) |
| `--chain [NAME ...]` | Run named chains or prefix groups (`tool`, `cross`, `impl`, `impl-p0`, ...) |
| `--save [DIR]` | Save answers + diffs (default: `results/`) |
| `--skip-install` | Skip SDK clone/checkout |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Chains

39 chains in 3 groups. Each chain sends all its calls to a single `_ActionBatch.prepare()`. Worker=1 is ground truth.

| Group | Prefix | Count | What it tests |
|---|---|---|---|
| Single-tool | `tool-*` | 13 | One tool type under concurrency stress |
| Cross-tool | `cross-*` | 7 | Interactions between different tool types |
| Implementation | `impl-*` | 19 | Verifies [#2527 Section 10](https://github.com/OpenHands/software-agent-sdk/issues/2527#issuecomment-4104045362) priorities |

## Output

```
  w=1 ✓ tool-terminal
  w=2 ✗ tool-terminal
  w=4 ✓ tool-glob

                      Benchmark Results
┏━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ Chain         ┃  W ┃ Wall (s) ┃ Speedup ┃ Mem(MB) ┃ Errors ┃ Unique ┃
┡━━━━━━━━━━━━━━━╇━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ tool-terminal │  1 │    0.602 │   1.00x │     0.1 │      0 │    1/3 │
│ tool-terminal │  2 │    0.304 │   1.99x │     0.2 │     12 │    3/3 │
└───────────────┴────┴──────────┴─────────┴─────────┴────────┴────────┘

Time vs w=1: w=2: -1.23s | w=4: -2.50s
Total errors: 24
```

| Column | Meaning |
|---|---|
| **W** | Worker threads |
| **Wall** | Mean wall-clock time |
| **Speedup** | `sum(tool_durations) / wall_clock` |
| **Errors** | Tool exceptions + ground truth mismatches |
| **Unique** | Distinct answer sets / total runs. `1/N` = deterministic |

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
