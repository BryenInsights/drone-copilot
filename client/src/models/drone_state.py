"""DroneState and Telemetry Pydantic models."""

import time

from pydantic import BaseModel, Field, computed_field

_TAKEOFF_GRACE_PERIOD_S: float = 8.0
_GROUND_ALTITUDE_THRESHOLD: float = 5.0


class DroneState(BaseModel):
    """Current physical state of the drone, updated via telemetry polling."""

    battery: int = Field(default=100, ge=0, le=100)
    altitude: float = Field(default=0.0, ge=0.0)
    temperature: int = Field(default=0, ge=0, le=100)
    is_connected: bool = False
    speed: int = Field(default=10, ge=10, le=100)
    flight_time: int = Field(default=0, ge=0)
    wifi_snr: int = 0
    takeoff_time: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_flying(self) -> bool:
        if self.takeoff_time is not None:
            elapsed = time.time() - self.takeoff_time
            if elapsed < _TAKEOFF_GRACE_PERIOD_S:
                return True  # Trust takeoff_time during stabilization
            if self.altitude < _GROUND_ALTITUDE_THRESHOLD:
                return False  # Physical auto-landing detected
            return True
        return self.altitude >= 10.0


class Telemetry(BaseModel):
    """Dashboard-optimized subset of DroneState for WebSocket transmission."""

    battery: int = Field(ge=0, le=100)
    altitude: float = Field(ge=0.0)
    temperature: int = 0
    flight_time: int = Field(ge=0)
    wifi_snr: int = 0
    is_flying: bool = False
