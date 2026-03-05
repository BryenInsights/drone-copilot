"""PerceptionBridge — thread-safe bridge for feeding perception data to consumers.

Extracted from exploration.py. Used by InspectionMission, ToolHandler,
and the dashboard to share perception state.
"""

from __future__ import annotations

import logging
import threading

from client.src.models.tool_calls import ReportPerceptionParams

logger = logging.getLogger(__name__)


class PerceptionBridge:
    """Bridges report_perception tool calls to mission consumers and dashboard.

    Thread-safe: feed() can be called from any thread, and latest/active
    can be read from any thread.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._latest: ReportPerceptionParams | None = None
        self._lock = threading.Lock()
        self._active = False

    def activate(self) -> None:
        """Mark perception as active — results are expected."""
        self._active = True
        self._event.clear()

    def deactivate(self) -> None:
        """Mark perception as inactive."""
        self._active = False
        self._event.set()  # Unblock any waiters

    @property
    def active(self) -> bool:
        return self._active

    @property
    def latest(self) -> ReportPerceptionParams | None:
        """Most recent perception result, for dashboard reads."""
        with self._lock:
            return self._latest

    def feed(self, params: ReportPerceptionParams) -> None:
        """Called by ToolHandler when a report_perception tool call arrives."""
        with self._lock:
            self._latest = params
        self._event.set()
