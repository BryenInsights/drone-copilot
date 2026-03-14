"""Command executor with serialization lock, heartbeat, and cancellation support."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from client.src.config import ClientConfig
from client.src.drone.exceptions import ConnectionLostError

logger = logging.getLogger(__name__)


class CancellationToken:
    """Thread-safe cancellation token for aborting in-flight commands."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def reset(self) -> None:
        self._cancelled.clear()

    def wait(self, timeout: float) -> bool:
        """Wait for cancellation. Returns True if cancelled during wait."""
        return self._cancelled.wait(timeout)


class CommandExecutor:
    """Serializes drone commands with safety delays and heartbeat.

    Uses threading.Lock (not asyncio.Lock) because the heartbeat runs in a
    separate thread and asyncio.Lock is not thread-safe.
    """

    def __init__(
        self,
        drone: Any,
        config: ClientConfig,
        state: Any | None = None,
    ) -> None:
        self.drone = drone
        self.config = config
        self._state = state
        self._lock = threading.Lock()
        self._cancellation = CancellationToken()
        self._last_command_time: float = 0.0
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_paused = threading.Event()  # Set = paused
        self._running = False
        self._on_heartbeat_safety: Callable[[], None] | None = None

    def set_heartbeat_safety_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to run after each heartbeat (outside the lock)."""
        self._on_heartbeat_safety = callback

    def start_heartbeat(self) -> None:
        """Start the heartbeat thread to prevent 15s Tello auto-land."""
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._heartbeat_thread.start()
        logger.info("Heartbeat thread started (interval=%ds)", self.config.HEARTBEAT_INTERVAL)

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat thread."""
        self._running = False
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5.0)
            self._heartbeat_thread = None
        logger.info("Heartbeat thread stopped")

    def pause_heartbeat(self) -> None:
        """Pause heartbeat to prevent UDP response interleaving with flight commands."""
        self._heartbeat_paused.set()
        logger.debug("Heartbeat paused")

    def resume_heartbeat(self) -> None:
        """Resume heartbeat after flight command completes."""
        self._heartbeat_paused.clear()
        logger.debug("Heartbeat resumed")

    def _heartbeat_loop(self) -> None:
        """Send periodic queries to keep the Tello connection alive."""
        # Wait for initialization commands (connect, streamon) to settle
        self._heartbeat_stop.wait(timeout=5.0)
        if self._heartbeat_stop.is_set():
            return

        while self._running and not self._heartbeat_stop.is_set():
            # Skip heartbeat while paused (prevents UDP response interleaving)
            if self._heartbeat_paused.is_set():
                self._heartbeat_stop.wait(timeout=1.0)
                continue
            # Non-blocking acquire — skip if a command is in progress (lesson C5)
            if self._lock.acquire(blocking=False):
                try:
                    self.drone.send_rc_control(0, 0, 0, 0)
                    logger.debug("Heartbeat: RC keepalive sent")
                except Exception:
                    logger.warning("Heartbeat: keepalive failed", exc_info=True)
                finally:
                    self._lock.release()

                # Run safety callback outside the lock — no deadlock risk
                if self._on_heartbeat_safety is not None:
                    try:
                        self._on_heartbeat_safety()
                    except Exception:
                        logger.warning("Heartbeat safety callback error", exc_info=True)
            else:
                logger.debug("Heartbeat: skipped — command in progress")

            self._heartbeat_stop.wait(timeout=self.config.HEARTBEAT_INTERVAL)

    def execute_command(
        self,
        command_fn: Callable[[], None],
        delay: float | None = None,
    ) -> bool:
        """Execute a drone command with lock, cancellation check, and delay enforcement.

        Returns True if command executed, False if cancelled or failed.
        """
        if self._state is not None and not self._state.is_connected:
            raise ConnectionLostError("Drone connection lost — cannot execute command")

        if self._cancellation.is_cancelled:
            logger.info("Command skipped — cancellation requested")
            return False

        with self._lock:
            if self._cancellation.is_cancelled:
                return False

            # Enforce inter-command delay
            if delay is not None:
                elapsed = time.time() - self._last_command_time
                if elapsed < delay:
                    wait_time = delay - elapsed
                    logger.debug("Waiting %.1fs inter-command delay", wait_time)
                    if self._cancellation.wait(wait_time):
                        return False  # Cancelled during wait

            try:
                command_fn()
                self._last_command_time = time.time()
                return True
            except Exception:
                logger.error("Command execution failed", exc_info=True)
                raise

    def cancel_commands(self) -> None:
        """Request cancellation of any in-flight commands."""
        self._cancellation.cancel()
        logger.info("Command cancellation requested")

    def reset_cancellation(self) -> None:
        """Reset the cancellation token for new commands."""
        self._cancellation.reset()

    @property
    def cancellation_token(self) -> CancellationToken:
        return self._cancellation
