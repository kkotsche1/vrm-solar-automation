from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp


class _RpcSession(Protocol):
    async def post(self, url: str, **kwargs: Any) -> aiohttp.ClientResponse: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[_RpcSession]]


class ShellyError(RuntimeError):
    """Base exception for Shelly plug errors."""


class ShellyAuthenticationError(ShellyError):
    """Raised when the Shelly device rejects the provided credentials."""


class ShellyRpcError(ShellyError):
    """Raised when the Shelly device returns an RPC error or invalid payload."""


@dataclass(frozen=True)
class ShellySettings:
    host: str
    port: int = 80
    switch_id: int = 0
    username: str | None = None
    password: str | None = None
    use_https: bool = False
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("Shelly host must not be blank.")
        if (self.username is None) != (self.password is None):
            raise ValueError("Provide both SHELLY_USERNAME and SHELLY_PASSWORD, or neither.")
        if self.port <= 0:
            raise ValueError("Shelly port must be positive.")
        if self.switch_id < 0:
            raise ValueError("Shelly switch id must be zero or greater.")
        if self.timeout_seconds <= 0:
            raise ValueError("Shelly timeout must be positive.")

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class ShellyDeviceInfo:
    name: str | None
    device_id: str
    mac: str | None
    model: str | None
    generation: int | None
    application: str | None
    version: str | None
    auth_enabled: bool
    auth_domain: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_rpc(cls, payload: dict[str, Any]) -> "ShellyDeviceInfo":
        return cls(
            name=payload.get("name"),
            device_id=str(payload["id"]),
            mac=payload.get("mac"),
            model=payload.get("model"),
            generation=_maybe_int(payload.get("gen")),
            application=payload.get("app"),
            version=payload.get("ver"),
            auth_enabled=bool(payload.get("auth_en", False)),
            auth_domain=payload.get("auth_domain"),
        )


@dataclass(frozen=True)
class ShellySwitchStatus:
    switch_id: int
    output: bool
    source: str | None
    power_watts: float | None
    voltage_volts: float | None
    current_amps: float | None
    temperature_c: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_rpc(cls, payload: dict[str, Any], switch_id: int) -> "ShellySwitchStatus":
        temperature = payload.get("temperature")
        temperature_c = None
        if isinstance(temperature, dict):
            temperature_c = _maybe_float(temperature.get("tC"))

        return cls(
            switch_id=switch_id,
            output=bool(payload["output"]),
            source=payload.get("source"),
            power_watts=_maybe_float(payload.get("apower")),
            voltage_volts=_maybe_float(payload.get("voltage")),
            current_amps=_maybe_float(payload.get("current")),
            temperature_c=temperature_c,
        )


@dataclass(frozen=True)
class ShellySwitchCommandResult:
    switch_id: int
    requested_on: bool
    was_on: bool | None
    output: bool
    source: str | None
    toggle_after_seconds: float | None
    executed_at_iso: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_rpc(
        cls,
        payload: dict[str, Any],
        *,
        switch_id: int,
        requested_on: bool,
        toggle_after_seconds: float | None,
    ) -> "ShellySwitchCommandResult":
        return cls(
            switch_id=switch_id,
            requested_on=requested_on,
            was_on=bool(payload["was_on"]) if "was_on" in payload else None,
            output=bool(payload["output"]) if "output" in payload else requested_on,
            source=payload.get("source"),
            toggle_after_seconds=toggle_after_seconds,
            executed_at_iso=datetime.now(UTC).isoformat(),
        )


class ShellyPlugClient:
    def __init__(
        self,
        settings: ShellySettings,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory or self._build_session

    async def fetch_device_info(self) -> ShellyDeviceInfo:
        payload = await self._rpc("Shelly.GetDeviceInfo")
        return ShellyDeviceInfo.from_rpc(payload)

    async def fetch_switch_status(self, switch_id: int | None = None) -> ShellySwitchStatus:
        resolved_switch_id = self._resolve_switch_id(switch_id)
        payload = await self._rpc("Switch.GetStatus", {"id": resolved_switch_id})
        return ShellySwitchStatus.from_rpc(payload, resolved_switch_id)

    async def set_switch(
        self,
        on: bool,
        *,
        switch_id: int | None = None,
        toggle_after_seconds: float | None = None,
    ) -> ShellySwitchCommandResult:
        resolved_switch_id = self._resolve_switch_id(switch_id)
        params: dict[str, object] = {"id": resolved_switch_id, "on": on}
        normalized_toggle_after = _normalize_delay(toggle_after_seconds, allow_zero=False)
        if normalized_toggle_after is not None:
            params["toggle_after"] = normalized_toggle_after

        payload = await self._rpc("Switch.Set", params)
        return ShellySwitchCommandResult.from_rpc(
            payload,
            switch_id=resolved_switch_id,
            requested_on=on,
            toggle_after_seconds=normalized_toggle_after,
        )

    async def turn_on(
        self,
        *,
        switch_id: int | None = None,
        auto_off_seconds: float | None = None,
    ) -> ShellySwitchCommandResult:
        return await self.set_switch(
            True,
            switch_id=switch_id,
            toggle_after_seconds=auto_off_seconds,
        )

    async def turn_off(
        self,
        *,
        switch_id: int | None = None,
        auto_on_seconds: float | None = None,
    ) -> ShellySwitchCommandResult:
        return await self.set_switch(
            False,
            switch_id=switch_id,
            toggle_after_seconds=auto_on_seconds,
        )

    async def turn_on_for(
        self,
        duration_seconds: float,
        *,
        switch_id: int | None = None,
    ) -> ShellySwitchCommandResult:
        return await self.turn_on(
            switch_id=switch_id,
            auto_off_seconds=duration_seconds,
        )

    async def turn_off_for(
        self,
        duration_seconds: float,
        *,
        switch_id: int | None = None,
    ) -> ShellySwitchCommandResult:
        return await self.turn_off(
            switch_id=switch_id,
            auto_on_seconds=duration_seconds,
        )

    async def _rpc(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        try:
            async with self._session_factory() as session:
                response = await session.post(
                    f"/rpc/{method}",
                    json=params or {},
                )
                async with response:
                    if response.status == 401:
                        raise ShellyAuthenticationError(
                            "Shelly device rejected the supplied credentials."
                        )
                    if response.status >= 400:
                        text = await response.text()
                        raise ShellyRpcError(
                            f"{method} failed with HTTP {response.status}: "
                            f"{text.strip() or 'no response body'}"
                        )

                    try:
                        payload = await response.json()
                    except aiohttp.ContentTypeError as exc:
                        text = await response.text()
                        raise ShellyRpcError(
                            f"{method} returned a non-JSON response: "
                            f"{text.strip() or 'empty response'}"
                        ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ShellyError(
                f"Unable to reach Shelly device at {self._settings.base_url} for {method}."
            ) from exc

        if not isinstance(payload, dict):
            raise ShellyRpcError(f"{method} returned an unexpected payload type.")
        if "code" in payload and "message" in payload:
            raise ShellyRpcError(f"{method} RPC error {payload['code']}: {payload['message']}")
        return payload

    def _build_session(self) -> AbstractAsyncContextManager[aiohttp.ClientSession]:
        timeout = aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
        middlewares = ()
        if self._settings.username and self._settings.password:
            middlewares = (
                aiohttp.DigestAuthMiddleware(
                    login=self._settings.username,
                    password=self._settings.password,
                ),
            )
        return aiohttp.ClientSession(
            base_url=self._settings.base_url,
            timeout=timeout,
            middlewares=middlewares,
        )

    def _resolve_switch_id(self, switch_id: int | None) -> int:
        return self._settings.switch_id if switch_id is None else switch_id


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _normalize_delay(value: float | None, *, allow_zero: bool = False) -> float | None:
    if value is None:
        return None

    normalized = float(value)
    if normalized < 0 or (normalized == 0 and not allow_zero):
        raise ValueError("Delay values must be greater than zero.")
    return normalized
