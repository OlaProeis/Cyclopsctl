"""Per-cycle agent lifecycle: new agent per implementation, reuse for update (task 7)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cursor_sdk import Agent, CursorAgentError, ModelSelection

from cyclopsctl.runner import (
    ActivityCallback,
    CreateAgentFn,
    PlanUpdateCallback,
    SendFn,
    SendRunResult,
    WaitFn,
    create_local_agent,
    send_and_wait,
)

logger = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """Invalid session state or lifecycle ordering."""


@dataclass
class CycleSession:
    """
    One cyclopsctl cycle: fresh agent for implementation, same agent for update.

    Create a new ``CycleSession`` for each cycle so implementation always
    starts with a new ``Agent.create`` call.
    """

    project_root: Path
    model: ModelSelection
    api_key: str | None = None
    create_agent: CreateAgentFn | None = None
    send_fn: SendFn | None = None
    wait_fn: WaitFn | None = None
    on_activity: ActivityCallback | None = None
    on_plan_update: PlanUpdateCallback | None = None
    _agent: Agent | None = field(default=None, init=False, repr=False)
    implementation: SendRunResult | None = field(default=None, init=False, repr=False)
    update: SendRunResult | None = field(default=None, init=False, repr=False)

    @property
    def agent_id(self) -> str | None:
        return self._agent.agent_id if self._agent is not None else None

    def start_implementation(self, prompt: str) -> SendRunResult:
        """Create a new agent and run the implementation prompt."""
        if self._agent is not None:
            self.close()

        self._agent = create_local_agent(
            model=self.model,
            project_root=self.project_root,
            api_key=self.api_key,
            create_agent=self.create_agent,
        )
        self.implementation = send_and_wait(
            self._agent,
            prompt,
            phase="implementation",
            send_fn=self.send_fn,
            wait_fn=self.wait_fn,
            on_activity=self.on_activity,
            on_plan_update=self.on_plan_update,
        )
        return self.implementation

    def run_update(self, prompt: str) -> SendRunResult:
        """Send the update prompt on the same agent handle."""
        if self._agent is None:
            raise SessionError(
                "Cannot run update phase before implementation has started"
            )

        self.update = send_and_wait(
            self._agent,
            prompt,
            phase="update",
            send_fn=self.send_fn,
            wait_fn=self.wait_fn,
            on_activity=self.on_activity,
            on_plan_update=self.on_plan_update,
        )
        return self.update

    def close(self) -> None:
        """Dispose the agent handle (best-effort).

        ``agent.close()`` is a bridge RPC. If the bridge died mid-run the RPC
        raises ``NetworkError`` (connection refused) — but the local agent
        process is already gone, so there is nothing left to clean up. Never
        let a cleanup failure crash a cycle that already completed.
        """
        if self._agent is None:
            return
        agent = self._agent
        self._agent = None
        try:
            agent.close()
        except CursorAgentError as exc:
            logger.warning(
                "Best-effort close of agent %s failed (bridge unreachable?): %s",
                agent.agent_id,
                exc,
            )
            return
        logger.info("Closed agent %s", agent.agent_id)

    def __enter__(self) -> CycleSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
