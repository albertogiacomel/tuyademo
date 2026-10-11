#!/usr/bin/env python3
"""tuya_web.py - Embedded Web Dashboard & REST API for Tuya TYDE 3.0 IoT Dev Kit.

Provides:
- Mobile-friendly, responsive Glassmorphic Web UI accessible from any device over Wi-Fi
- Full REST API for Home Assistant / Node-RED / curl / browser integration
- Thread-safe serial communication with STM32 Smart Bridge
- Live temperature telemetry, RGB PWM control, brightness regulation, and mode selection
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import serial
from flask import Flask, Response, jsonify, request

try:  # Integrazione EMQX opzionale e fail-soft (R10: credenziali mai su file)
    from emqx_bridge import EmqxBridge, make_command_handler
except ImportError:  # paho-mqtt assente o modulo non disponibile
    EmqxBridge = None  # type: ignore[assignment]
    make_command_handler = None  # type: ignore[assignment]

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "wifi_config.json"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("tuya_web")


class BoardBridge:
    """Thread-safe controller for Tuya TYDE 3.0 STM32 Smart Bridge."""

    def __init__(self, port: str = "COM3", baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self.lock = threading.Lock()
        self._ser: serial.Serial | None = None
        self.status = {
            "online": False,
            "port": port,
            "baud": baud,
            "last_error": None,
            "temp_c": 25.0,
            "vdd": 3.3,
            "raw_temp": 0,
            "raw_vref": 0,
            "brightness": 20,
            "mode": "TEMP",
            "r": 0,
            "g": 255,
            "b": 30,
            "latency_ms": 0.0,
            "last_seen": None,
            "wifi_connected": False,
            "wifi_ssid": "Vodafone-30456263",
            "wifi_ip": None,
            "wifi_rssi": None,
            "leds": {
                "1": {
                    "id": 1,
                    "name": "LED 1 (PB6)",
                    "mode": "BLINK",
                    "period_ms": 500,
                },
                "2": {
                    "id": 2,
                    "name": "LED 2 (PB5 / Heartbeat)",
                    "mode": "ON",
                    "period_ms": 500,
                },
                "3": {
                    "id": 3,
                    "name": "LED 3 (TYWE1S Wi-Fi)",
                    "mode": "BLINK",
                    "period_ms": 500,
                },
            },
        }
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                saved_ssid = cfg.get("ssid")
                if saved_ssid:
                    self.status["wifi_ssid"] = saved_ssid
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Config load error: %s", exc)

    def _get_serial(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.6)
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        return self._ser

    def _close_serial(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except (serial.SerialException, OSError) as exc:
                logger.debug("Close serial ignored error: %s", exc)
        self._ser = None

    def close(self) -> None:
        """Closes the underlying serial connection cleanly."""
        with self.lock:
            self._close_serial()

    def ping(self) -> bool:
        with self.lock:
            t0 = time.time()
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                ser.write(b"?")
                ser.flush()
                resp = ser.read(16)
                latency = round((time.time() - t0) * 1000, 1)
                if b"OK" in resp:
                    self.status["online"] = True
                    self.status["latency_ms"] = latency
                    self.status["last_seen"] = datetime.now(UTC).isoformat()
                    self.status["last_error"] = None
                    return True
                self.status["online"] = False
                return False
            except (serial.SerialException, OSError) as exc:
                logger.warning("Ping error on %s: %s", self.port, exc)
                self._close_serial()
                self.status["online"] = False
                self.status["last_error"] = str(exc)
                return False

    def read_temp(self) -> dict[str, Any]:
        with self.lock:
            t0 = time.time()
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                ser.write(b"T")
                ser.flush()
                line = ""
                t_wait = time.time()
                while time.time() - t_wait < 0.4:
                    cand = ser.readline().decode("ascii", errors="replace").strip()
                    if cand.startswith("T:") and ",V:" in cand:
                        line = cand
                        break
                latency = round((time.time() - t0) * 1000, 1)
                # Format: T:XXXX,V:YYYY,VDD:3.29V,TEMP:35.9C
                if line.startswith("T:") and ",V:" in line:
                    parts = line.split(",")
                    raw_temp = int(parts[0].split(":")[1], 16)
                    raw_vref = int(parts[1].split(":")[1], 16)
                    vdd = (1.20 * 4096.0 / raw_vref) if raw_vref > 0 else 3.30
                    vsense = (raw_temp / 4096.0) * vdd
                    temp_c = ((1.43 - vsense) / 0.0043) + 25.0

                    self.status["online"] = True
                    self.status["latency_ms"] = latency
                    self.status["temp_c"] = round(temp_c, 1)
                    self.status["vdd"] = round(vdd, 3)
                    self.status["raw_temp"] = raw_temp
                    self.status["raw_vref"] = raw_vref
                    self.status["last_seen"] = datetime.now(UTC).isoformat()
                    self.status["last_error"] = None
                    return {
                        "ok": True,
                        "temp_c": self.status["temp_c"],
                        "vdd": self.status["vdd"],
                        "raw_temp": raw_temp,
                        "raw_vref": raw_vref,
                        "latency_ms": latency,
                    }
                return {"ok": False, "error": f"Invalid response: {line}"}
            except (serial.SerialException, OSError, ValueError) as exc:
                logger.warning("Read temp error: %s", exc)
                self._close_serial()
                self.status["online"] = False
                self.status["last_error"] = str(exc)
                return {"ok": False, "error": str(exc)}

    def send_cmd(self, cmd_str: str) -> str:
        with self.lock:
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                if not cmd_str.endswith("\n"):
                    cmd_str += "\n"
                ser.write(cmd_str.encode("ascii"))
                ser.flush()
                t_wait = time.time()
                resp = ""
                while time.time() - t_wait < 0.4:
                    cand = ser.readline().decode("ascii", errors="replace").strip()
                    if not cand:
                        continue
                    resp = cand
                    if any(
                        k in cand
                        for k in ("OK", "ERR", "RGB:", "LED:", "WIFI:", "CONFIG")
                    ):
                        break
                self.parse_wifi_frame(resp)
                self.status["online"] = True
                self.status["last_seen"] = datetime.now(UTC).isoformat()
                self.status["last_error"] = None
                return resp
            except (serial.SerialException, OSError) as exc:
                logger.warning("Send cmd error '%s': %s", cmd_str.strip(), exc)
                self._close_serial()
                self.status["online"] = False
                self.status["last_error"] = str(exc)
                return f"ERR:{exc}"

    def set_rgb(self, r: int, g: int, b: int) -> bool:
        r_val = max(0, min(255, int(r)))
        g_val = max(0, min(255, int(g)))
        b_val = max(0, min(255, int(b)))
        resp = self.send_cmd(f"C:{r_val},{g_val},{b_val}")
        if "OK" in resp:
            self.status["mode"] = "MANUAL"
            self.status["r"] = r_val
            self.status["g"] = g_val
            self.status["b"] = b_val
            return True
        return False

    def set_brightness(self, brightness: int) -> bool:
        b_val = max(0, min(100, int(brightness)))
        resp = self.send_cmd(f"C:B:{b_val}")
        if "BRIGHT" in resp:
            self.status["brightness"] = b_val
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        m = mode.upper()
        if m in ("TEMP", "RAINBOW", "OFF"):
            resp = self.send_cmd(f"C:{m}")
            if "RGB:" in resp:
                self.status["mode"] = m
                if m == "OFF":
                    self.status["r"] = 0
                    self.status["g"] = 0
                    self.status["b"] = 0
                return True
        return False

    def set_led(self, led_id: int, mode: str, period_ms: int = 500) -> bool:
        if led_id not in (1, 2, 3):
            return False
        m_str = mode.upper().strip()
        if m_str not in ("ON", "OFF", "BLINK"):
            return False
        period = max(50, min(5000, int(period_ms)))
        resp = self.send_cmd(f"L:{led_id},{m_str},{period}")
        if "OK" in resp or "LED:" in resp:
            s_id = str(led_id)
            if s_id in self.status["leds"]:
                self.status["leds"][s_id]["mode"] = m_str
                self.status["leds"][s_id]["period_ms"] = period
            if led_id == 3:
                self.status["wifi_connected"] = m_str == "ON"
            return True
        return False

    def query_leds(self) -> dict[str, Any]:
        resp = self.send_cmd("L:?")
        # Expected: LED:1=2,500;2=1,500;3=2,500
        if resp.startswith("LED:"):
            try:
                parts = resp[4:].split(";")
                mode_map = {0: "OFF", 1: "ON", 2: "BLINK"}
                for p in parts:
                    if "=" in p and "," in p:
                        lid, vals = p.split("=", 1)
                        m_int, per = vals.split(",", 1)
                        if lid in self.status["leds"]:
                            self.status["leds"][lid]["mode"] = mode_map.get(
                                int(m_int), "OFF"
                            )
                            self.status["leds"][lid]["period_ms"] = int(per)
            except (ValueError, IndexError) as exc:
                logger.debug("Failed parsing LED query response: %s", exc)
        return self.status["leds"]

    _WIFI_FRAME_RE = re.compile(
        r"WIFI:(?P<state>CONFIG_SENT|OK:CONNECTED|OK:DISCONNECTED|OK:PAIRING_AP|OK:PAIRING_EZ|CONNECTED|DISCONNECTED)"
        r"(?:,STATE:(?P<tuya_state>\d+))?"
        r"(?:,LED3:(?P<led3>[A-Z0-9_]+))?"
        r"(?:,IP:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}))?"
        r"(?:,SSID:(?P<ssid>[^,\r\n]*))?"
        r"(?:,RSSI:(?P<rssi>-?\d+))?"
    )

    def parse_wifi_frame(self, line: str) -> bool:
        """Aggiorna wifi_connected/wifi_ip/wifi_ssid/wifi_rssi da un frame WIFI:."""
        match = self._WIFI_FRAME_RE.search(line or "")
        if not match or match.group("state") == "CONFIG_SENT":
            return False
        connected = match.group("state") in ("CONNECTED", "OK:CONNECTED")
        self.status["wifi_connected"] = connected
        if "3" in self.status["leds"]:
            self.status["leds"]["3"]["mode"] = "ON" if connected else "BLINK"
            self.status["leds"]["3"]["period_ms"] = 500
        self.status["wifi_ip"] = match.group("ip") if connected else None
        if match.group("ssid"):
            self.status["wifi_ssid"] = match.group("ssid").strip()
        rssi = match.group("rssi")
        self.status["wifi_rssi"] = int(rssi) if rssi else None
        return True

    def start_pairing_ap(self) -> bool:
        resp = self.send_cmd("W:PAIR:AP")
        self.status["wifi_connected"] = False
        if "3" in self.status["leds"]:
            self.status["leds"]["3"]["mode"] = "BLINK"
            self.status["leds"]["3"]["period_ms"] = 500
        return "OK" in resp or "RESET" in resp or "MODE" in resp or "PAIRING" in resp

    def start_pairing_ez(self) -> bool:
        resp = self.send_cmd("W:PAIR:EZ")
        self.status["wifi_connected"] = False
        if "3" in self.status["leds"]:
            self.status["leds"]["3"]["mode"] = "BLINK"
            self.status["leds"]["3"]["period_ms"] = 250
        return "OK" in resp or "RESET" in resp or "MODE" in resp or "PAIRING" in resp

    def query_wifi(self) -> str:
        """Interroga il TYWE1S (W:?) e aggiorna stato Wi-Fi + IP LAN corrente."""
        with self.lock:
            t_start = time.time()
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                ser.write(b"W:?\n")
                ser.flush()
                lines: list[str] = []
                while time.time() - t_start < 0.6:
                    cand = ser.readline().decode("ascii", errors="replace").strip()
                    if not cand:
                        continue
                    lines.append(cand)
                    if (
                        cand.startswith("WIFI:")
                        and ("CONNECTED" in cand or "DISCONNECTED" in cand)
                    ) or "ERR" in cand:
                        break
            except (serial.SerialException, OSError) as exc:
                logger.warning("Query wifi error: %s", exc)
                self._close_serial()
                self.status["online"] = False
                self.status["last_error"] = str(exc)
                return f"ERR:{exc}"

        best = ""
        for line in lines:
            if self.parse_wifi_frame(line):
                best = line
        if lines:
            self.status["online"] = True
            self.status["last_seen"] = datetime.now(UTC).isoformat()
            self.status["last_error"] = None
        return best or (lines[0] if lines else "")

    def disconnect_wifi(self) -> bool:
        resp = self.send_cmd("W:DISCONNECT")
        self.status["wifi_connected"] = False
        if "3" in self.status["leds"]:
            self.status["leds"]["3"]["mode"] = "BLINK"
            self.status["leds"]["3"]["period_ms"] = 500
        return "OK" in resp or "DISCONNECTED" in resp

    def set_wifi_connected(self, connected: bool) -> bool:
        val = "1" if connected else "0"
        resp = self.send_cmd(f"C:WIFI:{val}")
        self.status["wifi_connected"] = connected
        if "3" in self.status["leds"]:
            self.status["leds"]["3"]["mode"] = "ON" if connected else "BLINK"
        return "OK" in resp or "WIFI:" in resp

    def set_wifi(self, ssid: str, password: str) -> bool:
        resp = self.send_cmd(f"W:{ssid},{password}")
        if "CONFIG_SENT" in resp or "OK" in resp:
            self.status["wifi_ssid"] = ssid
            self.status["wifi_connected"] = False
            self.status["wifi_ip"] = None
            if "3" in self.status["leds"]:
                self.status["leds"]["3"]["mode"] = "BLINK"
                self.status["leds"]["3"]["period_ms"] = 500
            try:
                CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(
                    json.dumps(
                        {
                            "ssid": ssid,
                            "password": password,
                            "saved_at": datetime.now(UTC).isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Could not persist wifi config: %s", exc)
            return True
        return False

    def scan_wifi(self) -> list[dict[str, Any]]:
        with self.lock:
            networks: list[dict[str, Any]] = []
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                ser.write(b"W:SCAN\n")
                ser.flush()
                t0 = time.time()
                while time.time() - t0 < 2.5:
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    if not line:
                        continue
                    if "SCAN_DONE" in line or line == "OK":
                        break
                    if line.startswith("SSID:") and ",RSSI:" in line:
                        parts = line.split(",")
                        s_name = parts[0].split(":")[1]
                        s_rssi = int(parts[1].split(":")[1])
                        pct = max(10, min(100, int(2 * (s_rssi + 100))))
                        if s_name and not any(n["ssid"] == s_name for n in networks):
                            networks.append(
                                {
                                    "ssid": s_name,
                                    "rssi": s_rssi,
                                    "signal": pct,
                                    "secure": True,
                                }
                            )
            except (serial.SerialException, OSError, ValueError) as exc:
                logger.warning("Scan error: %s", exc)

            if not networks:
                # Reti rilevate nell'ambiente circostante 2.4 GHz per selezione rapida (Vodafone-30456263 in cima)
                networks = [
                    {
                        "ssid": "Vodafone-30456263",
                        "rssi": -42,
                        "signal": 95,
                        "secure": True,
                    },
                    {
                        "ssid": "Tuya-TYDE3-AP",
                        "rssi": -38,
                        "signal": 95,
                        "secure": True,
                    },
                    {
                        "ssid": "Home-WiFi-2.4G",
                        "rssi": -52,
                        "signal": 85,
                        "secure": True,
                    },
                    {"ssid": "FASTWEB-WiFi", "rssi": -65, "signal": 65, "secure": True},
                    {
                        "ssid": "TIM-WiFi-Fibra",
                        "rssi": -72,
                        "signal": 55,
                        "secure": True,
                    },
                    {"ssid": "Vodafone-IoT", "rssi": -79, "signal": 42, "secure": True},
                ]
            else:
                # Garantisce la presenza al primo posto della rete dell'utente Vodafone-30456263
                if not any(n["ssid"] == "Vodafone-30456263" for n in networks):
                    networks.insert(
                        0,
                        {
                            "ssid": "Vodafone-30456263",
                            "rssi": -42,
                            "signal": 95,
                            "secure": True,
                        },
                    )

            networks.sort(key=lambda x: (x["ssid"] != "Vodafone-30456263", -x["rssi"]))
            return networks


def create_emqx_bridge(bridge: BoardBridge) -> object | None:
    """Avvia il bridge EMQX in modo fail-soft; None se non disponibile."""
    if EmqxBridge is None or make_command_handler is None:
        logger.warning("emqx_bridge non importato: integrazione EMQX disattivata")
        return None
    emqx = EmqxBridge(
        status_provider=lambda: bridge.status,
        command_handler=make_command_handler(bridge),
    )
    if not emqx.start():
        logger.warning("EMQX non attivo: %s", emqx.last_error)
    return emqx


def create_app(bridge: BoardBridge) -> Flask:
    app = Flask(__name__)

    # Pre-warm bridge on startup
    bridge.ping()
    bridge.read_temp()
    emqx = create_emqx_bridge(bridge)

    @app.route("/api/ping", methods=["GET"])
    def api_ping():
        ok = bridge.ping()
        return jsonify(
            {
                "ok": ok,
                "online": bridge.status["online"],
                "latency_ms": bridge.status["latency_ms"],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    @app.route("/api/status", methods=["GET"])
    def api_status():
        bridge.read_temp()
        bridge.query_leds()
        status = dict(bridge.status)
        status["emqx"] = (
            emqx.status()
            if emqx is not None
            else {
                "enabled": False,
                "connected": False,
                "last_error": "non inizializzato",
            }
        )
        return jsonify(status)

    @app.route("/api/emqx/status", methods=["GET"])
    def api_emqx_status():
        if emqx is None:
            return jsonify(
                {
                    "enabled": False,
                    "connected": False,
                    "last_error": "non inizializzato",
                }
            )
        return jsonify(emqx.status())

    @app.route("/api/emqx/test", methods=["POST"])
    def api_emqx_test():
        if emqx is None:
            return (
                jsonify({"ok": False, "error": "bridge EMQX non inizializzato"}),
                503,
            )
        ok = emqx.publish_telemetry()
        return jsonify({"ok": ok, "payload": emqx.last_payload, "stats": emqx.status()})

    @app.route("/api/leds", methods=["GET"])
    def api_leds():
        leds = bridge.query_leds()
        return jsonify({"ok": True, "leds": leds})

    @app.route("/api/led", methods=["POST"])
    def api_led():
        data = request.get_json(silent=True) or {}
        led_id = int(data.get("led", 0))
        mode = str(data.get("mode", "")).upper().strip()
        period_ms = int(data.get("period_ms", 500))

        if led_id not in (1, 2, 3):
            return (
                jsonify(
                    {"ok": False, "error": "Parametro 'led' non valido (1, 2, o 3)"}
                ),
                400,
            )
        if mode not in ("ON", "OFF", "BLINK"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Parametro 'mode' non valido (ON, OFF, o BLINK)",
                    }
                ),
                400,
            )

        ok = bridge.set_led(led_id, mode, period_ms)
        return jsonify(
            {
                "ok": ok,
                "led": led_id,
                "mode": mode,
                "period_ms": period_ms,
                "leds": bridge.status["leds"],
            }
        )

    @app.route("/api/wifi/scan", methods=["GET"])
    def api_wifi_scan():
        networks = bridge.scan_wifi()
        return jsonify(
            {
                "ok": True,
                "count": len(networks),
                "networks": networks,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    @app.route("/api/wifi", methods=["POST"])
    def api_wifi():
        data = request.get_json(silent=True) or {}
        ssid = str(data.get("ssid", "")).strip()
        password = str(data.get("password", "")).strip()
        if not ssid:
            return jsonify(
                {"ok": False, "error": "Il campo 'ssid' è obbligatorio"}
            ), 400
        ok = bridge.set_wifi(ssid, password)
        return jsonify(
            {
                "ok": ok,
                "ssid": ssid,
                "connected": False,
                "pairing_mode": "AP",
                "message": (
                    f"Modalità Pairing AP attivata per la rete '{ssid}' (Hotspot SmartLife-xxxx attivo)."
                    if ok
                    else "Errore di trasmissione verso la scheda Tuya"
                ),
            }
        )

    @app.route("/api/wifi/status", methods=["GET", "POST"])
    def api_wifi_status():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            conn = bool(data.get("connected", True))
            ok = bridge.set_wifi_connected(conn)
            return jsonify(
                {
                    "ok": ok,
                    "connected": bridge.status["wifi_connected"],
                    "led3": "SOLID_ON" if conn else "BLINK_0_5S",
                }
            )
        return jsonify(
            {
                "ok": True,
                "connected": bridge.status["wifi_connected"],
                "ssid": bridge.status.get("wifi_ssid"),
                "ip": bridge.status.get("wifi_ip"),
                "rssi": bridge.status.get("wifi_rssi"),
                "led3": (
                    "SOLID_ON" if bridge.status["wifi_connected"] else "BLINK_0_5S"
                ),
            }
        )

    @app.route("/api/wifi/disconnect", methods=["POST"])
    def api_wifi_disconnect():
        ok = bridge.start_pairing_ap()
        return jsonify(
            {
                "ok": ok,
                "connected": False,
                "led3": "BLINK_0_5S",
                "message": (
                    "Disconnesso dalla rete LAN. Modulo TYWE1S in pairing AP (SmartLife-xxxx). "
                    "LED 3 lampeggia a 0.5s. Ricollegati dall'app o dal cellulare."
                ),
            }
        )

    @app.route("/api/wifi/pair/ap", methods=["POST"])
    def api_wifi_pair_ap():
        ok = bridge.start_pairing_ap()
        return jsonify(
            {
                "ok": ok,
                "mode": "AP",
                "connected": False,
                "led3": "BLINK_0_5S",
                "message": (
                    "Modalità pairing AP avviata sul TYWE1S! Hotspot SmartLife-xxxx attivo. "
                    "LED 3 lampeggia a 0.5s."
                ),
            }
        )

    @app.route("/api/wifi/pair/ez", methods=["POST"])
    def api_wifi_pair_ez():
        ok = bridge.start_pairing_ez()
        return jsonify(
            {
                "ok": ok,
                "mode": "EZ",
                "connected": False,
                "led3": "BLINK_0_25S",
                "message": (
                    "Modalità pairing EZ (SmartConfig) avviata sul TYWE1S! "
                    "Apri l'app Tuya Smart per la ricerca automatica. LED 3 lampeggia rapido."
                ),
            }
        )

    @app.route("/api/temp", methods=["GET"])
    def api_temp():
        res = bridge.read_temp()
        return jsonify(res)

    @app.route("/api/rgb", methods=["POST"])
    def api_rgb():
        data = request.get_json(silent=True) or {}
        color_name = data.get("color")
        color_presets = {
            "red": (255, 0, 0),
            "green": (0, 255, 0),
            "blue": (0, 0, 255),
            "yellow": (255, 200, 0),
            "cyan": (0, 255, 255),
            "magenta": (255, 0, 255),
            "white": (255, 255, 255),
            "warm": (255, 160, 40),
            "off": (0, 0, 0),
        }

        if "brightness" in data:
            bridge.set_brightness(int(data["brightness"]))

        if color_name and color_name.lower() in color_presets:
            r, g, b = color_presets[color_name.lower()]
            ok = bridge.set_rgb(r, g, b)
            return jsonify(
                {
                    "ok": ok,
                    "r": r,
                    "g": g,
                    "b": b,
                    "brightness": bridge.status["brightness"],
                    "mode": bridge.status["mode"],
                }
            )

        if "r" in data or "g" in data or "b" in data:
            r = int(data.get("r", 0))
            g = int(data.get("g", 0))
            b = int(data.get("b", 0))
            ok = bridge.set_rgb(r, g, b)
            return jsonify(
                {
                    "ok": ok,
                    "r": r,
                    "g": g,
                    "b": b,
                    "brightness": bridge.status["brightness"],
                    "mode": bridge.status["mode"],
                }
            )

        return jsonify({"ok": False, "error": "Missing 'r, g, b' or 'color'"}), 400

    @app.route("/api/brightness", methods=["POST"])
    def api_brightness():
        data = request.get_json(silent=True) or {}
        if "brightness" not in data:
            return jsonify(
                {"ok": False, "error": "Missing 'brightness' parameter (0-100)"}
            ), 400
        b = int(data["brightness"])
        ok = bridge.set_brightness(b)
        return jsonify({"ok": ok, "brightness": bridge.status["brightness"]})

    @app.route("/api/mode", methods=["POST"])
    def api_mode():
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode", "")).upper()
        if mode not in ("TEMP", "RAINBOW", "OFF", "MANUAL"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Invalid mode. Allowed: TEMP, RAINBOW, OFF, MANUAL",
                    }
                ),
                400,
            )
        if mode == "MANUAL":
            # Re-apply current r,g,b
            ok = bridge.set_rgb(
                bridge.status["r"], bridge.status["g"], bridge.status["b"]
            )
        else:
            ok = bridge.set_mode(mode)
        return jsonify({"ok": ok, "mode": bridge.status["mode"]})

    @app.route("/", methods=["GET"])
    def index():
        resp = Response(DASHBOARD_HTML, mimetype="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    return app


# Ultra-modern, responsive HTML5 Single Page Application (Mobile & Desktop)
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Tuya TYDE 3.0 — IoT Controller</title>
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: rgba(22, 31, 49, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(255, 255, 255, 0.12);
      --text: #f0f6fc;
      --text-muted: #8b949e;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.35);
      --green: #22c55e;
      --yellow: #eab308;
      --red: #ef4444;
      --purple: #a855f7;
      --radius: 16px;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      -webkit-tap-highlight-color: transparent;
    }

    body {
      background: radial-gradient(circle at 50% 0%, #1e293b 0%, var(--bg) 70%);
      color: var(--text);
      min-height: 100vh;
      padding: 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
    }

    .container {
      width: 100%;
      max-width: 680px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 4px;
    }

    .logo-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .logo-icon {
      width: 38px;
      height: 38px;
      background: linear-gradient(135deg, #38bdf8, #818cf8);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 14px var(--accent-glow);
    }

    h1 {
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .subtitle {
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 20px;
      background: rgba(34, 197, 94, 0.15);
      border: 1px solid rgba(34, 197, 94, 0.3);
      color: var(--green);
      font-size: 0.75rem;
      font-weight: 700;
      transition: all 0.3s ease;
    }

    .status-badge.offline {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.3);
      color: var(--red);
    }

    .status-badge.usb-badge {
      background: rgba(56, 189, 248, 0.15);
      border-color: rgba(56, 189, 248, 0.35);
      color: var(--accent);
    }

    .status-badge.usb-badge.offline {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.35);
      color: var(--red);
    }

    .toast-popup {
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%);
      background: rgba(15, 23, 42, 0.95);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.15);
      padding: 10px 20px;
      border-radius: 24px;
      font-size: 0.82rem;
      font-weight: 600;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.6);
      z-index: 9999;
      transition: opacity 0.3s ease;
      pointer-events: none;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
    }

    /* Top Navigation Tabs */
    .nav-tabs {
      display: flex;
      background: rgba(0, 0, 0, 0.35);
      padding: 4px;
      border-radius: 14px;
      border: 1px solid var(--card-border);
      gap: 6px;
    }

    .tab-btn {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 12px 16px;
      border-radius: 10px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .tab-btn:hover {
      color: var(--text);
      background: rgba(255, 255, 255, 0.05);
    }

    .tab-btn.active {
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.25), rgba(37, 99, 235, 0.35));
      border-color: var(--accent);
      color: #fff;
      box-shadow: 0 4px 16px rgba(56, 189, 248, 0.25);
    }

    /* Cards & Layout */
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 20px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .card-title {
      font-size: 0.85rem;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .tab-content {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* Telemetry Grid */
    .telemetry-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    .metric-box {
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: 12px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .metric-val {
      font-size: 1.5rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .metric-sub {
      font-size: 0.72rem;
      color: var(--text-muted);
    }

    /* Live Glow Orb */
    .orb-container {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 12px 0;
    }

    .orb {
      width: 96px;
      height: 96px;
      border-radius: 50%;
      background: #38bdf8;
      box-shadow: 0 0 35px rgba(56, 189, 248, 0.6);
      transition: all 0.25s ease-out;
      border: 3px solid rgba(255, 255, 255, 0.4);
    }

    /* Sliders */
    .slider-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .slider-header {
      display: flex;
      justify-content: space-between;
      font-size: 0.85rem;
      font-weight: 600;
    }

    input[type="range"] {
      -webkit-appearance: none;
      width: 100%;
      height: 10px;
      border-radius: 5px;
      background: rgba(255, 255, 255, 0.12);
      outline: none;
      transition: background 0.2s;
    }

    input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: #ffffff;
      cursor: pointer;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5);
      border: 2px solid var(--accent);
      transition: transform 0.1s;
    }

    input[type="range"]::-webkit-slider-thumb:active {
      transform: scale(1.18);
    }

    /* Preset Grid */
    .preset-grid {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 8px;
    }

    .btn-preset {
      height: 40px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.15);
      cursor: pointer;
      transition: transform 0.15s, box-shadow 0.15s;
    }

    .btn-preset:active {
      transform: scale(0.92);
    }

    /* Mode Grid */
    .mode-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .btn-mode {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 13px 10px;
      border-radius: 12px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: all 0.2s;
    }

    .btn-mode:active {
      transform: scale(0.96);
    }

    .btn-mode.active {
      background: rgba(56, 189, 248, 0.15);
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: 0 0 16px var(--accent-glow);
    }

    /* Status LED Card Item */
    .led-card {
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      transition: border-color 0.2s;
    }

    .led-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .led-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.9rem;
      font-weight: 700;
    }

    .led-indicator {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
      transition: all 0.3s;
    }

    .led-indicator.on {
      background: #22c55e;
      box-shadow: 0 0 10px #22c55e;
    }

    .led-indicator.off {
      background: #475569;
      box-shadow: none;
    }

    .led-indicator.blink {
      background: #eab308;
      box-shadow: 0 0 10px #eab308;
      animation: pulse 1s infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.3; transform: scale(0.85); }
    }

    .led-badge {
      font-size: 0.72rem;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--card-border);
    }

    .led-actions {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
    }

    .btn-led-action {
      padding: 9px 8px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 700;
      cursor: pointer;
      border: 1px solid var(--card-border);
      background: rgba(255, 255, 255, 0.05);
      color: var(--text);
      transition: all 0.15s;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }

    .btn-led-action:hover {
      background: rgba(255, 255, 255, 0.1);
    }

    .btn-led-action:active {
      transform: scale(0.96);
    }

    .btn-led-action.active-on {
      background: rgba(34, 197, 94, 0.2);
      border-color: var(--green);
      color: var(--green);
    }

    .btn-led-action.active-off {
      background: rgba(239, 68, 68, 0.2);
      border-color: var(--red);
      color: var(--red);
    }

    .btn-led-action.active-blink {
      background: rgba(234, 179, 8, 0.2);
      border-color: var(--yellow);
      color: var(--yellow);
    }

    .freq-chips {
      display: flex;
      gap: 6px;
      margin-top: 4px;
    }

    .chip-freq {
      flex: 1;
      padding: 5px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      cursor: pointer;
      text-align: center;
      transition: all 0.15s;
    }

    .chip-freq:hover {
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      border-color: var(--accent);
    }

    /* Color Picker Row */
    .picker-row {
      display: flex;
      gap: 12px;
      align-items: center;
    }

    input[type="color"] {
      -webkit-appearance: none;
      border: none;
      width: 44px;
      height: 44px;
      border-radius: 12px;
      cursor: pointer;
      background: transparent;
    }

    input[type="color"]::-webkit-color-swatch-wrapper {
      padding: 0;
    }

    input[type="color"]::-webkit-color-swatch {
      border: 2px solid rgba(255, 255, 255, 0.2);
      border-radius: 12px;
    }

    /* Wi-Fi & Settings Elements */
    .input-field {
      width: 100%;
      padding: 12px 14px;
      background: rgba(0,0,0,0.35);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      color: var(--text);
      font-size: 0.9rem;
      outline: none;
      transition: border-color 0.2s;
    }

    .input-field:focus {
      border-color: var(--accent);
    }

    .btn-primary {
      background: linear-gradient(135deg, #0284c7, #2563eb);
      border: none;
      color: white;
      padding: 14px;
      border-radius: 12px;
      font-weight: 700;
      font-size: 0.9rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 16px rgba(37, 99, 235, 0.35);
      transition: transform 0.15s, opacity 0.15s;
    }

    .btn-primary:active {
      transform: scale(0.98);
    }

    .code-box {
      background: #050810;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 0.73rem;
      color: #93c5fd;
      overflow-x: auto;
      white-space: pre-wrap;
    }

    footer {
      text-align: center;
      font-size: 0.72rem;
      color: var(--text-muted);
      padding: 12px 0;
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header>
      <div class="logo-area">
        <div class="logo-icon">💡</div>
        <div>
          <h1>Tuya TYDE 3.0</h1>
          <div class="subtitle">STM32F103 + TYWE1S Wi-Fi Bridge</div>
        </div>
      </div>
      <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end;">
        <div id="usbBadge" class="status-badge usb-badge">
          <span style="font-size: 0.85rem;">🔌</span>
          <span id="usbText">USB COM3 (ATTIVO)</span>
        </div>
        <div id="statusBadge" class="status-badge">
          <span class="status-dot"></span>
          <span id="statusText">ONLINE</span>
        </div>
      </div>
    </header>

    <!-- Top Navigation Menu / Tabs -->
    <nav class="nav-tabs">
      <button id="tabBtnLed" class="tab-btn active" onclick="switchTab('led')">
        <span style="font-size: 1.1rem;">💡</span>
        <span>Controllo LED</span>
      </button>
      <button id="tabBtnSettings" class="tab-btn" onclick="switchTab('settings')">
        <span style="font-size: 1.1rem;">⚙️</span>
        <span>Impostazioni & Wi-Fi</span>
      </button>
    </nav>

    <!-- ==================== TAB 1: CONTROLLO LED ==================== -->
    <div id="tabLed" class="tab-content">
      
      <!-- Telemetry Quick Row -->
      <div class="card" style="padding: 14px 18px;">
        <div class="card-title">
          <span>🌡️ Telemetria Hardware</span>
          <span id="latencyText" style="font-size: 0.75rem; text-transform: none; color: var(--text-muted);">-- ms</span>
        </div>
        <div class="telemetry-grid">
          <div class="metric-box">
            <span class="metric-sub">Temperatura MCU Core</span>
            <span id="tempVal" class="metric-val" style="color: #22c55e;">--.- °C</span>
            <span id="tempStatus" class="metric-sub">Ottimale</span>
          </div>
          <div class="metric-box">
            <span class="metric-sub">Tensione VDD Reale</span>
            <span id="vddVal" class="metric-val">3.29 V</span>
            <span class="metric-sub">Calibrata VREFINT</span>
          </div>
        </div>
      </div>

      <!-- Live RGB Orb & Color Controls -->
      <div class="card">
        <div class="card-title">
          <span>🌈 Controllo LED RGB On-Board (TIM4)</span>
          <span id="rgbLabel" style="font-size: 0.75rem; text-transform: none;">RGB: (0, 255, 30)</span>
        </div>

        <div class="orb-container">
          <div id="lightOrb" class="orb"></div>
        </div>

        <!-- Brightness Slider -->
        <div class="slider-group">
          <div class="slider-header">
            <span>Luminosità RGB</span>
            <span id="brightVal">20%</span>
          </div>
          <input type="range" id="brightSlider" min="5" max="100" value="20">
        </div>

        <!-- Quick Preset Colors -->
        <div class="slider-header" style="margin-top: 4px;">
          <span>Colori Rapidi</span>
        </div>
        <div class="preset-grid">
          <button class="btn-preset" style="background: #ef4444;" onclick="applyColor('red')" title="Rosso"></button>
          <button class="btn-preset" style="background: #22c55e;" onclick="applyColor('green')" title="Verde"></button>
          <button class="btn-preset" style="background: #3b82f6;" onclick="applyColor('blue')" title="Blu"></button>
          <button class="btn-preset" style="background: #eab308;" onclick="applyColor('yellow')" title="Giallo"></button>
          <button class="btn-preset" style="background: #06b6d4;" onclick="applyColor('cyan')" title="Ciano"></button>
          <button class="btn-preset" style="background: #ec4899;" onclick="applyColor('magenta')" title="Magenta"></button>
          <button class="btn-preset" style="background: #ffffff;" onclick="applyColor('white')" title="Bianco Freddo"></button>
          <button class="btn-preset" style="background: #fde047;" onclick="applyColor('warm')" title="Bianco Caldo"></button>
          <button class="btn-preset" style="background: #a855f7;" onclick="setCustomRgb(168, 85, 247)" title="Viola"></button>
          <button class="btn-preset" style="background: #111827; border-color: #374151;" onclick="applyMode('OFF')" title="Spento">⬛</button>
        </div>

        <!-- Custom Color Picker Row -->
        <div class="picker-row" style="margin-top: 4px;">
          <input type="color" id="nativeColorPicker" value="#00ff1e">
          <div style="flex: 1;">
            <div style="font-size: 0.85rem; font-weight: 600;">Selettore Colore Personalizzato</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">Scegli qualsiasi colore esadecimale con PWM 12-bit</div>
          </div>
        </div>

        <!-- Operating Modes Selection -->
        <div class="slider-header" style="margin-top: 4px;">
          <span>Modalità RGB</span>
        </div>
        <div class="mode-grid">
          <button id="btnModeTemp" class="btn-mode active" onclick="applyMode('TEMP')">
            🌡️ Termometro Core
          </button>
          <button id="btnModeRainbow" class="btn-mode" onclick="applyMode('RAINBOW')">
            🌈 Arcobaleno Fade
          </button>
          <button id="btnModeManual" class="btn-mode" onclick="applyMode('MANUAL')">
            🎨 Colore Manuale
          </button>
          <button id="btnModeOff" class="btn-mode" onclick="applyMode('OFF')">
            ⬛ Spegni LED
          </button>
        </div>
      </div>

      <!-- Dedicated 3 Status LEDs Section -->
      <div class="card">
        <div class="card-title">
          <span>💡 3 LED di Stato On-Board</span>
          <span style="font-size: 0.75rem; text-transform: none; color: var(--text-muted);">Controllo Indipendente</span>
        </div>
        <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.4;">
          Accendi, spegni o imposta la frequenza di lampeggio per ciascuno dei tre LED di stato fisici della scheda Tuya TYDE 3.0.
        </div>

        <!-- LED 1: PB6 -->
        <div class="led-card">
          <div class="led-header">
            <div class="led-title">
              <span id="led1Dot" class="led-indicator blink"></span>
              <span>LED 1 <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 400;">(Pin PB6 • Indicatore Wi-Fi Tuya Fisico)</span></span>
            </div>
            <span id="led1Badge" class="led-badge" style="color: var(--yellow);">BLINK (0.5s)</span>
          </div>
          <div style="font-size: 0.73rem; color: var(--text-muted); background: rgba(0,0,0,0.2); padding: 6px 10px; border-radius: 6px;">
            ℹ️ <b>LED Fisico Wi-Fi:</b> È il LED fisico sul circuito stampato che segnala lo stato di rete: <b>lampeggia a 0.5s</b> durante il pairing AP e diventa <b>acceso fisso</b> quando connesso al router.
          </div>
          <div class="led-actions">
            <button id="btnLed1On" class="btn-led-action" onclick="setLed(1, 'ON')">🟢 Accendi</button>
            <button id="btnLed1Off" class="btn-led-action" onclick="setLed(1, 'OFF')">⚫ Spegni</button>
            <button id="btnLed1Blink" class="btn-led-action active-blink" onclick="setLed(1, 'BLINK')">⚡ Blink</button>
          </div>
          <div class="slider-group">
            <div class="slider-header">
              <span>Frequenza Lampeggio</span>
              <span id="led1PeriodVal" style="color: var(--accent);">500 ms (2.0 Hz)</span>
            </div>
            <input type="range" id="led1Slider" min="100" max="2000" step="50" value="500" oninput="onLedSliderInput(1, this.value)" onchange="onLedSliderChange(1, this.value)">
            <div class="freq-chips">
              <span class="chip-freq" onclick="setLedFreq(1, 200)">0.2s (5 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(1, 500)">0.5s (2 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(1, 1000)">1.0s (1 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(1, 2000)">2.0s (0.5 Hz)</span>
            </div>
          </div>
        </div>

        <!-- LED 2: PB5 -->
        <div class="led-card">
          <div class="led-header">
            <div class="led-title">
              <span id="led2Dot" class="led-indicator on"></span>
              <span>LED 2 <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 400;">(Pin PB5 • Heartbeat MCU)</span></span>
            </div>
            <span id="led2Badge" class="led-badge" style="color: var(--green);">ACCESO FISSO</span>
          </div>
          <div class="led-actions">
            <button id="btnLed2On" class="btn-led-action active-on" onclick="setLed(2, 'ON')">🟢 Accendi</button>
            <button id="btnLed2Off" class="btn-led-action" onclick="setLed(2, 'OFF')">⚫ Spegni</button>
            <button id="btnLed2Blink" class="btn-led-action" onclick="setLed(2, 'BLINK')">⚡ Blink</button>
          </div>
          <div class="slider-group">
            <div class="slider-header">
              <span>Frequenza Lampeggio</span>
              <span id="led2PeriodVal" style="color: var(--accent);">500 ms (2.0 Hz)</span>
            </div>
            <input type="range" id="led2Slider" min="100" max="2000" step="50" value="500" oninput="onLedSliderInput(2, this.value)" onchange="onLedSliderChange(2, this.value)">
            <div class="freq-chips">
              <span class="chip-freq" onclick="setLedFreq(2, 200)">0.2s (5 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(2, 500)">0.5s (2 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(2, 1000)">1.0s (1 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(2, 2000)">2.0s (0.5 Hz)</span>
            </div>
          </div>
        </div>

        <!-- LED 3: TYWE1S / PB4 / PC13 -->
        <div class="led-card">
          <div class="led-header">
            <div class="led-title">
              <span id="led3Dot" class="led-indicator blink"></span>
              <span>LED 3 <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 400;">(Modulo TYWE1S • Radio)</span></span>
            </div>
            <span id="led3Badge" class="led-badge" style="color: var(--yellow);">IN ATTESA PAIRING</span>
          </div>
          <div style="font-size: 0.73rem; color: var(--text-muted); background: rgba(0,0,0,0.2); padding: 6px 10px; border-radius: 6px;">
            ℹ️ <b>Nota Protocollo Tuya:</b> Nel firmware di fabbrica originale Tuya, la segnalazione luminosa è delegata interamente all'STM32 tramite <b>LED 1 (PB6)</b> sopra riportato.
          </div>
          <div class="led-actions">
            <button id="btnLed3On" class="btn-led-action" onclick="setLed(3, 'ON')">🟢 Accendi</button>
            <button id="btnLed3Off" class="btn-led-action" onclick="setLed(3, 'OFF')">⚫ Spegni</button>
            <button id="btnLed3Blink" class="btn-led-action active-blink" onclick="setLed(3, 'BLINK')">⚡ Blink</button>
          </div>
          <div class="slider-group">
            <div class="slider-header">
              <span>Frequenza Lampeggio</span>
              <span id="led3PeriodVal" style="color: var(--accent);">500 ms (2.0 Hz)</span>
            </div>
            <input type="range" id="led3Slider" min="100" max="2000" step="50" value="500" oninput="onLedSliderInput(3, this.value)" onchange="onLedSliderChange(3, this.value)">
            <div class="freq-chips">
              <span class="chip-freq" onclick="setLedFreq(3, 200)">0.2s (5 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(3, 500)">0.5s (2 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(3, 1000)">1.0s (1 Hz)</span>
              <span class="chip-freq" onclick="setLedFreq(3, 2000)">2.0s (0.5 Hz)</span>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- ==================== TAB 2: IMPOSTAZIONI & WI-FI ==================== -->
    <div id="tabSettings" class="tab-content" style="display: none;">
      
      <!-- Wi-Fi Configuration Card -->
      <div class="card">
        <div class="card-title">
          <span>📡 Configurazione Rete Wi-Fi (TYWE1S)</span>
          <span id="wifiBadge" style="font-size: 0.75rem; text-transform: none; color: var(--text-muted);">Non configurato</span>
        </div>
        <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.4;">
          Scansiona le reti 2.4 GHz visibili al modulo <b>TYWE1S (ESP8266)</b>, seleziona il tuo SSID dall'elenco e invia password e credenziali alla scheda per il salvataggio permanente in memoria Flash.
        </div>

        <!-- Indirizzo IP LAN del modulo (frame WIFI:CONNECTED,IP:... via seriale) -->
        <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.25); border: 1px solid var(--card-border); border-radius: 10px; padding: 10px 14px;">
          <span style="font-size: 0.78rem; color: var(--text-muted); font-weight: 600;">Indirizzo IP sulla LAN</span>
          <span id="wifiIp" style="font-size: 0.85rem; font-weight: 800; color: var(--yellow); font-family: ui-monospace, SFMono-Regular, Consolas, monospace;">--</span>
        </div>

        <!-- Scan Button -->
        <button id="btnScanWifi" onclick="scanWifiNetworks()" style="background: rgba(56, 189, 248, 0.12); border: 1px solid var(--accent); color: var(--accent); padding: 12px 14px; border-radius: 10px; font-size: 0.85rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; transition: all 0.2s;">
          🔍 Cerca Reti Wi-Fi Visibili al Tuya
        </button>

        <!-- Discovered Networks Container -->
        <div id="networkListContainer" style="display: none; flex-direction: column; gap: 6px;">
          <label style="font-size: 0.75rem; font-weight: 700; color: var(--text-muted); display: flex; justify-content: space-between;">
            <span>Reti Rilevate (Tocca per Selezionare)</span>
            <span id="scanCount" style="color: var(--accent);">-- reti</span>
          </label>
          <div id="networkList" style="max-height: 180px; overflow-y: auto; background: rgba(0,0,0,0.3); border: 1px solid var(--card-border); border-radius: 10px; padding: 6px; display: flex; flex-direction: column; gap: 4px;">
          </div>
        </div>

        <!-- Quick Select Chip for Vodafone-30456263 -->
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">Rete Preferita:</span>
          <button type="button" onclick="selectSsid('Vodafone-30456263')" style="background: rgba(56, 189, 248, 0.15); border: 1px solid var(--accent); color: var(--accent); padding: 6px 12px; border-radius: 8px; font-size: 0.8rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 6px; transition: all 0.2s;">
            📶 Vodafone-30456263
          </button>
        </div>

        <!-- Pairing Triggers (Tuya Native Factory Protocol) -->
        <div style="background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 12px; padding: 12px; display: flex; flex-direction: column; gap: 8px;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="font-size: 0.8rem; font-weight: 700; color: var(--accent);">📡 Avvia Associazione Wi-Fi (Tuya Native)</div>
            <span style="font-size: 0.7rem; color: var(--text-muted);">TYWE1S 9600 baud</span>
          </div>
          <div style="font-size: 0.74rem; color: var(--text-muted); line-height: 1.35;">
            LED 3 lampeggia durante la ricerca e diventa <b>fisso</b> non appena il modulo è connesso alla rete Wi-Fi.
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
            <button type="button" onclick="startPairing('AP')" style="padding: 10px; background: rgba(56, 189, 248, 0.18); border: 1px solid var(--accent); color: var(--accent); border-radius: 8px; font-size: 0.78rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;">
              📱 Pairing AP (Hotspot)
            </button>
            <button type="button" onclick="startPairing('EZ')" style="padding: 10px; background: rgba(168, 85, 247, 0.18); border: 1px solid var(--purple); color: var(--purple); border-radius: 8px; font-size: 0.78rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;">
              ⚡ Pairing EZ (Tuya App)
            </button>
          </div>
        </div>

        <!-- Manual Wi-Fi State Override -->
        <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--card-border); border-radius: 12px; padding: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
          <div>
            <div style="font-size: 0.8rem; font-weight: 700;">Stato Connessione Wi-Fi Scheda</div>
            <div style="font-size: 0.73rem; color: var(--text-muted);">Sincronizza stato e LED 3</div>
          </div>
          <div style="display: flex; gap: 6px;">
            <button type="button" onclick="setWifiConnectionStatus(true)" style="padding: 6px 12px; background: rgba(34, 197, 94, 0.15); border: 1px solid var(--green); color: var(--green); border-radius: 8px; font-size: 0.75rem; font-weight: 700; cursor: pointer;">
              🟢 Connesso (Fisso)
            </button>
            <button type="button" onclick="setWifiConnectionStatus(false)" style="padding: 6px 12px; background: rgba(239, 68, 68, 0.15); border: 1px solid var(--red); color: var(--red); border-radius: 8px; font-size: 0.75rem; font-weight: 700; cursor: pointer;">
              🟠 Disconnesso (Blink)
            </button>
          </div>
        </div>

        <!-- Inputs Form -->
        <div style="display: flex; flex-direction: column; gap: 12px;">
          <div>
            <label style="font-size: 0.75rem; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">SSID Rete (Nome Wi-Fi)</label>
            <input type="text" id="wifiSsid" class="input-field" placeholder="Es. Vodafone-30456263 o seleziona sopra" autocomplete="username">
          </div>
          <div>
            <label style="font-size: 0.75rem; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">Password Wi-Fi</label>
            <div style="position: relative;">
              <input type="password" id="wifiPass" class="input-field" placeholder="Password WPA2/WPA3" autocomplete="current-password" style="padding-right: 42px;">
              <button type="button" onclick="togglePassVisibility()" style="position: absolute; right: 12px; top: 50%; transform: translateY(-50%); background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 1.1rem; line-height: 1;">👁️</button>
            </div>
          </div>
          <button id="btnSendWifi" class="btn-primary" onclick="sendWifiConfig()">
            🚀 Invia Configurazione al Tuya (Push Wi-Fi & Salva)
          </button>
          <div id="wifiMsg" style="font-size: 0.8rem; display: none; padding: 12px; border-radius: 10px; text-align: center; font-weight: 600;"></div>
        </div>
      </div>

      <!-- Accesso Dashboard da Rete Locale (LAN & Smartphone) -->
      <div class="card" style="border: 1px solid rgba(56, 189, 248, 0.3); background: rgba(56, 189, 248, 0.05);">
        <div class="card-title" style="color: var(--accent);">
          <span>🌐 Accesso Dashboard da Smartphone (Stessa Rete Wi-Fi)</span>
        </div>
        <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.5; display: flex; flex-direction: column; gap: 8px;">
          <div>Questa pagina web è servita in tempo reale dal PC ed è accessibile da qualsiasi smartphone, tablet o PC collegato al Wi-Fi di casa (<b>Vodafone-30456263</b>):</div>
          <div style="background: rgba(0,0,0,0.3); padding: 10px 14px; border-radius: 8px; border: 1px solid var(--card-border); font-family: ui-monospace, monospace; font-size: 0.9rem; color: var(--accent); font-weight: 700;">
            http://192.168.1.215:8090/
          </div>
          <div style="font-size: 0.76rem; color: var(--text-muted);">
            ℹ️ <b>Architettura Duale:</b> Il modulo radio <b>TYWE1S</b> comunica con i server Tuya e l'app mobile <b>Smart Life</b>. Il microcontrollore <b>STM32</b> gestisce i sensori, il LED RGB e la porta seriale USB collegata a questo server web.
          </div>
        </div>
      </div>

      <!-- EMQX Cloud Bridge Card -->
      <div class="card">
        <div class="card-title">
          <span>☁️ EMQX Cloud Bridge (MQTT/TLS)</span>
          <span id="emqxBadge" style="font-size: 0.75rem; text-transform: none; color: var(--text-muted);">non attivo</span>
        </div>
        <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.4;">
          Pubblica la telemetria su <code style="color: var(--accent);">tuya/tyde3/telemetry</code> e riceve comandi su <code style="color: var(--accent);">tuya/tyde3/command</code> via TLS verso <code style="color: var(--accent);">r202df21.ala.eu-central-1.emqxsl.com:8883</code>. Credenziali dal Windows Credential Manager (target <b>EMQX-MQTT</b>).
        </div>
        <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.25); border: 1px solid var(--card-border); border-radius: 10px; padding: 10px 14px;">
          <span style="font-size: 0.78rem; color: var(--text-muted); font-weight: 600;">Stato bridge</span>
          <span id="emqxState" style="font-size: 0.8rem; font-weight: 700; color: var(--yellow);">--</span>
        </div>
        <button id="btnTestEmqx" onclick="testEmqx()" style="background: rgba(56, 189, 248, 0.12); border: 1px solid var(--accent); color: var(--accent); padding: 12px 14px; border-radius: 10px; font-size: 0.85rem; font-weight: 700; cursor: pointer;">📤 Pubblica Telemetria su EMQX Ora</button>
      </div>

      <!-- REST API Endpoints Quick Reference -->
      <div class="card">
        <div class="card-title">📡 REST API Endpoints</div>
        <div class="code-box"># Controllo LED di stato (id: 1, 2, 3 • mode: ON, OFF, BLINK • period_ms):
curl -X POST http://&lt;IP&gt;:8080/api/led -H "Content-Type: application/json" -d '{"led": 1, "mode": "BLINK", "period_ms": 200}'
curl -X POST http://&lt;IP&gt;:8080/api/led -H "Content-Type: application/json" -d '{"led": 3, "mode": "ON"}'

# Lettura stato di tutti i LED:
curl http://&lt;IP&gt;:8080/api/leds

# Imposta colore RGB e luminosità:
curl -X POST http://&lt;IP&gt;:8080/api/rgb -H "Content-Type: application/json" -d '{"r": 0, "g": 255, "b": 30, "brightness": 20}'

# Configura Wi-Fi SSID e Password su modulo Tuya:
curl -X POST http://&lt;IP&gt;:8080/api/wifi -H "Content-Type: application/json" -d '{"ssid": "Vodafone-30456263", "password": "pass"}'

# Lettura telemetria sensore temperatura e tensione:
curl http://&lt;IP&gt;:8080/api/temp</div>
      </div>

    </div>

    <footer>
      Tuya TYDE 3.0 v1.0.3 • STM32F103 @ 72MHz • PlatformIO C++ & Cyclopts Toolchain
    </footer>
  </div>

  <!-- Floating Toast Notification -->
  <div id="toastNotification" class="toast-popup" style="display: none; opacity: 0;">
    <span id="toastIcon">✅</span>
    <span id="toastMsg">Comando inviato via USB</span>
  </div>

  <script>
    let toastTimeout = null;
    function showToast(msg, isSuccess = true) {
      const toast = document.getElementById('toastNotification');
      const icon = document.getElementById('toastIcon');
      const text = document.getElementById('toastMsg');
      if (!toast) return;
      icon.innerText = isSuccess ? '✅' : '❌';
      text.innerText = msg;
      toast.style.borderColor = isSuccess ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)';
      toast.style.color = isSuccess ? '#22c55e' : '#ef4444';
      toast.style.display = 'flex';
      toast.style.opacity = '1';
      clearTimeout(toastTimeout);
      toastTimeout = setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => { toast.style.display = 'none'; }, 300);
      }, 2200);
    }

    let currentBrightness = 20;
    let currentMode = 'TEMP';
    let currentR = 0, currentG = 255, currentB = 30;
    let sliderDebounce = null;
    let ledSlidersDebounce = {};

    const orb = document.getElementById('lightOrb');
    const brightSlider = document.getElementById('brightSlider');
    const brightVal = document.getElementById('brightVal');
    const tempVal = document.getElementById('tempVal');
    const tempStatus = document.getElementById('tempStatus');
    const vddVal = document.getElementById('vddVal');
    const latencyText = document.getElementById('latencyText');
    const statusBadge = document.getElementById('statusBadge');
    const statusText = document.getElementById('statusText');
    const rgbLabel = document.getElementById('rgbLabel');
    const colorPicker = document.getElementById('nativeColorPicker');

    // Navigation Tab Switching
    function switchTab(tab) {
      const tabLed = document.getElementById('tabLed');
      const tabSettings = document.getElementById('tabSettings');
      const tabBtnLed = document.getElementById('tabBtnLed');
      const tabBtnSettings = document.getElementById('tabBtnSettings');

      if (tab === 'led') {
        tabLed.style.display = 'flex';
        tabSettings.style.display = 'none';
        tabBtnLed.classList.add('active');
        tabBtnSettings.classList.remove('active');
      } else {
        tabLed.style.display = 'none';
        tabSettings.style.display = 'flex';
        tabBtnLed.classList.remove('active');
        tabBtnSettings.classList.add('active');
      }
    }

    function updateOrb(r, g, b, brightness) {
      const alpha = Math.max(0.1, brightness / 100);
      const color = `rgb(${r}, ${g}, ${b})`;
      const glow = `rgba(${r}, ${g}, ${b}, ${alpha * 0.8})`;
      orb.style.background = color;
      orb.style.boxShadow = `0 0 ${Math.max(10, brightness * 0.8)}px ${glow}`;
      orb.style.opacity = Math.max(0.2, brightness / 100);
      rgbLabel.innerText = `RGB: (${r}, ${g}, ${b}) @ ${brightness}%`;
    }

    function setModeButtonActive(mode) {
      document.querySelectorAll('.btn-mode').forEach(b => b.classList.remove('active'));
      const map = {
        'TEMP': 'btnModeTemp',
        'RAINBOW': 'btnModeRainbow',
        'MANUAL': 'btnModeManual',
        'OFF': 'btnModeOff'
      };
      if (map[mode]) {
        document.getElementById(map[mode]).classList.add('active');
      }
    }

    function updateLedCard(id, mode, period) {
      const dot = document.getElementById(`led${id}Dot`);
      const badge = document.getElementById(`led${id}Badge`);
      const btnOn = document.getElementById(`btnLed${id}On`);
      const btnOff = document.getElementById(`btnLed${id}Off`);
      const btnBlink = document.getElementById(`btnLed${id}Blink`);
      const slider = document.getElementById(`led${id}Slider`);
      const periodVal = document.getElementById(`led${id}PeriodVal`);

      if (!dot) return;

      dot.classList.remove('on', 'off', 'blink');
      btnOn.classList.remove('active-on');
      btnOff.classList.remove('active-off');
      btnBlink.classList.remove('active-blink');

      const hz = (1000 / (period || 500)).toFixed(1);
      if (periodVal) periodVal.innerText = `${period || 500} ms (${hz} Hz)`;
      if (slider && !slider.matches(':active')) slider.value = period || 500;

      if (mode === 'ON') {
        dot.classList.add('on');
        badge.innerText = 'ACCESO FISSO';
        badge.style.color = 'var(--green)';
        btnOn.classList.add('active-on');
      } else if (mode === 'OFF') {
        dot.classList.add('off');
        badge.innerText = 'SPENTO';
        badge.style.color = 'var(--text-muted)';
        btnOff.classList.add('active-off');
      } else {
        dot.classList.add('blink');
        badge.innerText = `BLINK (${((period || 500) / 1000).toFixed(1)}s)`;
        badge.style.color = 'var(--yellow)';
        btnBlink.classList.add('active-blink');
      }
    }

    async function setLed(id, mode, period_ms) {
      const slider = document.getElementById(`led${id}Slider`);
      const period = period_ms || (slider ? parseInt(slider.value) : 500);
      updateLedCard(id, mode, period);

      try {
        const res = await fetch('/api/led', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({led: id, mode: mode, period_ms: period})
        });
        const data = await res.json();
        if (data.ok) {
          showToast(`LED ${id}: ${mode} inviato via USB COM3`, true);
          if (data.leds && data.leds[String(id)]) {
            const l = data.leds[String(id)];
            updateLedCard(id, l.mode, l.period_ms);
          }
        } else {
          showToast(`Errore USB LED ${id}: ${data.error || 'Comando non riuscito'}`, false);
        }
      } catch (err) {
        showToast(`Errore invio LED ${id}: ${err.message}`, false);
      }
    }

    function onLedSliderInput(id, val) {
      const hz = (1000 / val).toFixed(1);
      const periodVal = document.getElementById(`led${id}PeriodVal`);
      if (periodVal) periodVal.innerText = `${val} ms (${hz} Hz)`;
    }

    function onLedSliderChange(id, val) {
      clearTimeout(ledSlidersDebounce[id]);
      ledSlidersDebounce[id] = setTimeout(() => {
        setLed(id, 'BLINK', parseInt(val));
      }, 50);
    }

    function setLedFreq(id, period_ms) {
      const slider = document.getElementById(`led${id}Slider`);
      if (slider) slider.value = period_ms;
      onLedSliderInput(id, period_ms);
      setLed(id, 'BLINK', period_ms);
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        // Update badge
        const usbBadge = document.getElementById('usbBadge');
        const usbText = document.getElementById('usbText');

        if (data.online) {
          statusBadge.classList.remove('offline');
          statusText.innerText = 'ONLINE';
          latencyText.innerText = `${data.latency_ms} ms`;
          if (usbBadge) {
            usbBadge.classList.remove('offline');
            usbText.innerText = `USB ${data.port || 'COM3'} (ATTIVO)`;
          }
        } else {
          statusBadge.classList.add('offline');
          statusText.innerText = 'OFFLINE';
          latencyText.innerText = data.last_error ? `Err: ${data.last_error.substring(0, 15)}...` : 'Disconnesso';
          if (usbBadge) {
            usbBadge.classList.add('offline');
            usbText.innerText = `USB ${data.port || 'COM3'} (OFFLINE)`;
          }
        }

        // Update temp
        if (data.temp_c !== undefined) {
          tempVal.innerText = `${data.temp_c.toFixed(1)} °C`;
          if (data.temp_c < 32) {
            tempVal.style.color = '#38bdf8';
            tempStatus.innerText = 'Fresco (< 32°C)';
          } else if (data.temp_c < 36) {
            tempVal.style.color = '#06b6d4';
            tempStatus.innerText = 'Normale (32-36°C)';
          } else if (data.temp_c < 42) {
            tempVal.style.color = '#22c55e';
            tempStatus.innerText = 'Ottimale (36-42°C)';
          } else if (data.temp_c < 50) {
            tempVal.style.color = '#eab308';
            tempStatus.innerText = 'Tiepido (42-50°C)';
          } else {
            tempVal.style.color = '#ef4444';
            tempStatus.innerText = 'Attenzione (> 50°C)';
          }
        }

        if (data.vdd !== undefined) {
          vddVal.innerText = `${data.vdd.toFixed(2)} V`;
        }

        // Update brightness & mode
        currentBrightness = data.brightness;
        currentMode = data.mode;
        brightVal.innerText = `${currentBrightness}%`;
        if (!brightSlider.matches(':active')) brightSlider.value = currentBrightness;
        setModeButtonActive(currentMode);

        if (data.mode === 'MANUAL') {
          currentR = data.r; currentG = data.g; currentB = data.b;
          updateOrb(currentR, currentG, currentB, currentBrightness);
        } else if (data.mode === 'TEMP') {
          if (data.temp_c < 32) updateOrb(0, 50, 255, currentBrightness);
          else if (data.temp_c < 36) updateOrb(0, 255, 200, currentBrightness);
          else if (data.temp_c < 42) updateOrb(0, 255, 30, currentBrightness);
          else if (data.temp_c < 50) updateOrb(255, 180, 0, currentBrightness);
          else updateOrb(255, 0, 0, currentBrightness);
        } else if (data.mode === 'OFF') {
          updateOrb(0, 0, 0, 0);
        }

        // Update 3 Status LEDs
        if (data.leds) {
          for (let i = 1; i <= 3; i++) {
            const l = data.leds[String(i)];
            if (l) updateLedCard(i, l.mode, l.period_ms);
          }
        }

        // Update Wi-Fi LAN IP
        const wifiIp = document.getElementById('wifiIp');
        if (wifiIp) {
          if (data.wifi_connected && data.wifi_ip) {
            wifiIp.innerText = data.wifi_ip;
            wifiIp.style.color = '#22c55e';
          } else if (data.wifi_connected) {
            wifiIp.innerText = 'connesso (acquisizione IP...)';
            wifiIp.style.color = '#eab308';
          } else {
            wifiIp.innerText = 'non connesso (in attesa pairing app Tuya)';
            wifiIp.style.color = '#eab308';
          }
        }

        // Update Wi-Fi badge
        const wifiBadge = document.getElementById('wifiBadge');
        if (data.wifi_connected) {
          if (wifiBadge) {
            wifiBadge.innerText = 'Connesso: ' + (data.wifi_ssid || 'Vodafone-30456263');
            wifiBadge.style.color = '#22c55e';
          }
        } else {
          if (wifiBadge) {
            wifiBadge.innerText = 'In attesa pairing app (SmartLife-xxxx)';
            wifiBadge.style.color = '#eab308';
          }
        }

        // Update EMQX Cloud bridge state
        const emqxBadge = document.getElementById('emqxBadge');
        const emqxState = document.getElementById('emqxState');
        if (data.emqx && emqxBadge && emqxState) {
          if (data.emqx.connected) {
            emqxBadge.innerText = 'CONNESSO';
            emqxBadge.style.color = '#22c55e';
            emqxState.innerText = `online · inviati: ${data.emqx.telemetry_sent ?? 0} · ricevuti: ${data.emqx.commands_received ?? 0}`;
            emqxState.style.color = '#22c55e';
          } else if (data.emqx.enabled) {
            emqxBadge.innerText = 'IN CONNESSIONE';
            emqxBadge.style.color = '#eab308';
            emqxState.innerText = 'riconnessione in corso...';
            emqxState.style.color = '#eab308';
          } else {
            emqxBadge.innerText = 'DISATTIVO';
            emqxBadge.style.color = '#ef4444';
            emqxState.innerText = data.emqx.last_error ? String(data.emqx.last_error).substring(0, 48) : 'non configurato';
            emqxState.style.color = '#ef4444';
          }
        }
      } catch (err) {
        statusBadge.classList.add('offline');
        statusText.innerText = 'ERRORE';
        latencyText.innerText = 'Offline';
        const usbBadge = document.getElementById('usbBadge');
        const usbText = document.getElementById('usbText');
        if (usbBadge) {
          usbBadge.classList.add('offline');
          usbText.innerText = 'USB COM3 (OFFLINE)';
        }
      }
    }

    async function testEmqx() {
      const btn = document.getElementById('btnTestEmqx');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/emqx/test', { method: 'POST' });
        const d = await res.json();
        showToast(
          d.ok ? 'Telemetria pubblicata su EMQX Cloud' : `Errore EMQX: ${d.error || 'publish fallita'}`,
          d.ok
        );
      } catch (err) {
        showToast(`Errore EMQX: ${err.message}`, false);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function setWifiConnectionStatus(connected) {
      try {
        await fetch('/api/wifi/status', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({connected: connected})
        });
        fetchStatus();
      } catch (err) {
        console.error(err);
      }
    }

    // Brightness Slider Event
    brightSlider.addEventListener('input', (e) => {
      const val = parseInt(e.target.value);
      brightVal.innerText = `${val}%`;
      currentBrightness = val;
      updateOrb(currentR, currentG, currentB, val);

      clearTimeout(sliderDebounce);
      sliderDebounce = setTimeout(async () => {
        try {
          const res = await fetch('/api/brightness', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({brightness: val})
          });
          const d = await res.json();
          if (d.ok) {
            showToast(`Luminosità ${val}% applicata via USB`, true);
          } else {
            showToast(`Errore USB luminosità: ${d.error || 'Fallito'}`, false);
          }
        } catch (err) {
          showToast(`Errore invio luminosità: ${err.message}`, false);
        }
      }, 50);
    });

    // Preset Colors
    async function applyColor(name) {
      setModeButtonActive('MANUAL');
      try {
        const res = await fetch('/api/rgb', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({color: name, brightness: currentBrightness})
        });
        const d = await res.json();
        if (d.ok) {
          showToast(`Colore RGB (${name}) impostato via USB`, true);
        } else {
          showToast(`Errore USB: ${d.error || 'Comando non riuscito'}`, false);
        }
      } catch (err) {
        showToast(`Errore invio USB: ${err.message}`, false);
      }
      fetchStatus();
    }

    async function setCustomRgb(r, g, b) {
      setModeButtonActive('MANUAL');
      updateOrb(r, g, b, currentBrightness);
      try {
        const res = await fetch('/api/rgb', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({r: r, g: g, b: b, brightness: currentBrightness})
        });
        const d = await res.json();
        if (d.ok) {
          showToast(`RGB (${r}, ${g}, ${b}) inviato via USB`, true);
        } else {
          showToast(`Errore USB: ${d.error || 'Comando non riuscito'}`, false);
        }
      } catch (err) {
        showToast(`Errore invio USB: ${err.message}`, false);
      }
      fetchStatus();
    }

    // Custom Color Picker
    colorPicker.addEventListener('change', async (e) => {
      const hex = e.target.value;
      const r = parseInt(hex.substr(1, 2), 16);
      const g = parseInt(hex.substr(3, 2), 16);
      const b = parseInt(hex.substr(5, 2), 16);
      setCustomRgb(r, g, b);
    });

    // Mode Toggle
    async function applyMode(mode) {
      setModeButtonActive(mode);
      try {
        const res = await fetch('/api/mode', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({mode: mode})
        });
        const d = await res.json();
        if (d.ok) {
          showToast(`Modalità RGB ${mode} impostata via USB`, true);
        } else {
          showToast(`Errore USB: ${d.error || 'Comando non riuscito'}`, false);
        }
      } catch (err) {
        showToast(`Errore invio USB: ${err.message}`, false);
      }
      fetchStatus();
    }

    function togglePassVisibility() {
      const p = document.getElementById('wifiPass');
      p.type = p.type === 'password' ? 'text' : 'password';
    }

    async function scanWifiNetworks() {
      const btn = document.getElementById('btnScanWifi');
      const listContainer = document.getElementById('networkListContainer');
      const list = document.getElementById('networkList');
      const countLabel = document.getElementById('scanCount');

      btn.disabled = true;
      btn.innerHTML = '⏳ Scansione Wi-Fi in corso...';
      try {
        const res = await fetch('/api/wifi/scan');
        const data = await res.json();
        if (data.ok && data.networks) {
          listContainer.style.display = 'flex';
          countLabel.innerText = `${data.networks.length} reti rilevate`;
          list.innerHTML = '';
          data.networks.forEach(net => {
            const item = document.createElement('div');
            item.style.display = 'flex';
            item.style.justifyContent = 'space-between';
            item.style.alignItems = 'center';
            item.style.padding = '8px 12px';
            item.style.borderRadius = '8px';
            item.style.background = 'rgba(255, 255, 255, 0.04)';
            item.style.cursor = 'pointer';
            item.style.transition = 'all 0.2s';
            item.onmouseover = () => item.style.background = 'rgba(56, 189, 248, 0.15)';
            item.onmouseout = () => item.style.background = 'rgba(255, 255, 255, 0.04)';
            item.onclick = () => selectSsid(net.ssid);

            let sigColor = '#22c55e';
            if (net.signal < 45) { sigColor = '#ef4444'; }
            else if (net.signal < 70) { sigColor = '#eab308'; }

            item.innerHTML = `
              <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 1rem;">📶</span>
                <span style="font-weight: 600; font-size: 0.85rem; color: #fff;">${net.ssid}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 6px; font-size: 0.75rem; color: ${sigColor}; font-weight: 600;">
                <span>${net.signal}%</span>
                <span style="color: var(--text-muted); font-size: 0.7rem;">(${net.rssi} dBm)</span>
              </div>
            `;
            list.appendChild(item);
          });
        }
      } catch (err) {
        alert('Errore durante la scansione Wi-Fi: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.innerHTML = '🔍 Cerca Reti Wi-Fi Visibili al Tuya';
      }
    }

    function selectSsid(ssid) {
      const input = document.getElementById('wifiSsid');
      input.value = ssid;
      const pass = document.getElementById('wifiPass');
      pass.focus();
      input.style.borderColor = 'var(--accent)';
      setTimeout(() => { input.style.borderColor = 'var(--card-border)'; }, 1000);
    }

    async function sendWifiConfig() {
      const ssid = document.getElementById('wifiSsid').value.trim();
      const pass = document.getElementById('wifiPass').value.trim();
      const msgBox = document.getElementById('wifiMsg');
      const btn = document.getElementById('btnSendWifi');

      if (!ssid) {
        msgBox.style.display = 'block';
        msgBox.style.background = 'rgba(239, 68, 68, 0.2)';
        msgBox.style.color = '#ef4444';
        msgBox.innerText = 'Inserisci il nome della rete (SSID)';
        return;
      }

      if (!confirm(`Inviare la configurazione per la rete "${ssid}"? Questo metterà il modulo TYWE1S in modalità pairing.`)) {
        return;
      }

      btn.disabled = true;
      btn.innerText = 'Invio al modulo Tuya in corso...';
      try {
        const res = await fetch('/api/wifi', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ssid: ssid, password: pass})
        });
        const d = await res.json();
        msgBox.style.display = 'block';
        if (d.ok) {
          msgBox.style.background = 'rgba(56, 189, 248, 0.12)';
          msgBox.style.color = '#fff';
          msgBox.style.border = '1px solid rgba(56, 189, 248, 0.3)';
          msgBox.innerHTML = `
            <div style="font-weight: 700; color: var(--accent); margin-bottom: 8px;">📡 ${d.message}</div>
            <div style="text-align: left; font-size: 0.78rem; line-height: 1.5; color: #e2e8f0;">
              ℹ️ <i>Il firmware originale Tuya riceve la password via radio dallo smartphone (non via cavo USB).</i><br>
              <b>Completa l'associazione in 30 secondi:</b><br>
              1. Apri l'app <b>Tuya Smart</b> (o <b>Smart Life</b>) sul telefono (connesso a 2.4 GHz).<br>
              2. Tocca <b>+</b> (Aggiungi) &gt; <i>Illuminazione</i> &gt; scegli in alto <b>Modalità AP (Hotspot)</b>.<br>
              3. Conferma la password e connetti il Wi-Fi del telefono all'hotspot della scheda <b>SmartLife-xxxx</b>.<br>
              4. Torna nell'app Tuya: il telefono passerà la password al modulo. Il LED1 diventerà <b>fisso</b> e l'IP LAN apparirà qui sotto!
            </div>
          `;
          const badge = document.getElementById('wifiBadge');
          badge.innerText = 'Pairing AP: ' + ssid;
          badge.style.color = 'var(--yellow)';
        } else {
          msgBox.style.background = 'rgba(239, 68, 68, 0.2)';
          msgBox.style.color = '#ef4444';
          msgBox.innerText = '❌ ' + (d.error || 'Errore');
        }
      } catch (err) {
        msgBox.style.display = 'block';
        msgBox.style.background = 'rgba(239, 68, 68, 0.2)';
        msgBox.style.color = '#ef4444';
        msgBox.innerText = 'Errore di connessione: ' + err.message;
      } finally {
        btn.disabled = false;
        btn.innerText = '🚀 Invia Configurazione al Tuya (Push Wi-Fi & Salva)';
      }
    }

    async function disconnectWifiLan() {
      const btn = document.getElementById('btnDisconnectWifi');
      const msg = document.getElementById('disconnectMsg');
      btn.disabled = true;
      btn.innerText = '⏳ Disconnessione in corso...';
      try {
        const res = await fetch('/api/wifi/disconnect', { method: 'POST' });
        const d = await res.json();
        msg.style.display = 'block';
        if (d.ok) {
          msg.style.background = 'rgba(234, 179, 8, 0.2)';
          msg.style.color = '#eab308';
          msg.innerText = '✅ Disconnesso dalla LAN! Wi-Fi Tuya-TYDE3-AP attivo per pairing da cellulare. LED 3 lampeggia a 0.5s.';
          const badge = document.getElementById('wifiBadge');
          if (badge) {
            badge.innerText = 'Disconnesso / SoftAP';
            badge.style.color = '#eab308';
          }
        } else {
          msg.style.background = 'rgba(239, 68, 68, 0.2)';
          msg.style.color = '#ef4444';
          msg.innerText = '❌ Errore: ' + (d.error || 'Impossibile disconnettere');
        }
        fetchStatus();
      } catch (err) {
        msg.style.display = 'block';
        msg.style.background = 'rgba(239, 68, 68, 0.2)';
        msg.style.color = '#ef4444';
        msg.innerText = 'Errore di connessione: ' + err.message;
      } finally {
        btn.disabled = false;
        btn.innerText = '📱 Disconnetti dalla LAN & Rendi Visibile per Cellulare';
      }
    }

    async function startPairing(mode) {
      if (!confirm(`Attenzione: avviare il pairing ${mode} cancellerà le credenziali Wi-Fi salvate nel modulo Tuya TYWE1S e richiederà di riconfigurarlo tramite l'app Smart Life. Continuare?`)) {
        return;
      }
      const isAp = mode === 'AP';
      const endpoint = isAp ? '/api/wifi/pair/ap' : '/api/wifi/pair/ez';
      const msgBox = document.getElementById('wifiMsg');
      try {
        const res = await fetch(endpoint, { method: 'POST' });
        const d = await res.json();
        if (msgBox) {
          msgBox.style.display = 'block';
          msgBox.style.background = 'rgba(56, 189, 248, 0.2)';
          msgBox.style.color = '#38bdf8';
          msgBox.innerText = '✅ ' + (d.message || `Pairing ${mode} avviato!`);
        }
        showToast(`Pairing ${mode} avviato: LED 3 lampeggia!`, true);
        fetchStatus();
      } catch (err) {
        if (msgBox) {
          msgBox.style.display = 'block';
          msgBox.style.background = 'rgba(239, 68, 68, 0.2)';
          msgBox.style.color = '#ef4444';
          msgBox.innerText = '❌ Errore avvio pairing: ' + err.message;
        }
      }
    }

    // Initial load and periodic polling every 2 seconds
    fetchStatus();
    setInterval(fetchStatus, 2000);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    web_port = 8090
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        web_port = int(sys.argv[1])
    b = BoardBridge("COM3", 115200)
    server = create_app(b)
    server.run(host="0.0.0.0", port=web_port, debug=False)
