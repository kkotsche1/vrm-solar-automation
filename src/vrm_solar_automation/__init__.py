from importlib import import_module

from .client import VrmProbeClient
from .models import PowerSnapshot
from .policy import PumpDecision, PumpPolicy, PumpPolicyConfig
from .shelly import (
    ShellyAuthenticationError,
    ShellyDeviceInfo,
    ShellyError,
    ShellyPlugClient,
    ShellyRpcError,
    ShellySettings,
    ShellySwitchCommandResult,
    ShellySwitchStatus,
)
from .system import PumpActuationResult, PumpControlSystem, PumpOverrideResult

__all__ = [
    "PowerSnapshot",
    "app",
    "create_app",
    "PumpActuationResult",
    "PumpDecision",
    "PumpControlSystem",
    "PumpOverrideResult",
    "PumpPolicy",
    "PumpPolicyConfig",
    "ShellyAuthenticationError",
    "ShellyDeviceInfo",
    "ShellyError",
    "ShellyPlugClient",
    "ShellyRpcError",
    "ShellySettings",
    "ShellySwitchCommandResult",
    "ShellySwitchStatus",
    "VrmProbeClient",
]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        api_module = import_module(".api", __name__)
        return getattr(api_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
