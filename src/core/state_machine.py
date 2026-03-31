from __future__ import annotations

from typing import Any, Callable, Coroutine, Dict, Optional, Set

from src.models.worker import WorkerState

# Valid transitions: from_state → set of allowed to_states
TRANSITION_TABLE: Dict[WorkerState, Set[WorkerState]] = {
    WorkerState.IDLE: {WorkerState.LAUNCHING, WorkerState.CANCELLED},
    WorkerState.LAUNCHING: {WorkerState.NAVIGATING, WorkerState.ERROR},
    WorkerState.NAVIGATING: {
        WorkerState.WAITING_FOR_CHALLENGE,
        WorkerState.READY,
        WorkerState.ERROR,
    },
    WorkerState.WAITING_FOR_CHALLENGE: {
        WorkerState.NAVIGATING,
        WorkerState.READY,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.READY: {
        WorkerState.SELECTING_MODEL,
        WorkerState.PASTING,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.SELECTING_MODEL: {WorkerState.PASTING, WorkerState.ERROR, WorkerState.CANCELLED},
    WorkerState.PASTING: {
        WorkerState.PREPARED,
        WorkerState.SUBMITTING,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.PREPARED: {
        WorkerState.SUBMITTING,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.SUBMITTING: {
        WorkerState.WAITING_FOR_CHALLENGE,
        WorkerState.POLLING,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.POLLING: {
        WorkerState.WAITING_FOR_CHALLENGE,
        WorkerState.COMPLETE,
        WorkerState.ERROR,
        WorkerState.CANCELLED,
    },
    WorkerState.COMPLETE: {WorkerState.IDLE, WorkerState.READY},
    WorkerState.ERROR: {WorkerState.IDLE},
    WorkerState.CANCELLED: {WorkerState.IDLE},
}

# Progress percentage mapped to each state (for the GUI progress bar)
STATE_PROGRESS: Dict[WorkerState, float] = {
    WorkerState.IDLE: 0.0,
    WorkerState.LAUNCHING: 5.0,
    WorkerState.NAVIGATING: 10.0,
    WorkerState.WAITING_FOR_CHALLENGE: 15.0,
    WorkerState.READY: 20.0,
    WorkerState.SELECTING_MODEL: 30.0,
    WorkerState.PASTING: 40.0,
    WorkerState.PREPARED: 45.0,
    WorkerState.SUBMITTING: 50.0,
    WorkerState.POLLING: 65.0,
    WorkerState.COMPLETE: 100.0,
    WorkerState.ERROR: 100.0,
    WorkerState.CANCELLED: 100.0,
}

TransitionCallback = Callable[
    [WorkerState, WorkerState, int], Coroutine[Any, Any, None]
]


class InvalidTransitionError(Exception):
    def __init__(
        self,
        worker_id: int,
        from_state: WorkerState,
        to_state: WorkerState,
    ):
        self.worker_id = worker_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Worker {worker_id}: invalid transition "
            f"{from_state.value} -> {to_state.value}"
        )


class WorkerStateMachine:
    """Manages state transitions for a single worker.

    Enforces valid transitions via TRANSITION_TABLE and fires an async
    callback on every successful transition.
    """

    def __init__(
        self,
        worker_id: int,
        on_transition: Optional[TransitionCallback] = None,
    ) -> None:
        self._worker_id = worker_id
        self._state = WorkerState.IDLE
        self._on_transition = on_transition

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def progress(self) -> float:
        return STATE_PROGRESS[self._state]

    @property
    def is_terminal(self) -> bool:
        return self._state in {
            WorkerState.COMPLETE,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        }

    async def transition(self, to_state: WorkerState) -> None:
        """Transition to *to_state*. Raises on invalid transition."""
        allowed = TRANSITION_TABLE.get(self._state, set())
        if to_state not in allowed:
            raise InvalidTransitionError(
                self._worker_id, self._state, to_state
            )

        old_state = self._state
        self._state = to_state

        if self._on_transition:
            await self._on_transition(old_state, to_state, self._worker_id)

    async def force_error(self, reason: str = "") -> None:
        """Force transition to ERROR from any non-terminal state."""
        if not self.is_terminal:
            old = self._state
            self._state = WorkerState.ERROR
            if self._on_transition:
                await self._on_transition(
                    old, WorkerState.ERROR, self._worker_id
                )

    async def reset(self) -> None:
        """Reset to IDLE from any terminal state."""
        if self.is_terminal:
            old = self._state
            self._state = WorkerState.IDLE
            if self._on_transition:
                await self._on_transition(
                    old, WorkerState.IDLE, self._worker_id
                )
