"""Benchmark executor: drives the SDK's _ActionBatch and ParallelToolExecutor."""

import json
import logging
import time
import tracemalloc
import uuid
from dataclasses import dataclass, field
from typing import Any

from openhands.sdk.agent.agent import _ActionBatch
from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
from openhands.sdk.event.base import Event
from openhands.sdk.event.llm_convertible import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
)
from openhands.sdk.llm import MessageToolCall
from openhands.sdk.tool.schema import Action, Observation
from openhands.sdk.tool.tool import ToolDefinition

from pte.metrics import (
    BatchRecord,
    ChainRecord,
    ConcurrencyTracker,
    ConversationMetrics,
    ToolExecution,
)

logger = logging.getLogger(__name__)


def make_action_event(
    tool_name: str,
    action: Action,
    action_kwargs: dict[str, Any],
) -> ActionEvent:
    """Build a minimal ActionEvent suitable for _ActionBatch."""
    uid = uuid.uuid4().hex
    call_id = f"call_{uid[:12]}"
    return ActionEvent(
        source="agent",
        thought=[],
        action=action,
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_call=MessageToolCall(
            id=call_id,
            name=tool_name,
            arguments=json.dumps(action_kwargs),
            origin="completion",
        ),
        llm_response_id=f"bench_{uid[12:20]}",
    )


def _extract_observation_text(observation: Observation) -> str:
    """Extract comparable text from an observation."""
    for attr in ("text", "content"):
        val = getattr(observation, attr, None)
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, (list, tuple)):
            return str(val)
    return str(observation)


@dataclass
class _MinimalState:
    """Stand-in for ConversationState — nothing is ever blocked."""

    def pop_blocked_action(self, action_id: str) -> str | None:
        return None


@dataclass(slots=True)
class ChainResult:
    """Result of a chain execution: metrics + ordered answers."""

    record: ChainRecord
    answers: list[str]
    tool_names: list[str]
    peak_memory_bytes: int = 0


@dataclass
class BenchmarkExecutor:
    """Execute tool calls via the SDK's _ActionBatch and ParallelToolExecutor."""

    max_workers: int = 1
    metrics: ConversationMetrics = field(default_factory=ConversationMetrics)
    _executor: ParallelToolExecutor = field(init=False, repr=False)
    _state: _MinimalState = field(default_factory=_MinimalState, repr=False)

    def __post_init__(self) -> None:
        self._executor = ParallelToolExecutor(max_workers=self.max_workers)

    def run_batch(
        self,
        action_events: list[ActionEvent],
        tools_map: dict[str, ToolDefinition],
    ) -> list[str]:
        """Execute a batch and return ordered answers."""
        if not action_events:
            return []

        tracker = ConcurrencyTracker()
        answers_by_id: dict[str, str] = {}
        batch_start = time.perf_counter()

        def tool_runner(ae: ActionEvent) -> list[Event]:
            events = self._run_one(ae, tools_map, tracker)
            for ev in events:
                if isinstance(ev, ObservationEvent):
                    answers_by_id[ae.id] = _extract_observation_text(ev.observation)
                elif isinstance(ev, AgentErrorEvent):
                    answers_by_id[ae.id] = f"ERROR: {ev.error}"
            return events

        _ActionBatch.prepare(
            action_events=action_events,
            state=self._state,
            executor=self._executor,
            tool_runner=tool_runner,
        )
        wall = time.perf_counter() - batch_start

        ordered = [answers_by_id.get(ae.id, "NO_ANSWER") for ae in action_events]

        batch_executions = self.metrics.executions[-len(action_events) :]
        self.metrics.record_batch(
            BatchRecord(
                batch_size=len(action_events),
                max_workers=self.max_workers,
                wall_clock_seconds=wall,
                peak_concurrency=tracker.peak,
                executions=list(batch_executions),
            )
        )
        return ordered

    def run_chain(
        self,
        chain_name: str,
        action_events: list[ActionEvent],
        tools_map: dict[str, ToolDefinition],
    ) -> ChainResult:
        """Execute a chain with memory tracking."""
        tracemalloc.start()
        chain_start = time.perf_counter()
        num_batches_before = len(self.metrics.batches)

        answers = self.run_batch(action_events, tools_map)
        tool_names = [ae.tool_name for ae in action_events]

        wall = time.perf_counter() - chain_start
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        batch_records = self.metrics.batches[num_batches_before:]
        record = ChainRecord(
            name=chain_name,
            num_batches=len(batch_records),
            wall_clock_seconds=wall,
            batch_records=list(batch_records),
        )
        self.metrics.record_chain(record)
        return ChainResult(record, answers, tool_names, peak_memory)

    def _run_one(
        self,
        ae: ActionEvent,
        tools_map: dict[str, ToolDefinition],
        tracker: ConcurrencyTracker,
    ) -> list[Event]:
        """Execute a single ActionEvent with instrumentation."""
        tool = tools_map.get(ae.tool_name)
        if tool is None:
            now = time.perf_counter()
            self._record(
                ae.tool_name, now, now, error=f"Tool '{ae.tool_name}' not found"
            )
            return [
                AgentErrorEvent(
                    error=f"Tool '{ae.tool_name}' not found",
                    tool_name=ae.tool_name,
                    tool_call_id=ae.tool_call_id,
                )
            ]

        tracker.enter()
        start = time.perf_counter()
        try:
            observation: Observation = tool(ae.action)
        except Exception as exc:
            end = time.perf_counter()
            tracker.exit()
            logger.error("Tool %s raised: %s", ae.tool_name, exc, exc_info=True)
            self._record(ae.tool_name, start, end, error=str(exc))
            return [
                AgentErrorEvent(
                    error=str(exc),
                    tool_name=ae.tool_name,
                    tool_call_id=ae.tool_call_id,
                )
            ]
        end = time.perf_counter()
        tracker.exit()

        error = ""
        if getattr(observation, "is_error", False):
            error = getattr(observation, "text", "tool returned is_error=True")

        self._record(ae.tool_name, start, end, error=error)
        return [
            ObservationEvent(
                observation=observation,
                action_id=ae.id,
                tool_name=ae.tool_name,
                tool_call_id=ae.tool_call_id,
            )
        ]

    def _record(
        self, tool_name: str, start: float, end: float, error: str = ""
    ) -> None:
        self.metrics.record(
            ToolExecution(
                tool_name=tool_name,
                start_time=start,
                end_time=end,
                is_error=bool(error),
                error_message=error,
            )
        )
