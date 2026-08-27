"""Per-cycle agent lifecycle: new agent per implementation, reuse for update (task 7)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cursor_sdk import Agent, CursorAgentError, ModelSelection

from cyclopsctl.runner import (
    ActivityCallback,
    AgentRunError,
    CreateAgentFn,
    PlanUpdateCallback,
    SendFn,
    SendRunResult,
    WaitFn,
    create_local_agent,
    send_and_wait,
    should_continue_same_agent,
)

# Sent once on the live agent handle after an empty-detail run error.
# Not a crash-resume (PRD out of scope): the SDK already returned, and the
# same CycleSession agent is often still usable for a follow-up send.
EMPTY_RUN_ERROR_CONTINUE_PROMPT = """\
The previous agent run ended with status=error and no SDK result detail \
(an infrastructure drop, not a reported task failure). The workspace and \
any in-progress edits are still on disk.

Continue the current phase from where you left off:
- Inspect the workspace, terminals, and last commands before doing more work.
- Do not redo completed edits.
- Do not re-run a full project test suite unless a targeted test shows a \
real failure or the previous suite never finished.
- Finish the current task only; do not start the next task or edit handover files.
"""

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
        self.implementation = self._send_and_wait_phase(
            prompt, phase="implementation"
        )
        return self.implementation

    def run_update(self, prompt: str) -> SendRunResult:
        """Send the update prompt on the same agent handle."""
        if self._agent is None:
            raise SessionError(
                "Cannot run update phase before implementation has started"
            )

        self.update = self._send_and_wait_phase(prompt, phase="update")
        return self.update

    def _send_and_wait_phase(self, prompt: str, *, phase: str) -> SendRunResult:
        """Send ``prompt``; on empty-detail run error, nudge the same agent once."""
        if self._agent is None:
            raise SessionError(f"Cannot send {phase} prompt without an agent")
        try:
            return self._send_and_wait(prompt, phase=phase)
        except AgentRunError as exc:
            if self._agent is None or not should_continue_same_agent(exc):
                raise
            logger.warning(
                "Empty-detail %s run error; sending same-agent continue "
                "(agent=%s run=%s diagnostic=%s)",
                phase,
                exc.agent_id,
                exc.run_id,
                exc.diagnostic_detail,
            )
            try:
                return self._send_and_wait(
                    EMPTY_RUN_ERROR_CONTINUE_PROMPT, phase=phase
                )
            except AgentRunError as continue_exc:
                _annotate_continue_failure(continue_exc, original=exc)
                raise continue_exc

    def _send_and_wait(self, prompt: str, *, phase: str) -> SendRunResult:
        if self._agent is None:
            raise SessionError(f"Cannot send {phase} prompt without an agent")
        return send_and_wait(
            self._agent,
            prompt,
            phase=phase,
            send_fn=self.send_fn,
            wait_fn=self.wait_fn,
            on_activity=self.on_activity,
            on_plan_update=self.on_plan_update,
        )

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


def _annotate_continue_failure(
    continue_exc: AgentRunError, *, original: AgentRunError
) -> None:
    """Mark a failed continue and keep the original mid-work diagnostic."""
    continue_exc.same_agent_continued = True
    original_detail = (original.diagnostic_detail or "").strip()
    continue_detail = (continue_exc.diagnostic_detail or "").strip()
    if original_detail and continue_detail:
        continue_exc.diagnostic_detail = (
            f"{continue_detail} (after continue; earlier: {original_detail})"
        )
    elif original_detail:
        continue_exc.diagnostic_detail = original_detail
