from __future__ import annotations

import unittest

from vrm_solar_automation.config import Settings
from vrm_solar_automation.runtime import detect_runtime_support, ensure_supported_runtime


class RuntimeSupportTests(unittest.TestCase):
    def test_linux_with_mqtt_enabled_is_supported(self) -> None:
        support = detect_runtime_support(
            Settings(cerbo_mqtt_enabled=True),
            os_name="posix",
            platform_system="Linux",
            platform_release="6.12.0",
        )

        self.assertTrue(support.mqtt_requested)
        self.assertTrue(support.mqtt_supported)
        self.assertFalse(support.is_native_windows)
        self.assertIsNone(support.reason)

    def test_windows_with_mqtt_enabled_is_not_supported(self) -> None:
        support = detect_runtime_support(
            Settings(cerbo_mqtt_enabled=True),
            os_name="nt",
            platform_system="Windows",
            platform_release="11",
        )

        self.assertTrue(support.mqtt_requested)
        self.assertFalse(support.mqtt_supported)
        self.assertTrue(support.is_native_windows)
        self.assertIn("CERBO_MQTT_ENABLED=false", support.reason or "")

    def test_windows_with_mqtt_disabled_is_supported(self) -> None:
        support = detect_runtime_support(
            Settings(cerbo_mqtt_enabled=False),
            os_name="nt",
            platform_system="Windows",
            platform_release="11",
        )

        self.assertFalse(support.mqtt_requested)
        self.assertTrue(support.mqtt_supported)
        self.assertTrue(support.is_native_windows)

    def test_ensure_supported_runtime_raises_on_unsupported_mqtt_runtime(self) -> None:
        with self.assertRaises(RuntimeError):
            ensure_supported_runtime(
                Settings(cerbo_mqtt_enabled=True),
                detect_runtime_support(
                    Settings(cerbo_mqtt_enabled=True),
                    os_name="nt",
                    platform_system="Windows",
                    platform_release="11",
                ),
            )


if __name__ == "__main__":
    unittest.main()
