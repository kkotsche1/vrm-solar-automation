from __future__ import annotations

import os
import platform
from dataclasses import asdict, dataclass

from .config import Settings


@dataclass(frozen=True)
class RuntimeSupport:
    platform_system: str
    platform_release: str
    os_name: str
    is_native_windows: bool
    is_wsl: bool
    mqtt_requested: bool
    mqtt_supported: bool
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def detect_runtime_support(
    settings: Settings,
    *,
    os_name: str | None = None,
    platform_system: str | None = None,
    platform_release: str | None = None,
) -> RuntimeSupport:
    resolved_os_name = os_name or os.name
    resolved_platform_system = platform_system or platform.system()
    resolved_platform_release = platform_release or platform.release()
    release_lower = resolved_platform_release.lower()
    is_wsl = "microsoft" in release_lower or "wsl" in release_lower
    is_native_windows = resolved_os_name == "nt" and not is_wsl
    mqtt_requested = bool(settings.cerbo_mqtt_enabled)
    mqtt_supported = True
    reason = None

    if mqtt_requested and is_native_windows:
        mqtt_supported = False
        reason = (
            "Native Windows development does not support the Cerbo MQTT transport with the current "
            "Uvicorn/aiomqtt runtime combination. Use CERBO_MQTT_ENABLED=false on Windows, run the "
            "backend in WSL for live MQTT development, or run live MQTT on the Raspberry Pi/Linux target."
        )

    return RuntimeSupport(
        platform_system=resolved_platform_system,
        platform_release=resolved_platform_release,
        os_name=resolved_os_name,
        is_native_windows=is_native_windows,
        is_wsl=is_wsl,
        mqtt_requested=mqtt_requested,
        mqtt_supported=mqtt_supported,
        reason=reason,
    )


def ensure_supported_runtime(settings: Settings, runtime_support: RuntimeSupport | None = None) -> RuntimeSupport:
    support = runtime_support or detect_runtime_support(settings)
    if support.mqtt_requested and not support.mqtt_supported:
        raise RuntimeError(support.reason or "Unsupported runtime for Cerbo MQTT transport.")
    return support
