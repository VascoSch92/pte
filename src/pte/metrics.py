"""Metrics for parallel tool execution benchmarking."""

import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ToolExecution:
    """A single tool execution record."""

    tool_name: str
    start_time: float
    end_time: float
    is_error: bool = False
    error_message: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.end_time - self.start_time


@dataclass
class BatchRecord:
    """Metrics for a single _ActionBatch.prepare() call."""

    batch_size: int
    max_workers: int
    wall_clock_seconds: float
    peak_concurrency: int
    executions: list[ToolExecution] = field(default_factory=list)

    @property
    def speedup(self) -> float:
        """sum(durations) / wall_clock. 1.0 = sequential, higher = more parallel."""
        if not self.executions or self.wall_clock_seconds == 0:
            return 1.0
        return (
            sum(e.duration_seconds for e in self.executions) / self.wall_clock_seconds
        )


@dataclass
class ChainRecord:
    """Metrics for a full chain execution."""

    name: str
    num_batches: int
    wall_clock_seconds: float
    batch_records: list[BatchRecord] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return sum(b.batch_size for b in self.batch_records)

    @property
    def speedup(self) -> float:
        """Weighted speedup: total_cpu / total_wall across all batches."""
        if not self.batch_records:
            return 1.0
        total_wall = sum(b.wall_clock_seconds for b in self.batch_records)
        if total_wall == 0:
            return 1.0
        total_cpu = sum(
            e.duration_seconds for b in self.batch_records for e in b.executions
        )
        return total_cpu / total_wall

    @property
    def tool_errors(self) -> int:
        return sum(1 for b in self.batch_records for e in b.executions if e.is_error)


@dataclass
class ConversationMetrics:
    """Aggregated metrics for a benchmark run."""

    executions: list[ToolExecution] = field(default_factory=list)
    batches: list[BatchRecord] = field(default_factory=list)
    chains: list[ChainRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, execution: ToolExecution) -> None:
        with self._lock:
            self.executions.append(execution)
        logger.debug(
            "tool=%s duration=%.4f error=%s",
            execution.tool_name,
            execution.duration_seconds,
            execution.is_error,
        )

    def record_batch(self, batch: BatchRecord) -> None:
        with self._lock:
            self.batches.append(batch)
        logger.debug(
            "batch: size=%d concurrency=%d wall=%.4f speedup=%.2f",
            batch.batch_size,
            batch.peak_concurrency,
            batch.wall_clock_seconds,
            batch.speedup,
        )

    def record_chain(self, chain: ChainRecord) -> None:
        with self._lock:
            self.chains.append(chain)
        logger.debug(
            "chain: name=%s calls=%d wall=%.4f",
            chain.name,
            chain.total_calls,
            chain.wall_clock_seconds,
        )


@dataclass
class ConcurrencyTracker:
    """Tracks peak concurrent executions within a batch."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _active: int = field(default=0, repr=False)
    _peak: int = 0

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1

    @property
    def peak(self) -> int:
        return self._peak
