"""DroneController — high-level drone operations wrapping CommandExecutor with SafetyGuard."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from client.src.config import ClientConfig
from client.src.drone.command_executor import CommandExecutor
from client.src.drone.safety_guard import SafetyGuard
from client.src.models.drone_state import DroneState, Telemetry

logger = logging.getLogger(__name__)


class DroneController:
    """High-level drone control with safety validation.

    Wraps CommandExecutor with SafetyGuard validation.
    Maintains DroneState via telemetry polling.
    Accepts real Tello or MockDrone.
    """

    def __init__(self, drone: Any, config: ClientConfig) -> None:
        self.drone = drone
        self.config = config
        self.state = DroneState(is_connected=True)
        self.safety = SafetyGuard(config, self.state)
        self.executor = CommandExecutor(drone, config, state=self.state)
        self._on_emergency_land: Callable[[str], None] | None = None
        self._commanded_landing = False

    def set_emergency_land_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked after an automatic emergency landing."""
        self._on_emergency_land = callback

    def _check_connection(self) -> dict | None:
        """Return error dict if drone is disconnected, None if OK."""
        if not self.state.is_connected:
            return {
                "success": False,
                "error": "connection_lost",
                "message": "Drone connection lost",
            }
        return None

    def _heartbeat_safety_check(self) -> None:
        """Periodic safety check called from heartbeat thread (outside lock)."""
        if not self.state.is_flying:
            return
        was_flying = self.state.is_flying
        self.poll_telemetry()

        # Detect silent auto-landing (e.g. Tello 15s timeout)
        if was_flying and not self.state.is_flying:
            if not self._commanded_landing:
                logger.warning("Heartbeat: drone auto-landed (external/timeout)")
                self.state.takeoff_time = None
                if self._on_emergency_land:
                    self._on_emergency_land(
                        "Drone auto-landed unexpectedly (possible Tello timeout)"
                    )
            return

        if self.safety.check_battery_critical():
            logger.warning("Heartbeat: critical battery %d%% — auto-landing", self.state.battery)
            self.emergency_land()
            if self._on_emergency_land:
                self._on_emergency_land(
                    f"Critical battery {self.state.battery}% — auto-landed for safety"
                )
        elif self.safety.check_temperature_critical():
            logger.warning(
                "Heartbeat: critical temperature %d°C — auto-landing", self.state.temperature
            )
            self.emergency_land()
            if self._on_emergency_land:
                self._on_emergency_land(
                    f"Critical temperature {self.state.temperature}°C — auto-landed for safety"
                )

    def start(self) -> None:
        """Start heartbeat and telemetry polling."""
        self.executor.set_heartbeat_safety_callback(self._heartbeat_safety_check)
        self.executor.start_heartbeat()
        self.poll_telemetry()

    def stop(self) -> None:
        """Stop heartbeat."""
        self.executor.stop_heartbeat()

    def poll_telemetry(self) -> None:
        """Update DroneState from drone telemetry."""
        try:
            self.state.battery = self.drone.get_battery()
            self.state.altitude = float(self.drone.get_height())
            self.state.temperature = self.drone.get_temperature()
            self.state.flight_time = self.drone.get_flight_time()
            self.state.is_connected = True
            self.state.last_telemetry_time = time.time()
        except Exception:
            logger.warning("Telemetry poll failed", exc_info=True)
            self.state.is_connected = False

    def get_telemetry(self) -> Telemetry:
        """Get current telemetry for dashboard."""
        self.poll_telemetry()
        return Telemetry(
            battery=self.state.battery,
            altitude=self.state.altitude,
            temperature=self.state.temperature,
            flight_time=self.state.flight_time,
            wifi_snr=self.state.wifi_snr,
            is_flying=self.state.is_flying,
        )

    def takeoff(self) -> dict:
        """Validate and execute takeoff with stabilization wait."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        result = self.safety.validate_takeoff()
        if not result.safe:
            logger.warning("Takeoff rejected: %s", result.reason)
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        self.executor.execute_command(self.drone.takeoff)
        self.state.takeoff_time = time.time()

        self.poll_telemetry()
        logger.info("Takeoff complete, altitude=%.0fcm", self.state.altitude)
        return {"success": True, "result": "takeoff_complete"}

    def land(self) -> dict:
        """Execute graceful landing with retry."""
        self._commanded_landing = True
        try:
            self.executor.execute_command(self.drone.land)
            self.state.takeoff_time = None
            self.poll_telemetry()
            self._commanded_landing = False
            logger.info("Landing complete")
            return {"success": True, "result": "landed"}
        except Exception as e:
            logger.error("Land failed, retrying: %s", e)
            try:
                self.drone.land()
                self.state.takeoff_time = None
                self._commanded_landing = False
                return {"success": True, "result": "landed_retry"}
            except Exception as e2:
                self._commanded_landing = False
                logger.error("Land retry failed: %s", e2)
                return {"success": False, "error": "land_failed", "message": str(e2)}

    def emergency_land(self) -> dict:
        """Emergency landing — FR-007: land() → raw SDK land → motor stop.

        If less than 5s since takeoff, wait remaining stabilization time
        before landing (lesson D2).
        """
        if not self.state.is_flying:
            logger.info("Emergency land skipped — already on the ground")
            self.state.takeoff_time = None
            return {"success": True, "result": "already_landed"}

        logger.warning("EMERGENCY LAND initiated")
        self.executor.cancel_commands()

        # Check time-since-takeoff — landing within 5s of takeoff is unreliable
        if self.state.takeoff_time is not None:
            elapsed = time.time() - self.state.takeoff_time
            if elapsed < 5.0:
                wait = 5.0 - elapsed
                logger.warning("Waiting %.1fs (takeoff was <5s ago) before emergency land", wait)
                time.sleep(wait)

        # Layer 1: graceful land
        try:
            self.drone.land()
            self.state.takeoff_time = None
            logger.info("Emergency: graceful land succeeded")
            return {"success": True, "result": "emergency_landed"}
        except Exception:
            logger.error("Emergency: graceful land failed, trying raw land")

        # Layer 2: raw SDK land
        try:
            self.drone.land()
            self.state.takeoff_time = None
            return {"success": True, "result": "emergency_landed_raw"}
        except Exception:
            logger.error("Emergency: raw land failed, motor stop")

        # Layer 3: motor stop
        try:
            self.drone.emergency()
            self.state.takeoff_time = None
            return {"success": True, "result": "emergency_motor_stop"}
        except Exception as e:
            logger.critical("Emergency: ALL landing methods failed: %s", e)
            return {"success": False, "error": "emergency_failed", "message": str(e)}

    def move(
        self, direction: str, distance_cm: int, *, delay_override: float | None = None,
    ) -> dict:
        """Validate, clamp, and execute a move command."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        result = self.safety.validate_command()
        if not result.safe:
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        clamped = self.safety.clamp_move_distance(distance_cm, direction)

        move_methods = {
            "forward": self.drone.move_forward,
            "back": self.drone.move_back,
            "left": self.drone.move_left,
            "right": self.drone.move_right,
            "up": self.drone.move_up,
            "down": self.drone.move_down,
        }
        method = move_methods.get(direction)
        if method is None:
            return {
                "success": False,
                "error": "invalid_direction",
                "message": f"Unknown: {direction}",
            }

        delay = (
            delay_override if delay_override is not None
            else self.config.INTER_COMMAND_MOVE_DELAY
        )
        self.executor.execute_command(
            lambda: method(clamped),
            delay=delay,
        )
        self.poll_telemetry()
        logger.info("Moved %s %dcm", direction, clamped)
        return {"success": True, "result": f"moved_{direction}_{clamped}cm"}

    def rotate(
        self, direction: str, degrees: int, *, delay_override: float | None = None,
    ) -> dict:
        """Validate, clamp, and execute a rotation command."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        result = self.safety.validate_command()
        if not result.safe:
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        clamped = self.safety.clamp_rotation(degrees)

        if direction == "clockwise":
            method = self.drone.rotate_clockwise
        elif direction == "counter_clockwise":
            method = self.drone.rotate_counter_clockwise
        else:
            return {
                "success": False,
                "error": "invalid_direction",
                "message": f"Unknown: {direction}",
            }

        delay = (
            delay_override if delay_override is not None
            else self.config.INTER_COMMAND_ROTATE_DELAY
        )
        self.executor.execute_command(
            lambda: method(clamped),
            delay=delay,
        )
        self.poll_telemetry()
        logger.info("Rotated %s %d°", direction, clamped)
        return {"success": True, "result": f"rotated_{direction}_{clamped}deg"}

    def curve(
        self,
        x1: int, y1: int, z1: int,
        x2: int, y2: int, z2: int,
        speed: int,
        *,
        delay_override: float | None = None,
    ) -> dict:
        """Execute a curve_xyz_speed command (arc flight)."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        result = self.safety.validate_command()
        if not result.safe:
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        # Clamp speed to SDK range 10-60, coords to -500..500
        speed = max(10, min(60, speed))
        x1 = max(-500, min(500, x1))
        y1 = max(-500, min(500, y1))
        z1 = max(-500, min(500, z1))
        x2 = max(-500, min(500, x2))
        y2 = max(-500, min(500, y2))
        z2 = max(-500, min(500, z2))

        delay = (
            delay_override if delay_override is not None
            else self.config.INTER_COMMAND_MOVE_DELAY
        )
        self.executor.execute_command(
            lambda: self.drone.curve_xyz_speed(x1, y1, z1, x2, y2, z2, speed),
            delay=delay,
        )
        self.poll_telemetry()
        logger.info(
            "Curve mid=(%d,%d,%d) end=(%d,%d,%d) speed=%d",
            x1, y1, z1, x2, y2, z2, speed,
        )
        return {"success": True, "result": f"curve_({x1},{y1},{z1})_({x2},{y2},{z2})"}

    def hover(self) -> dict:
        """Stop all movement — hover in place."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        result = self.safety.validate_command()
        if not result.safe:
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        self.executor.execute_command(
            lambda: self.drone.send_rc_control(0, 0, 0, 0)
        )
        logger.info("Hovering")
        return {"success": True, "result": "hovering"}

    def set_speed(self, speed: int) -> dict:
        """Set movement speed."""
        conn_err = self._check_connection()
        if conn_err:
            return conn_err
        clamped = max(10, min(100, speed))
        self.executor.execute_command(lambda: self.drone.set_speed(clamped))
        self.state.speed = clamped
        logger.info("Speed set to %d cm/s", clamped)
        return {"success": True, "result": f"speed_set_{clamped}"}

    def get_state_dict(self) -> dict:
        """Get current drone state as a dict for tool responses."""
        self.poll_telemetry()
        return {
            "battery": self.state.battery,
            "altitude": self.state.altitude,
            "is_flying": self.state.is_flying,
        }
