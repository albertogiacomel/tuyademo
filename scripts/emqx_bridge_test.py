#!/usr/bin/env python3
"""emqx_bridge_test.py - Test unitari del bridge MQTT verso EMQX Cloud.

Esecuzione: python scripts/emqx_bridge_test.py
Copre: telemetria, comandi MQTT, fail-soft, credenziali, CLI helper.
Nessuna chiamata di rete: paho non richiesto per l'import del modulo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import emqx_bridge

logger = logging.getLogger(__name__)


class FakeClient:
    """Client MQTT finto che cattura le publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []

    def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> Any:
        self.published.append((topic, payload, qos, retain))
        return mock.Mock(rc=0)


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


SAMPLE_STATUS: dict[str, Any] = {
    "temp_c": 35.9,
    "vdd": 3.29,
    "brightness": 20,
    "mode": "TEMP",
    "r": 0,
    "g": 255,
    "b": 30,
    "wifi_connected": True,
    "wifi_ip": "192.168.1.42",
    "wifi_ssid": "Vodafone-30456263",
    "wifi_rssi": -52,
    "online": True,
    "port": "COM3",
    "leds": {
        "1": {"id": 1, "mode": "BLINK", "period_ms": 500},
        "3": {"id": 3, "mode": "ON", "period_ms": 500},
    },
}


class BuildTelemetryTest(unittest.TestCase):
    def test_whitelist_fields_and_utc_timestamp(self) -> None:
        payload = emqx_bridge.build_telemetry(SAMPLE_STATUS)
        self.assertIn("temp_c", payload)
        self.assertNotIn("port", payload)
        self.assertEqual(payload["leds"]["3"]["mode"], "ON")
        self.assertTrue(payload["ts"].endswith("+00:00"))
        json.dumps(payload)  # serializzabile


class EmqxBridgeTest(unittest.TestCase):
    def test_fail_soft_without_credentials(self) -> None:
        bridge = emqx_bridge.EmqxBridge()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(emqx_bridge, "read_credential", return_value=None),
        ):
            self.assertFalse(bridge.start())
        self.assertFalse(bridge.enabled)
        self.assertIn("credenziali", bridge.last_error or "")

    def test_status_shape_when_idle(self) -> None:
        bridge = emqx_bridge.EmqxBridge()
        status = bridge.status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["telemetry_sent"], 0)
        self.assertIn("r202df21", status["broker"])

    def test_publish_telemetry_noop_without_client(self) -> None:
        bridge = emqx_bridge.EmqxBridge(status_provider=lambda: SAMPLE_STATUS)
        self.assertFalse(bridge.publish_telemetry())
        self.assertEqual(bridge.telemetry_sent, 0)

    def test_on_message_valid_command_publishes_ack(self) -> None:
        bridge = emqx_bridge.EmqxBridge(
            command_handler=lambda payload: {"echo": payload["cmd"]}
        )
        bridge._client = FakeClient()
        bridge._on_message(
            None, None, FakeMessage("tuya/tyde3/command", b'{"cmd":"ping"}')
        )
        topic, payload, _, _ = bridge._client.published[-1]
        self.assertEqual(topic, "tuya/tyde3/ack")
        self.assertTrue(json.loads(payload)["ok"])
        self.assertEqual(bridge.commands_received, 1)

    def test_on_message_invalid_json_reports_error(self) -> None:
        bridge = emqx_bridge.EmqxBridge(command_handler=lambda p: p)
        bridge._client = FakeClient()
        bridge._on_message(None, None, FakeMessage("tuya/tyde3/command", b"non-json"))
        _, payload, _, _ = bridge._client.published[-1]
        self.assertFalse(json.loads(payload)["ok"])

    def test_on_message_handler_exception_isolated(self) -> None:
        def boom(_payload: dict[str, Any]) -> Any:
            raise RuntimeError("board offline")

        bridge = emqx_bridge.EmqxBridge(command_handler=boom)
        bridge._client = FakeClient()
        bridge._on_message(
            None, None, FakeMessage("tuya/tyde3/command", b'{"cmd":"ping"}')
        )
        _, payload, _, _ = bridge._client.published[-1]
        self.assertFalse(json.loads(payload)["ok"])
        self.assertIn("board offline", json.loads(payload)["error"])

    def test_wait_connected_true(self) -> None:
        bridge = emqx_bridge.EmqxBridge()
        bridge._client = mock.Mock(is_connected=mock.Mock(return_value=True))
        self.assertTrue(bridge._wait_connected())

    def test_wait_connected_timeout(self) -> None:
        bridge = emqx_bridge.EmqxBridge(connect_timeout=0.2)
        bridge._client = mock.Mock(is_connected=mock.Mock(return_value=False))
        self.assertFalse(bridge._wait_connected())


class CommandHandlerTest(unittest.TestCase):
    def test_dispatch_to_board(self) -> None:
        board = mock.Mock()
        board.set_rgb.return_value = True
        handler = emqx_bridge.make_command_handler(board)
        self.assertTrue(handler({"cmd": "rgb", "r": 1, "g": 2, "b": 3}))
        board.set_rgb.assert_called_once_with(1, 2, 3)

    def test_wifi_query_routed(self) -> None:
        board = mock.Mock()
        board.query_wifi.return_value = "WIFI:CONNECTED,IP:192.168.1.42"
        handler = emqx_bridge.make_command_handler(board)
        self.assertIn("192.168.1.42", handler({"cmd": "wifi"}))

    def test_unknown_command_raises(self) -> None:
        handler = emqx_bridge.make_command_handler(mock.Mock())
        with self.assertRaises(ValueError):
            handler({"cmd": "kill"})


class CredentialTest(unittest.TestCase):
    def test_env_fallback(self) -> None:
        with (
            mock.patch.object(emqx_bridge, "read_credential", return_value=None),
            mock.patch.dict(
                os.environ,
                {"EMQX_MQTT_USERNAME": "u1", "EMQX_MQTT_PASSWORD": "p1"},
            ),
        ):
            self.assertEqual(emqx_bridge.load_credentials(), ("u1", "p1"))

    def test_credential_manager_first(self) -> None:
        with (
            mock.patch.object(
                emqx_bridge, "read_credential", return_value=("emqx-mqtt", "s3cr3t")
            ),
            mock.patch.dict(
                os.environ, {"EMQX_MQTT_USERNAME": "u1", "EMQX_MQTT_PASSWORD": "p1"}
            ),
        ):
            self.assertEqual(emqx_bridge.load_credentials(), ("emqx-mqtt", "s3cr3t"))

    def test_none_when_absent(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("EMQX_MQTT_USERNAME", "EMQX_MQTT_PASSWORD")
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(emqx_bridge, "read_credential", return_value=None),
        ):
            self.assertIsNone(emqx_bridge.load_credentials())


class DashboardIntegrationTest(unittest.TestCase):
    def test_dashboard_module_imports_with_bridge(self) -> None:
        try:
            import tuya_web
        except ImportError as exc:
            self.skipTest(f"dipendenze dashboard assenti: {exc}")
        self.assertTrue(hasattr(tuya_web, "create_emqx_bridge"))
        self.assertIsNotNone(tuya_web.create_emqx_bridge)
        self.assertIn("wifi_ip", tuya_web.BoardBridge("COM_NULL").status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
