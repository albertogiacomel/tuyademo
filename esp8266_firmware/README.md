# 📡 Tuya TYWE1S (ESP8266EX) Standalone Wi-Fi Gateway Firmware

Questo firmware trasforma il modulo Wi-Fi on-board **TYWE1S** in un web server autonomo e gateway REST API.

---

## 🌟 Caratteristiche

* **Access Point Wi-Fi Standalone**: `Tuya-TYDE3-AP` (Password: `tuya123456`, IP: `192.168.4.1`).
* **Web Server HTTP integrato**: Pagina Web HTML5/CSS3 con cursore luminosità, bottoni colori e indicatori.
* **REST API completa**:
  * `GET /api/status`: restituisce telemetria, temperatura MCU, luminosità, colore attuale.
  * `GET /api/temp`: telemetria sensore temperatura core STM32.
  * `POST /api/rgb`: `{"r": 0, "g": 255, "b": 0, "brightness": 20}`.
  * `POST /api/brightness`: `{"brightness": 15}`.
  * `POST /api/mode`: `{"mode": "TEMP" | "RAINBOW" | "OFF"}`.
* **Comunicazione con STM32**: Protocollo ASCII su UART0 (`Serial` @ 115200) <-> USART3 STM32 (`PB10/PB11` via ponticello P3).

---

## 🚀 Compilazione e Flash con PlatformIO

1. Assicurarsi di aver eseguito prima il backup di fabbrica:
   ```bash
   python scripts/tuya_cli.py backup-esp
   ```
2. Compilare il firmware:
   ```bash
   pio run -d esp8266_firmware
   ```
3. Caricare via `esptool`:
   ```bash
   python -m esptool -p COM3 -b 115200 write_flash 0x0 esp8266_firmware/.pio/build/tywe1s_wifi/firmware.bin
   ```
