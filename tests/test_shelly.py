from __future__ import annotations

import unittest
from typing import Any

from vrm_solar_automation.shelly import ShellyPlugClient, ShellySettings


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)


class FakeSession:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return FakeResponse(self._responses.pop(0))


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class ShellyPlugClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_on_for_uses_toggle_after(self) -> None:
        session = FakeSession([{"was_on": False}])
        client = ShellyPlugClient(
            ShellySettings(host="plug.local"),
            session_factory=lambda: FakeSessionContext(session),
        )

        result = await client.turn_on_for(600)

        self.assertTrue(result.output)
        self.assertFalse(result.was_on)
        self.assertEqual(
            session.calls,
            [
                (
                    "/rpc/Switch.Set",
                    {"json": {"id": 0, "on": True, "toggle_after": 600.0}},
                )
            ],
        )

    async def test_turn_off_for_uses_toggle_after(self) -> None:
        session = FakeSession([{"was_on": True}])
        client = ShellyPlugClient(
            ShellySettings(host="plug.local"),
            session_factory=lambda: FakeSessionContext(session),
        )

        result = await client.turn_off_for(120)

        self.assertFalse(result.output)
        self.assertTrue(result.was_on)
        self.assertEqual(
            session.calls,
            [
                (
                    "/rpc/Switch.Set",
                    {"json": {"id": 0, "on": False, "toggle_after": 120.0}},
                )
            ],
        )

    async def test_fetch_device_info_maps_expected_fields(self) -> None:
        session = FakeSession(
            [
                {
                    "name": "Pump Plug",
                    "id": "shellyplugsg3-123456",
                    "mac": "AABBCCDDEEFF",
                    "model": "S3PL-0012EU",
                    "gen": 3,
                    "app": "PlusPlugS",
                    "ver": "1.6.2",
                    "auth_en": True,
                    "auth_domain": "shellyplugsg3-123456",
                }
            ]
        )
        client = ShellyPlugClient(
            ShellySettings(host="plug.local", username="admin", password="secret"),
            session_factory=lambda: FakeSessionContext(session),
        )

        info = await client.fetch_device_info()

        self.assertEqual(info.device_id, "shellyplugsg3-123456")
        self.assertEqual(info.model, "S3PL-0012EU")
        self.assertTrue(info.auth_enabled)
        self.assertEqual(session.calls[0][0], "/rpc/Shelly.GetDeviceInfo")


if __name__ == "__main__":
    unittest.main()
