/**
 * @file main.cpp
 * @brief Standalone Wi-Fi Web Server & REST API for Tuya TYWE1S (ESP8266EX)
 * 
 * Target: TYWE1S Wi-Fi Module on Tuya TYDE 3.0 IoT Board
 * Hardware UART: Serial (GPIO1 TX, GPIO3 RX @ 115200) <-> STM32 USART3 (PB10/PB11)
 * Functions:
 * - Serves responsive HTML5 Web App on Port 80
 * - Serves complete REST API (/api/status, /api/temp, /api/rgb, /api/brightness, /api/mode)
 * - Captive Portal / Access Point "Tuya-TYDE3-AP" if Wi-Fi credentials not set
 */

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>

const char* AP_SSID = "Tuya-TYDE3-AP";
const char* AP_PASS = "tuya123456";

ESP8266WebServer server(80);

void handleApiWifiScan();

// Cached telemetry state from STM32
static float cached_temp = 25.0f;
static float cached_vdd = 3.30f;
static uint8_t cached_brightness = 20;
static String cached_mode = "TEMP";
static uint8_t cached_r = 0, cached_g = 255, cached_b = 30;

void sendCommandToStm32(const String& cmd) {
    Serial.println(cmd);
    Serial.flush();
}

void pollTemperatureFromStm32() {
    Serial.print("T");
    Serial.flush();
    uint32_t t0 = millis();
    while (!Serial.available() && (millis() - t0 < 80)) {
        delay(1);
    }
    if (Serial.available()) {
        String resp = Serial.readStringUntil('\n');
        resp.trim();
        // Expected: T:XXXX,V:YYYY,VDD:3.29V,TEMP:35.9C
        if (resp.startsWith("T:") && resp.indexOf(",V:") > 0) {
            int tempIdx = resp.indexOf("TEMP:");
            int vddIdx = resp.indexOf("VDD:");
            if (tempIdx > 0) {
                cached_temp = resp.substring(tempIdx + 5).toFloat();
            }
            if (vddIdx > 0) {
                cached_vdd = resp.substring(vddIdx + 4, tempIdx - 1).toFloat();
            }
        }
    }
}

void handleApiStatus() {
    pollTemperatureFromStm32();
    String json = "{";
    json += "\"online\":true,";
    json += "\"temp_c\":" + String(cached_temp, 1) + ",";
    json += "\"vdd\":" + String(cached_vdd, 2) + ",";
    json += "\"brightness\":" + String(cached_brightness) + ",";
    json += "\"mode\":\"" + cached_mode + "\",";
    json += "\"r\":" + String(cached_r) + ",";
    json += "\"g\":" + String(cached_g) + ",";
    json += "\"b\":" + String(cached_b);
    json += "}";
    server.send(200, "application/json", json);
}

void handleApiTemp() {
    pollTemperatureFromStm32();
    String json = "{";
    json += "\"ok\":true,";
    json += "\"temp_c\":" + String(cached_temp, 1) + ",";
    json += "\"vdd\":" + String(cached_vdd, 2);
    json += "}";
    server.send(200, "application/json", json);
}

void handleApiBrightness() {
    if (server.hasArg("plain")) {
        String body = server.arg("plain");
        int bIdx = body.indexOf("\"brightness\":");
        if (bIdx >= 0) {
            int val = body.substring(bIdx + 13).toInt();
            cached_brightness = constrain(val, 0, 100);
            sendCommandToStm32("C:B:" + String(cached_brightness));
            server.send(200, "application/json", "{\"ok\":true,\"brightness\":" + String(cached_brightness) + "}");
            return;
        }
    }
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"Missing brightness\"}");
}

void handleApiRgb() {
    if (server.hasArg("plain")) {
        String body = server.arg("plain");
        int r = 0, g = 0, b = 0;
        int rIdx = body.indexOf("\"r\":");
        int gIdx = body.indexOf("\"g\":");
        int bIdx = body.indexOf("\"b\":");
        int briIdx = body.indexOf("\"brightness\":");
        if (briIdx >= 0) {
            cached_brightness = constrain(body.substring(briIdx + 13).toInt(), 0, 100);
            sendCommandToStm32("C:B:" + String(cached_brightness));
        }
        if (rIdx >= 0 && gIdx >= 0 && bIdx >= 0) {
            r = constrain(body.substring(rIdx + 4).toInt(), 0, 255);
            g = constrain(body.substring(gIdx + 4).toInt(), 0, 255);
            b = constrain(body.substring(bIdx + 4).toInt(), 0, 255);
            cached_r = r; cached_g = g; cached_b = b;
            cached_mode = "MANUAL";
            sendCommandToStm32("C:" + String(r) + "," + String(g) + "," + String(b));
            server.send(200, "application/json", "{\"ok\":true,\"r\":" + String(r) + ",\"g\":" + String(g) + ",\"b\":" + String(b) + "}");
            return;
        }
    }
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"Invalid parameters\"}");
}

void handleApiMode() {
    if (server.hasArg("plain")) {
        String body = server.arg("plain");
        int mIdx = body.indexOf("\"mode\":");
        if (mIdx >= 0) {
            int q1 = body.indexOf('"', mIdx + 7);
            int q2 = body.indexOf('"', q1 + 1);
            if (q1 > 0 && q2 > q1) {
                String m = body.substring(q1 + 1, q2);
                m.toUpperCase();
                cached_mode = m;
                if (m == "OFF") {
                    sendCommandToStm32("C:OFF");
                } else if (m == "RAINBOW") {
                    sendCommandToStm32("C:RAINBOW");
                } else if (m == "TEMP") {
                    sendCommandToStm32("C:TEMP");
                }
                server.send(200, "application/json", "{\"ok\":true,\"mode\":\"" + m + "\"}");
                return;
            }
        }
    }
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"Invalid mode\"}");
}

void handleApiLed() {
    if (server.hasArg("plain")) {
        String body = server.arg("plain");
        int lIdx = body.indexOf("\"led\":");
        int mIdx = body.indexOf("\"mode\":");
        int pIdx = body.indexOf("\"period_ms\":");
        if (lIdx >= 0) {
            int led = body.substring(lIdx + 6).toInt();
            String mode = "BLINK";
            int period = 500;
            if (mIdx >= 0) {
                int q1 = body.indexOf('"', mIdx + 7);
                int q2 = body.indexOf('"', q1 + 1);
                if (q1 > 0 && q2 > q1) mode = body.substring(q1 + 1, q2);
            }
            if (pIdx >= 0) {
                period = body.substring(pIdx + 12).toInt();
            }
            mode.toUpperCase();
            sendCommandToStm32("L:" + String(led) + "," + mode + "," + String(period));
            server.send(200, "application/json", "{\"ok\":true}");
            return;
        }
    }
    server.send(400, "application/json", "{\"ok\":false}");
}

// Redirect root to dashboard (stored in PROGMEM)
extern const char INDEX_HTML[] PROGMEM;
void handleRoot() {
    server.send_P(200, "text/html; charset=utf-8", INDEX_HTML);
}

const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tuya TYWE1S Wi-Fi Controller</title>
<style>
body{background:#0b0f19;color:#f0f6fc;font-family:sans-serif;padding:20px;text-align:center}
.card{background:#161f31;border-radius:16px;padding:20px;margin:12px auto;max-width:480px;box-shadow:0 4px 20px rgba(0,0,0,0.4)}
.orb{width:80px;height:80px;border-radius:50%;margin:16px auto;box-shadow:0 0 30px #38bdf8;background:#38bdf8}
button{background:#38bdf8;border:none;color:#0b0f19;padding:12px 20px;border-radius:10px;font-weight:700;margin:6px;cursor:pointer}
input[type=range]{width:90%;margin:15px 0}
</style>
</head>
<body>
<div class="card">
  <h2>💡 Tuya TYDE 3.0 Wi-Fi</h2>
  <div id="orb" class="orb"></div>
  <p>Temp: <b id="t">--.- °C</b> | VDD: <b id="v">-- V</b></p>
  <p>Luminosità: <b id="bv">20%</b></p>
  <input type="range" min="5" max="100" value="20" oninput="setBri(this.value)">
  <div>
    <button onclick="setColor(255,0,0)">Rosso</button>
    <button onclick="setColor(0,255,0)">Verde</button>
    <button onclick="setColor(0,0,255)">Blu</button>
    <button onclick="setMode('TEMP')">Termometro</button>
    <button onclick="setMode('RAINBOW')">Rainbow</button>
    <button onclick="setMode('OFF')">Off</button>
  </div>
</div>
<script>
async function poll(){
  try{
    let r=await fetch('/api/status');let d=await r.json();
    document.getElementById('t').innerText=d.temp_c+' °C';
    document.getElementById('v').innerText=d.vdd+' V';
    document.getElementById('bv').innerText=d.brightness+'%';
    document.getElementById('orb').style.background='rgb('+d.r+','+d.g+','+d.b+')';
  }catch(e){}
}
async function setBri(b){
  document.getElementById('bv').innerText=b+'%';
  await fetch('/api/brightness',{method:'POST',body:JSON.stringify({brightness:parseInt(b)})});
}
async function setColor(r,g,b){
  await fetch('/api/rgb',{method:'POST',body:JSON.stringify({r:r,g:g,b:b})});
  poll();
}
async function setMode(m){
  await fetch('/api/mode',{method:'POST',body:JSON.stringify({mode:m})});
  poll();
}
setInterval(poll,2000);poll();
</script>
</body>
</html>
)rawliteral";

void handleApiWifiScan() {
    int n = WiFi.scanNetworks(false, true);
    String json = "[";
    for (int i = 0; i < n; i++) {
        if (i > 0) json += ",";
        int rssi = WiFi.RSSI(i);
        int signal = constrain((rssi + 100) * 2, 5, 100);
        json += "{\"ssid\":\"" + WiFi.SSID(i) + "\",";
        json += "\"rssi\":" + String(rssi) + ",";
        json += "\"signal\":" + String(signal) + ",";
        json += "\"secure\":" + String(WiFi.encryptionType(i) != ENC_TYPE_NONE ? "true" : "false") + "}";
    }
    json += "]";
    server.send(200, "application/json", json);
}

// Candidate GPIO pins on TYWE1S for LED3 (Active Low)
static const uint8_t LED_PINS[] = {4, 5, 12, 13, 14};

void handleApiWifiDisconnect() {
    WiFi.disconnect();
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.println("WIFI:DISCONNECTED");
    server.send(200, "application/json", "{\"ok\":true,\"message\":\"Disconnesso da LAN. Wi-Fi SoftAP Tuya-TYDE3-AP attivo per pairing con cellulare.\"}");
}

void setup() {
    Serial.begin(115200);
    delay(100);

    // Setup LED pins (Active Low: HIGH = OFF initially)
    for (uint8_t p : LED_PINS) {
        pinMode(p, OUTPUT);
        digitalWrite(p, HIGH);
    }

    // Setup Soft Access Point
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASS);

    // REST API Routes
    server.on("/", HTTP_GET, handleRoot);
    server.on("/api/status", HTTP_GET, handleApiStatus);
    server.on("/api/temp", HTTP_GET, handleApiTemp);
    server.on("/api/rgb", HTTP_POST, handleApiRgb);
    server.on("/api/brightness", HTTP_POST, handleApiBrightness);
    server.on("/api/mode", HTTP_POST, handleApiMode);
    server.on("/api/wifi/scan", HTTP_GET, handleApiWifiScan);
    server.on("/api/wifi/disconnect", HTTP_POST, handleApiWifiDisconnect);
    server.on("/api/led", HTTP_POST, handleApiLed);

    server.begin();
}

void loop() {
    server.handleClient();

    // Wi-Fi Status Indicator on LED3:
    // If connected -> Solid ON (LOW). If not connected -> Blink every 0.5s (500ms).
    bool is_conn = (WiFi.status() == WL_CONNECTED);
    static bool last_conn_state = false;
    if (is_conn != last_conn_state) {
        last_conn_state = is_conn;
        Serial.println(is_conn ? "WIFI:CONNECTED" : "WIFI:DISCONNECTED");
    }

    if (is_conn) {
        for (uint8_t p : LED_PINS) digitalWrite(p, LOW); // Active Low: Solid ON
    } else {
        bool blk = ((millis() / 500) % 2 == 0);
        for (uint8_t p : LED_PINS) digitalWrite(p, blk ? LOW : HIGH); // 0.5s Blink
    }

    // Check for commands from STM32 over Serial (USART3)
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();
        if (line.equalsIgnoreCase("SCAN") || line.equalsIgnoreCase("W:SCAN")) {
            int n = WiFi.scanNetworks(false, true);
            for (int i = 0; i < n; i++) {
                Serial.printf("SSID:%s,RSSI:%d\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i));
            }
        } else if (line.equalsIgnoreCase("W:DISCONNECT") || line.equalsIgnoreCase("W:RESET")) {
            WiFi.disconnect();
            WiFi.mode(WIFI_AP_STA);
            WiFi.softAP(AP_SSID, AP_PASS);
            Serial.println("WIFI:DISCONNECTED");
        } else if (line.startsWith("W:") || line.startsWith("w:")) {
            // Format: W:<ssid>,<password>
            int comma = line.indexOf(',');
            if (comma > 2) {
                String s = line.substring(2, comma);
                String p = line.substring(comma + 1);
                WiFi.begin(s.c_str(), p.c_str());
                Serial.println("WIFI:CONNECTING");
            }
        }
    }
}
