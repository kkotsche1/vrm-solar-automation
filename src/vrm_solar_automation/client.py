from __future__ import annotations

from .cerbo import CerboProbeClient
from .config import Settings
from .mock_data import build_mock_power_snapshot
from .models import PowerSnapshot


class ProbeUnavailableError(RuntimeError):
    """Raised when the Cerbo probe cannot be reached or queried successfully."""


class VrmProbeClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def source(self) -> str:
        return "cerbo_mock" if self._settings.cerbo_mock_enabled else "cerbo_modbus"

    async def fetch_snapshot(self) -> PowerSnapshot:
        if self._settings.cerbo_mock_enabled:
            return build_mock_power_snapshot(self._settings)

        try:
            return await CerboProbeClient(self._settings.cerbo_settings()).fetch_snapshot()
        except (OSError, TimeoutError, RuntimeError) as exc:
            detail = str(exc)
            suffix = f" Details: {detail}" if detail else ""
            raise ProbeUnavailableError(
                f"Unable to reach Cerbo GX at {self._settings.cerbo_host}:{self._settings.cerbo_port}."
                f"{suffix}"
            ) from exc
