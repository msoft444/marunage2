from collections.abc import Iterable


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ALLOWED_TRANSITIONS = {
    "queued": {"leased"},
    "leased": {"running", "blocked", "cancelled"},
    "running": {"succeeded", "failed", "blocked", "waiting_approval", "cancelled"},
    "blocked": {"queued"},
    "waiting_approval": {"succeeded", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


class TaskStateMachine:
    @staticmethod
    def can_transition(current: str, new: str) -> bool:
        return new in ALLOWED_TRANSITIONS.get(current, set())

    @staticmethod
    def allowed_targets(current: str) -> Iterable[str]:
        return tuple(ALLOWED_TRANSITIONS.get(current, set()))