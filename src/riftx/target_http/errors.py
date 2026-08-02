"""Explicit interruption outcomes for Target HTTP Runner execution."""

from __future__ import annotations

import asyncio

from .models import TargetHttpRunnerStopOutcome


class TargetHttpRunnerExecutionUncertainError(RuntimeError):
    """The dispatch completed, but the Runner effect may still be active."""

    def __init__(
        self,
        message: str,
        *,
        stop_outcome: TargetHttpRunnerStopOutcome,
    ) -> None:
        super().__init__(message)
        self.stop_outcome = stop_outcome


class TargetHttpRunnerExecutionCancelledError(asyncio.CancelledError):
    """Control Plane cancellation annotated with the Runner stop acknowledgement."""

    def __init__(
        self,
        message: str,
        *,
        stop_outcome: TargetHttpRunnerStopOutcome,
    ) -> None:
        super().__init__(message)
        self.stop_outcome = stop_outcome
