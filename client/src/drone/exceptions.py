"""Drone-specific exceptions."""


class ConnectionLostError(Exception):
    """Raised when a command is attempted after the drone connection is lost."""
