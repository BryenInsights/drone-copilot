"""DroneController — high-level drone operations wrapping CommandExecutor with SafetyGuard."""

from __future__ import annotations

import logging
import time
from typing import Any

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
        self.executor = CommandExecutor(drone, config)

    def start(self) -> None:
        """Start heartbeat and telemetry polling."""
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
        result = self.safety.validate_takeoff()
        if not result.safe:
            logger.warning("Takeoff rejected: %s", result.reason)
            return {"success": False, "error": "safety_check_failed", "message": result.reason}

        self.executor.execute_command(self.drone.takeoff)
        self.state.takeoff_time = time.time()

        # Wait for post-takeoff stabilization
        logger.info(
            "Waiting %.1fs for post-takeoff stabilization",
            self.config.POST_TAKEOFF_STABILIZATION,
        )
        time.sleep(self.config.POST_TAKEOFF_STABILIZATION)

        self.poll_telemetry()
        logger.info("Takeoff complete, altitude=%.0fcm", self.state.altitude)
        return {"success": True, "result": "takeoff_complete"}

    def land(self) -> dict:
        """Execute graceful landing with retry."""
        try:
            self.executor.execute_command(self.drone.land)
            self.state.takeoff_time = None
            self.poll_telemetry()
            logger.info("Landing complete")
            return {"success": True, "result": "landed"}
        except Exception as e:
            logger.error("Land failed, retrying: %s", e)
            try:
                self.drone.land()
                self.state.takeoff_time = None
                return {"success": True, "result": "landed_retry"}
            except Exception as e2:
                logger.error("Land retry failed: %s", e2)
                return {"success": False, "error": "land_failed", "message": str(e2)}

    def emergency_land(self) -> dict:
        """Emergency landing — FR-007: land() → raw SDK land → motor stop.

        If less than 5s since takeoff, wait remaining stabilization time
        before landing (lesson D2).
        """
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

    def move(self, direction: str, distance_cm: int) -> dict:
        """Validate, clamp, and execute a move command."""
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

        self.executor.execute_command(
            lambda: method(clamped),
            delay=self.config.INTER_COMMAND_MOVE_DELAY,
        )
        self.poll_telemetry()
        logger.info("Moved %s %dcm", direction, clamped)
        return {"success": True, "result": f"moved_{direction}_{clamped}cm"}

    def rotate(self, direction: str, degrees: int) -> dict:
        """Validate, clamp, and execute a rotation command."""
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

        self.executor.execute_command(
            lambda: method(clamped),
            delay=self.config.INTER_COMMAND_ROTATE_DELAY,
        )
        self.poll_telemetry()
        logger.info("Rotated %s %d°", direction, clamped)
        return {"success": True, "result": f"rotated_{direction}_{clamped}deg"}

    def hover(self) -> dict:
        """Stop all movement — hover in place."""
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
