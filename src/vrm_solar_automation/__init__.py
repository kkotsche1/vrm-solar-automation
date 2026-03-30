from importlib import import_module

from .client import VrmProbeClient
from .models import PowerSnapshot
from .policy import PumpDecision, PumpPolicy, PumpPolicyConfig
from .runtime import RuntimeSupport, detect_runtime_support, ensure_supported_runtime
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
from .telemetry import CerboMqttClient, ControlCoordinator, TelemetryHub, TelemetryRuntimeStatus

__all__ = [
    "PowerSnapshot",
    "app",
    "create_app",
    "CerboMqttClient",
    "ControlCoordinator",
    "detect_runtime_support",
    "ensure_supported_runtime",
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
    "RuntimeSupport",
    "TelemetryHub",
    "TelemetryRuntimeStatus",
    "VrmProbeClient",
]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        api_module = import_module(".api", __name__)
        return getattr(api_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
