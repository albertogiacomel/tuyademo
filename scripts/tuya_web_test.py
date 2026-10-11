#!/usr/bin/env python3
"""tuya_web_test.py - Unit test suite for tuya_web.py REST API & BoardBridge."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuya_web import BoardBridge, create_app


class TestBoardBridge(unittest.TestCase):
    """Test suite for BoardBridge frame parsing and Wi-Fi state management."""

    def setUp(self) -> None:
        self.bridge = BoardBridge("COM_MOCK", 115200)
        self.bridge._get_serial = MagicMock()

    def test_parse_wifi_frame_connected(self) -> None:
        line = "WIFI:CONNECTED,STATE:4,LED3:SOLID_ON,IP:192.168.1.105,SSID:Vodafone-30456263,RSSI:-45"
        res = self.bridge.parse_wifi_frame(line)
        self.assertTrue(res)
        self.assertTrue(self.bridge.status["wifi_connected"])
        self.assertEqual(self.bridge.status["wifi_ip"], "192.168.1.105")
        self.assertEqual(self.bridge.status["wifi_ssid"], "Vodafone-30456263")
        self.assertEqual(self.bridge.status["wifi_rssi"], -45)
        self.assertEqual(self.bridge.status["leds"]["3"]["mode"], "ON")

    def test_parse_wifi_frame_disconnected(self) -> None:
        line = "WIFI:DISCONNECTED,STATE:1,LED3:BLINK_0_5S"
        res = self.bridge.parse_wifi_frame(line)
        self.assertTrue(res)
        self.assertFalse(self.bridge.status["wifi_connected"])
        self.assertIsNone(self.bridge.status["wifi_ip"])
        self.assertEqual(self.bridge.status["leds"]["3"]["mode"], "BLINK")
        self.assertEqual(self.bridge.status["leds"]["3"]["period_ms"], 500)

    def test_start_pairing_ap(self) -> None:
        self.bridge.send_cmd = MagicMock(return_value="WIFI:OK:PAIRING_AP")  # type: ignore[method-assign]
        res = self.bridge.start_pairing_ap()
        self.assertTrue(res)
        self.assertFalse(self.bridge.status["wifi_connected"])
        self.assertEqual(self.bridge.status["leds"]["3"]["mode"], "BLINK")
        self.assertEqual(self.bridge.status["leds"]["3"]["period_ms"], 500)

    def test_start_pairing_ez(self) -> None:
        self.bridge.send_cmd = MagicMock(return_value="WIFI:OK:PAIRING_EZ")  # type: ignore[method-assign]
        res = self.bridge.start_pairing_ez()
        self.assertTrue(res)
        self.assertFalse(self.bridge.status["wifi_connected"])
        self.assertEqual(self.bridge.status["leds"]["3"]["mode"], "BLINK")
        self.assertEqual(self.bridge.status["leds"]["3"]["period_ms"], 250)


class TestFlaskEndpoints(unittest.TestCase):
    """Test suite for Flask API routes."""

    def setUp(self) -> None:
        self.bridge = BoardBridge("COM_MOCK", 115200)
        self.bridge.ping = MagicMock(return_value=True)  # type: ignore[method-assign]
        self.bridge.read_temp = MagicMock(  # type: ignore[method-assign]
            return_value={"ok": True, "temp_c": 35.0, "vdd": 3.3}
        )
        self.bridge.start_pairing_ap = MagicMock(return_value=True)  # type: ignore[method-assign]
        self.bridge.start_pairing_ez = MagicMock(return_value=True)  # type: ignore[method-assign]
        self.app = create_app(self.bridge)
        self.client = self.app.test_client()

    def test_api_ping(self) -> None:
        resp = self.client.get("/api/ping")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])

    def test_api_wifi_pair_ap(self) -> None:
        resp = self.client.post("/api/wifi/pair/ap")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "AP")
        self.assertEqual(data["led3"], "BLINK_0_5S")

    def test_api_wifi_pair_ez(self) -> None:
        resp = self.client.post("/api/wifi/pair/ez")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "EZ")
        self.assertEqual(data["led3"], "BLINK_0_25S")


if __name__ == "__main__":
    unittest.main()
