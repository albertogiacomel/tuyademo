#!/usr/bin/env python3
"""tuya_web.py - Embedded Web Dashboard & REST API for Tuya TYDE 3.0 IoT Dev Kit.

Provides:
- Mobile-friendly, responsive Glassmorphic Web UI accessible from any device over Wi-Fi
- Full REST API for Home Assistant / Node-RED / curl / browser integration
- Thread-safe serial communication with STM32 Smart Bridge
- Live temperature telemetry, RGB PWM control, brightness regulation, and mode selection
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import serial
from flask import Flask, Response, jsonify, request

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
        }

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
                    return True
                self.status["online"] = False
                return False
            except (serial.SerialException, OSError) as exc:
                logger.warning("Ping error on %s: %s", self.port, exc)
                self._close_serial()
                self.status["online"] = False
                return False

    def read_temp(self) -> dict[str, Any]:
        with self.lock:
            t0 = time.time()
            try:
                ser = self._get_serial()
                ser.reset_input_buffer()
                ser.write(b"T")
                ser.flush()
                line = ser.readline().decode("ascii", errors="replace").strip()
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
                resp = ser.readline().decode("ascii", errors="replace").strip()
                self.status["online"] = True
                self.status["last_seen"] = datetime.now(UTC).isoformat()
                return resp
            except (serial.SerialException, OSError) as exc:
                logger.warning("Send cmd error '%s': %s", cmd_str.strip(), exc)
                self._close_serial()
                self.status["online"] = False
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


def create_app(bridge: BoardBridge) -> Flask:
    app = Flask(__name__)

    # Pre-warm bridge on startup
    bridge.ping()
    bridge.read_temp()

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
        # Quick live temp update if possible
        bridge.read_temp()
        return jsonify(bridge.status)

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
        return Response(DASHBOARD_HTML, mimetype="text/html; charset=utf-8")

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
      padding: 8px 4px;
    }

    .logo-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .logo-icon {
      width: 36px;
      height: 36px;
      background: linear-gradient(135deg, #38bdf8, #818cf8);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 12px var(--accent-glow);
    }

    h1 {
      font-size: 1.25rem;
      font-weight: 700;
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
      font-weight: 600;
      transition: all 0.3s ease;
    }

    .status-badge.offline {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.3);
      color: var(--red);
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
    }

    /* Cards */
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
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      display: flex;
      justify-content: space-between;
      align-items: center;
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
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .metric-val {
      font-size: 1.7rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .metric-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    /* Live Glow Orb */
    .orb-container {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px 0;
    }

    .orb {
      width: 100px;
      height: 100px;
      border-radius: 50%;
      background: #38bdf8;
      box-shadow: 0 0 40px rgba(56, 189, 248, 0.6);
      transition: all 0.25s ease-out;
      border: 3px solid rgba(255, 255, 255, 0.4);
    }

    /* Controls */
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
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: #ffffff;
      cursor: pointer;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5);
      border: 2px solid var(--accent);
      transition: transform 0.1s;
    }

    input[type="range"]::-webkit-slider-thumb:active {
      transform: scale(1.15);
    }

    /* Preset Buttons */
    .preset-grid {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 8px;
    }

    .btn-preset {
      height: 42px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.15);
      cursor: pointer;
      transition: transform 0.15s, box-shadow 0.15s;
      position: relative;
    }

    .btn-preset:active {
      transform: scale(0.92);
    }

    /* Mode Selection */
    .mode-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .btn-mode {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 14px 10px;
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

    /* Color Picker Row */
    .picker-row {
      display: flex;
      gap: 12px;
      align-items: center;
    }

    input[type="color"] {
      -webkit-appearance: none;
      border: none;
      width: 48px;
      height: 48px;
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

    /* API Explorer */
    .code-box {
      background: #050810;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 0.75rem;
      color: #93c5fd;
      overflow-x: auto;
      white-space: pre-wrap;
    }

    footer {
      text-align: center;
      font-size: 0.7rem;
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
      <div id="statusBadge" class="status-badge">
        <span class="status-dot"></span>
        <span id="statusText">ONLINE</span>
      </div>
    </header>

    <!-- Telemetry Card -->
    <div class="card">
      <div class="card-title">
        <span>🌡️ Telemetria MCU STM32</span>
        <span id="latencyText" style="font-size: 0.75rem; text-transform: none; color: var(--text-muted);">-- ms</span>
      </div>
      <div class="telemetry-grid">
        <div class="metric-box">
          <span class="metric-sub">Temperatura Core (ADC16)</span>
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
          <span>Luminosità</span>
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
          <div style="font-size: 0.75rem; color: var(--text-muted);">Tocca per scegliere qualsiasi tonalità esadecimale</div>
        </div>
      </div>
    </div>

    <!-- Operating Modes Card -->
    <div class="card">
      <div class="card-title">Modalità Operativa</div>
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

    <!-- REST API Endpoints Quick Reference -->
    <div class="card">
      <div class="card-title">📡 REST API Endpoints</div>
      <div class="code-box"># Regola luminosità (0-100%):
curl -X POST http://&lt;IP&gt;:8080/api/brightness -H "Content-Type: application/json" -d '{"brightness": 15}'

# Imposta colore RGB e luminosità:
curl -X POST http://&lt;IP&gt;:8080/api/rgb -H "Content-Type: application/json" -d '{"r": 0, "g": 255, "b": 0, "brightness": 20}'

# Imposta modalità (TEMP, RAINBOW, MANUAL, OFF):
curl -X POST http://&lt;IP&gt;:8080/api/mode -H "Content-Type: application/json" -d '{"mode": "RAINBOW"}'

# Lettura telemetria sensore temperatura:
curl http://&lt;IP&gt;:8080/api/temp</div>
    </div>

    <footer>
      Tuya TYDE 3.0 v1.0.3 • STM32F103 @ 72MHz • PlatformIO C++ & Cyclopts Toolchain
    </footer>
  </div>

  <script>
    let currentBrightness = 20;
    let currentMode = 'TEMP';
    let currentR = 0, currentG = 255, currentB = 30;
    let sliderDebounce = null;

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

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        // Update badge
        if (data.online) {
          statusBadge.classList.remove('offline');
          statusText.innerText = 'ONLINE';
          latencyText.innerText = `${data.latency_ms} ms`;
        } else {
          statusBadge.classList.add('offline');
          statusText.innerText = 'OFFLINE';
          latencyText.innerText = 'Disconnesso';
        }

        // Update temp
        if (data.temp_c !== undefined) {
          tempVal.innerText = `${data.temp_c.toFixed(1)} °C`;
          if (data.temp_c < 32) {
            tempVal.style.color = '#38bdf8'; // Blue
            tempStatus.innerText = 'Fresco (< 32°C)';
          } else if (data.temp_c < 36) {
            tempVal.style.color = '#06b6d4'; // Cyan
            tempStatus.innerText = 'Normale (32-36°C)';
          } else if (data.temp_c < 42) {
            tempVal.style.color = '#22c55e'; // Green
            tempStatus.innerText = 'Ottimale (36-42°C)';
          } else if (data.temp_c < 50) {
            tempVal.style.color = '#eab308'; // Amber
            tempStatus.innerText = 'Tiepido (42-50°C)';
          } else {
            tempVal.style.color = '#ef4444'; // Red
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
        brightSlider.value = currentBrightness;
        setModeButtonActive(currentMode);

        if (data.mode === 'MANUAL') {
          currentR = data.r; currentG = data.g; currentB = data.b;
          updateOrb(currentR, currentG, currentB, currentBrightness);
        } else if (data.mode === 'TEMP') {
          // Calculate approx color from temp
          if (data.temp_c < 32) updateOrb(0, 50, 255, currentBrightness);
          else if (data.temp_c < 36) updateOrb(0, 255, 200, currentBrightness);
          else if (data.temp_c < 42) updateOrb(0, 255, 30, currentBrightness);
          else if (data.temp_c < 50) updateOrb(255, 180, 0, currentBrightness);
          else updateOrb(255, 0, 0, currentBrightness);
        } else if (data.mode === 'OFF') {
          updateOrb(0, 0, 0, 0);
        }
      } catch (err) {
        statusBadge.classList.add('offline');
        statusText.innerText = 'ERRORE';
        latencyText.innerText = 'Offline';
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
        await fetch('/api/brightness', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({brightness: val})
        });
      }, 50);
    });

    // Preset Colors
    async function applyColor(name) {
      setModeButtonActive('MANUAL');
      await fetch('/api/rgb', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({color: name, brightness: currentBrightness})
      });
      fetchStatus();
    }

    async function setCustomRgb(r, g, b) {
      setModeButtonActive('MANUAL');
      await fetch('/api/rgb', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({r: r, g: g, b: b, brightness: currentBrightness})
      });
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
      await fetch('/api/mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode})
      });
      fetchStatus();
    }

    // Initial load and periodic polling every 2 seconds
    fetchStatus();
    setInterval(fetchStatus, 2000);
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    b = BoardBridge("COM3", 115200)
    server = create_app(b)
    server.run(host="0.0.0.0", port=8080, debug=False)
