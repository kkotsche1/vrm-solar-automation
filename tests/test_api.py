from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
HTTPX_AVAILABLE = importlib.util.find_spec("httpx") is not None

if FASTAPI_AVAILABLE and HTTPX_AVAILABLE:
    from fastapi.testclient import TestClient

    from vrm_solar_automation.api import create_app
    from vrm_solar_automation.config import Settings


@unittest.skipUnless(
    FASTAPI_AVAILABLE and HTTPX_AVAILABLE,
    "FastAPI API tests require fastapi and httpx to be installed.",
)
class ApiTests(unittest.TestCase):
    def test_control_loop_starts_automatically_on_backend_startup(self) -> None:
        system = FakeSystem()
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(control_interval_seconds=30.0),
            system_factory=lambda settings: system,
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(system.control_calls, 1)
        self.assertTrue(response.json()["control_loop"]["last_completed_at_iso"])

    def test_status_endpoint_returns_controller_payload(self) -> None:
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(shelly_host="plug.local"),
            system_factory=lambda settings: FakeSystem(),
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            response = client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("decision", payload)
        self.assertIn("plug", payload)
        self.assertIn("control_loop", payload)
        self.assertTrue(payload["plug"]["reachable"])

    def test_override_on_endpoint_accepts_minutes(self) -> None:
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(),
            system_factory=lambda settings: FakeSystem(),
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            response = client.post("/api/override/on", json={"minutes": 45})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["override"]["mode"], "manual_on_until")
        self.assertTrue(payload["override"]["is_active"])

    def test_emergency_off_endpoint_sets_persistent_override(self) -> None:
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(),
            system_factory=lambda settings: FakeSystem(),
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            response = client.post("/api/override/emergency-off")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["override"]["mode"], "emergency_off")
        self.assertTrue(payload["override"]["is_active"])

    def test_frontend_root_serves_built_app_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_dir = Path(temp_dir)
            (frontend_dir / "index.html").write_text("<html><body>dashboard</body></html>", encoding="utf-8")
            (frontend_dir / "assets").mkdir()
            (frontend_dir / "assets" / "app.js").write_text("console.log('dashboard');", encoding="utf-8")

            app = create_app(
                env_file=".env",
                settings_loader=lambda env_file: Settings(),
                system_factory=lambda settings: FakeSystem(),
                plug_client_factory=lambda settings: FakePlugClient(),
                frontend_dist=frontend_dir,
            )

            with TestClient(app) as client:
                root_response = client.get("/")
                asset_response = client.get("/assets/app.js")
                api_missing_response = client.get("/api/missing")

            self.assertEqual(root_response.status_code, 200)
            self.assertIn("dashboard", root_response.text)
            self.assertEqual(asset_response.status_code, 200)
            self.assertIn("console.log", asset_response.text)
            self.assertEqual(api_missing_response.status_code, 404)

    def test_sse_events_endpoint_returns_event_stream(self) -> None:
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(),
            system_factory=lambda settings: FakeSystem(),
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            with client.stream("GET", "/api/events") as response:
                self.assertEqual(response.status_code, 200)
                content_type = response.headers.get("content-type", "")
                self.assertIn("text/event-stream", content_type)

    def test_status_includes_control_interval(self) -> None:
        app = create_app(
            env_file=".env",
            settings_loader=lambda env_file: Settings(control_interval_seconds=60.0),
            system_factory=lambda settings: FakeSystem(),
            plug_client_factory=lambda settings: FakePlugClient(),
        )

        with TestClient(app) as client:
            response = client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("control_loop", payload)
        self.assertEqual(payload["control_loop"]["interval_seconds"], 60.0)


if FASTAPI_AVAILABLE and HTTPX_AVAILABLE:
    class FakeSystem:
        def __init__(self):
            self.control_calls = 0

        async def evaluate(self):
            return (
                FakeDecision(),
                {
                    "decision": FakeDecision().to_dict(),
                    "power": {"battery_soc_percent": 80.0},
                    "weather": {"current_temperature_c": 18.0},
                    "override": {
                        "mode": None,
                        "is_active": False,
                        "effective_target_is_on": True,
                        "reason": None,
                        "until_iso": None,
                        "seen_auto_off": False,
                    },
                    "previous_state": None,
                    "next_state": {"is_on": True},
                },
            )

        async def control(self):
            self.control_calls += 1
            return (
                FakeDecision(),
                {
                    "decision": FakeDecision().to_dict(),
                    "power": {"battery_soc_percent": 80.0},
                    "weather": {"current_temperature_c": 18.0},
                    "override": {
                        "mode": None,
                        "is_active": False,
                        "effective_target_is_on": True,
                        "reason": None,
                        "until_iso": None,
                        "seen_auto_off": False,
                    },
                    "previous_state": None,
                    "next_state": {"is_on": True},
                    "actuation": {"status": "reconciled"},
                },
            )

        async def set_manual_on_override(self, *, duration_minutes: float):
            return {
                "override": {
                    "mode": "manual_on_until",
                    "is_active": True,
                    "effective_target_is_on": True,
                    "reason": None,
                    "until_iso": "2026-01-01T01:00:00+00:00",
                    "seen_auto_off": False,
                },
                "actuation": {"status": "reconciled"},
            }

        async def set_manual_off_override(self, *, duration_minutes: float):
            return {
                "override": {
                    "mode": "manual_off_until",
                    "is_active": True,
                    "effective_target_is_on": False,
                    "reason": None,
                    "until_iso": "2026-01-01T01:00:00+00:00",
                    "seen_auto_off": False,
                },
                "actuation": {"status": "reconciled"},
            }

        async def set_manual_off_until_next_auto_on_override(self):
            return {
                "override": {
                    "mode": "manual_off_until_next_auto_on",
                    "is_active": True,
                    "effective_target_is_on": False,
                    "reason": None,
                    "until_iso": None,
                    "seen_auto_off": False,
                },
                "actuation": {"status": "reconciled"},
            }

        async def set_emergency_off_override(self):
            return {
                "override": {
                    "mode": "emergency_off",
                    "is_active": True,
                    "effective_target_is_on": False,
                    "reason": "Emergency off is active until automatic control is manually restored.",
                    "until_iso": None,
                    "seen_auto_off": False,
                },
                "actuation": {"status": "reconciled"},
            }

        async def clear_override(self):
            return {
                "override": {
                    "mode": None,
                    "is_active": False,
                    "effective_target_is_on": True,
                    "reason": None,
                    "until_iso": None,
                    "seen_auto_off": False,
                },
                "actuation": {"status": "reconciled"},
                "decision": FakeDecision().to_dict(),
            }

        def read_override(self):
            return {
                "mode": None,
                "is_active": False,
                "effective_target_is_on": True,
                "reason": None,
                "until_iso": None,
                "seen_auto_off": False,
            }


    class FakePlugClient:
        async def fetch_switch_status(self):
            return FakeSwitchStatus()


    class FakeDecision:
        def to_dict(self):
            return {
                "should_turn_on": True,
                "action": "turn_on",
                "reason": "Test decision",
                "reasons": ["Test decision"],
                "weather_mode": "heating",
            }


    class FakeSwitchStatus:
        def to_dict(self):
            return {
                "switch_id": 0,
                "output": True,
                "source": "HTTP_in",
                "power_watts": 180.0,
                "voltage_volts": 230.0,
                "current_amps": 0.8,
                "temperature_c": 21.0,
            }


if __name__ == "__main__":
    unittest.main()
