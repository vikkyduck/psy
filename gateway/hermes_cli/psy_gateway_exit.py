"""A process-exit deadline that starts only after durable gateway shutdown.

Embedded gateway users never install this guard. The two foreground CLI entrypoints
install it, and the shutdown tail arms it only after the real stop task has finished
closing adapters, checkpointing work and closing the session databases. Active work
and lecture generation have no deadline here.
"""
from __future__ import annotations

from contextvars import ContextVar, copy_context
import math
import os
import threading
from typing import Callable


_current_guard: ContextVar[GatewayProcessExitGuard | None] = ContextVar(
    "psy_gateway_process_exit_guard", default=None
)


def arm_after_durable_shutdown(exit_code: int) -> None:
    """No-op for embedded users. Called after the gateway stop task succeeds."""
    guard = _current_guard.get()
    if guard is not None:
        guard.arm(exit_code)


class GatewayProcessExitGuard:
    """Keep a short finalization deadline alive across asyncio.run() teardown.

The normal exit callback performs the existing idempotent lock/lifecycle/log cleanup.
If that callback itself blocks, a separate OS thread exits after the emergency grace.
    Neither callback runs before ``arm_after_durable_shutdown`` reaches the safe barrier.
"""

    def __init__(
        self, exit_callback: Callable[[int], None], *, timeout: float = 12.0,
        emergency_grace: float = 2.0, hard_exit: Callable[[int], None] = os._exit,
    ):
        for value in (timeout, emergency_grace):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Process-finalization deadlines must be finite and positive")
        self._exit_callback = exit_callback
        self._hard_exit = hard_exit
        self._timeout = timeout
        self._emergency_grace = emergency_grace
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._timer = None
        self._emergency_timer = None
        self._context_token = None
        self._exit_code = 1

    def __enter__(self):
        self._context_token = _current_guard.set(self)
        return self

    def arm(self, exit_code: int) -> None:
        if not isinstance(exit_code, int):
            raise TypeError("Gateway exit code must be an integer")
        with self._lock:
            if self._closed.is_set() or self._timer is not None:
                return
            self._exit_code = int(exit_code)
            # Preserve the selected profile for lifecycle/lock cleanup in this thread.
            self._timer = threading.Timer(self._timeout, copy_context().run, (self._expire,))
            self._timer.daemon = True
            self._timer.start()

    def _emergency_exit(self, exit_code: int) -> None:
        if not self._closed.is_set():
            self._hard_exit(exit_code)

    def _expire(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            exit_code = self._exit_code
            self._emergency_timer = threading.Timer(
                self._emergency_grace, self._emergency_exit, (exit_code,)
            )
            self._emergency_timer.daemon = True
            self._emergency_timer.start()
        try:
            self._exit_callback(exit_code)
        finally:
            # A production callback should never return. Preserve the deadline if
            # a changed callback raises or returns after a failed cleanup step.
            self._emergency_exit(exit_code)

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None and self._timer is not None:
            # The process is already safely stopped. A CLI exception can still
            # strand non-daemon threads during interpreter finalization, so keep
            # the guard alive and preserve the exception's process exit status.
            with self._lock:
                code = exc_value.code if isinstance(exc_value, SystemExit) else 1
                self._exit_code = 0 if code is None else int(code) if isinstance(code, int) else 1
            if self._context_token is not None:
                _current_guard.reset(self._context_token)
            return False
        self._closed.set()
        with self._lock:
            for timer in (self._timer, self._emergency_timer):
                if timer is not None:
                    timer.cancel()
        if self._context_token is not None:
            _current_guard.reset(self._context_token)
        return False
