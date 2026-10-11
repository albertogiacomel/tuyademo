/**
 * @file main.cpp
 * @brief Smart UART Bridge & Advanced RGB/Sensors Firmware with Tuya MCU Protocol for Tuya TYDE 3.0
 * 
 * Target MCU: STM32F103C8T6 (72MHz ARM Cortex-M3)
 * Exact Pinout reverse-engineered from factory firmware:
 * - Status LEDs (Active Low): PB6 (LED1), PB5 (LED2)
 * - LED3: Physically driven on TYWE1S module via Tuya Wi-Fi state protocol (PB4/PC13 fallback)
 * - RGB LED (Active Low PWM on TIM4): PB8 (Red), PB9 (Green), PB7 (Blue)
 * - User Buttons (Active Low): PA11 (Key1), PA12 (Key2)
 * - Serial: PA9/PA10 (USART1 @ 115200 to PC USB), PB10/PB11 (USART3 @ 9600 to TYWE1S Tuya module)
 */

#include <Arduino.h>
#include "board_pins.h"

enum RgbMode {
    RGB_MODE_MANUAL = 0,
    RGB_MODE_TEMP,
    RGB_MODE_RAINBOW,
    RGB_MODE_OFF
};

static RgbMode current_rgb_mode = RGB_MODE_TEMP; // Default: Visual Core Temperature Monitor
static uint32_t last_temp_update = 0;
static uint32_t last_rainbow_update = 0;
static uint16_t rainbow_hue = 0;
static bool last_key2_state = HIGH;
static float current_temp_c = 25.0f;

// --- Wi-Fi Connection & LED3 Status Tracking ---
static bool wifi_connected = false;
static uint8_t tuya_wifi_state = 0xFF; // 0=SmartConfig/EZ, 1=AP, 2=Connecting, 3=Router, 4=Cloud, 5=Timeout
static uint32_t last_tuya_heartbeat_sent = 0;
static uint32_t last_tuya_heartbeat_recv = 0;

// --- Individual Control for the 3 On-Board Status LEDs ---
struct StatusLed {
    uint8_t mode;        // 0 = OFF, 1 = ON (Solid), 2 = BLINK
    uint16_t period_ms;  // Blink period in milliseconds (toggle interval)
    uint32_t last_toggle;
    bool state;
};

static StatusLed leds[3] = {
    {2, 500, 0, false}, // LED 1 (PB6)
    {1, 500, 0, true},  // LED 2 (PB5)
    {2, 500, 0, false}  // LED 3 (TYWE1S / PB4 / PC13)
};

void set_led_ctrl(uint8_t id, uint8_t mode, uint16_t period_ms) {
    if (id < 1 || id > 3) return;
    uint8_t idx = id - 1;
    leds[idx].mode = mode;
    leds[idx].period_ms = period_ms > 0 ? period_ms : 500;
    leds[idx].state = (mode == 1);

    uint8_t lvl = (mode == 1) ? LOW : HIGH;
    if (id == 1) {
        digitalWrite(PIN_LED1, lvl);
    } else if (id == 2) {
        digitalWrite(PIN_LED2, lvl);
    } else if (id == 3) {
        digitalWrite(PB4, lvl);
        digitalWrite(PB3, lvl);
        digitalWrite(PC13, lvl);
        wifi_connected = (mode == 1);
    }
}

// --- Brightness Control State ---
static uint8_t current_brightness = 20; // Default 20% (comfortable, not blinding)
static int8_t brightness_dir = -1;      // -1 = dimming, +1 = brightening
static uint32_t key1_press_start = 0;
static bool key1_is_held = false;
static uint32_t last_brightness_ramp = 0;
static bool last_key1_state = HIGH;

// Current base RGB values before brightness scaling
static uint8_t cur_r = 0, cur_g = 0, cur_b = 0;

// CIE 1931 perceptual lightness table for 12-bit PWM (0..4095) across 0..100% brightness
static const uint16_t CIE_12BIT[101] = {
       0,    5,    9,   14,   18,   23,   27,   32,   36,   41,
      46,   52,   58,   64,   71,   78,   86,   94,  103,  112,
     122,  133,  144,  156,  168,  181,  194,  209,  223,  239,
     255,  272,  290,  309,  328,  348,  369,  391,  413,  436,
     461,  486,  512,  539,  567,  595,  625,  656,  688,  720,
     754,  789,  825,  862,  900,  939,  979, 1021, 1063, 1107,
    1152, 1198, 1245, 1293, 1343, 1394, 1447, 1500, 1555, 1611,
    1669, 1728, 1788, 1849, 1913, 1977, 2043, 2110, 2179, 2249,
    2321, 2394, 2469, 2546, 2623, 2703, 2784, 2867, 2951, 3037,
    3125, 3214, 3305, 3397, 3492, 3588, 3686, 3785, 3887, 3990,
    4095
};

static inline void write_channel_pwm(pin_size_t pin, uint16_t pwm_val) {
    if (pwm_val == 0) {
        // Active Low: 0% brightness -> pin driven solidly HIGH (completely OFF)
        pinMode(pin, OUTPUT);
        digitalWrite(pin, HIGH);
    } else if (pwm_val >= 4095) {
        // Active Low: 100% brightness -> pin driven solidly LOW (maximum ON)
        pinMode(pin, OUTPUT);
        digitalWrite(pin, LOW);
    } else {
        // Active Low 12-bit PWM: 4095 - val on TIM4
        analogWrite(pin, 4095 - pwm_val);
    }
}

void apply_rgb_brightness() {
    uint8_t bri = constrain(current_brightness, 0, 100);
    uint32_t scale = CIE_12BIT[bri]; // 0..4095

    uint16_t pwm_r = (uint32_t)cur_r * scale / 255;
    uint16_t pwm_g = (uint32_t)cur_g * scale / 255;
    uint16_t pwm_b = (uint32_t)cur_b * scale / 255;

    write_channel_pwm(PIN_RGB_RED, pwm_r);
    write_channel_pwm(PIN_RGB_GREEN, pwm_g);
    write_channel_pwm(PIN_RGB_BLUE, pwm_b);
}

void set_rgb_pwm(uint8_t r, uint8_t g, uint8_t b) {
    cur_r = r;
    cur_g = g;
    cur_b = b;
    apply_rgb_brightness();
}

void update_temp_color(float temp_c) {
    if (temp_c < 32.0f) {
        set_rgb_pwm(0, 50, 255); // Cool Blue
    } else if (temp_c < 36.0f) {
        set_rgb_pwm(0, 255, 200); // Cyan
    } else if (temp_c < 42.0f) {
        set_rgb_pwm(0, 255, 30); // Vibrant Green (Normal operational range)
    } else if (temp_c < 50.0f) {
        set_rgb_pwm(255, 180, 0); // Warm Yellow/Amber
    } else {
        set_rgb_pwm(255, 0, 0); // Warning Red
    }
}

// Convert HSV hue (0..359) to RGB (0..255)
void hue_to_rgb(uint16_t hue, uint8_t &r, uint8_t &g, uint8_t &b) {
    uint8_t region = hue / 60;
    uint8_t remainder = (hue - (region * 60)) * 255 / 60;
    uint8_t q = 255 - remainder;
    uint8_t t = remainder;

    switch (region) {
        case 0:  r = 255; g = t;   b = 0;   break;
        case 1:  r = q;   g = 255; b = 0;   break;
        case 2:  r = 0;   g = 255; b = t;   break;
        case 3:  r = 0;   g = q;   b = 255; break;
        case 4:  r = t;   g = 0;   b = 255; break;
        default: r = 255; g = 0;   b = q;   break;
    }
}

float measure_temperature(int &out_raw_temp, int &out_raw_vref, float &out_vdd) {
    out_raw_temp = analogRead(ATEMP);
    out_raw_vref = analogRead(AVREF);

    out_vdd = (out_raw_vref > 0) ? (1.20f * 4096.0f / (float)out_raw_vref) : 3.30f;
    float vsense = ((float)out_raw_temp / 4096.0f) * out_vdd;
    return ((1.43f - vsense) / 0.0043f) + 25.0f;
}

void jump_to_bootloader() {
    Serial.print("REBOOT_BOOTLOADER\r\n");
    Serial.flush();
    delay(50);

    // AN2606 clean hardware de-init
    __disable_irq();
    SysTick->CTRL = 0;
    SysTick->LOAD = 0;
    SysTick->VAL = 0;

    // Reset RCC to default HSI 8MHz state
    RCC->CR |= RCC_CR_HSION;
    while (!(RCC->CR & RCC_CR_HSIRDY));
    RCC->CFGR = 0;
    RCC->CR &= ~(RCC_CR_PLLON | RCC_CR_HSEON | RCC_CR_CSSON);

    // Reset all peripherals via RCC reset registers
    RCC->APB1RSTR = 0xFFFFFFFF;
    RCC->APB1RSTR = 0x00000000;
    RCC->APB2RSTR = 0xFFFFFFFF;
    RCC->APB2RSTR = 0x00000000;

    // Jump to ST System Memory Bootloader (0x1FFFF000)
    void (*SysMemBootJump)(void) = (void (*)(void))(*((uint32_t *)0x1FFFF004));
    __set_MSP(*((uint32_t *)0x1FFFF000));
    SysMemBootJump();
}

void enter_pure_passthrough() {
    Serial.print("PASSTHROUGH_ACTIVE\r\n");
    Serial.flush();
    digitalWrite(PIN_LED2, LOW); // Active Low: LED2 ON solid to indicate passthrough mode

    // Disable UART interrupts so this tight 72MHz hardware register loop
    // has 100% exclusive access without HAL ISR interference.
    USART1->CR1 &= ~(USART_CR1_RXNEIE | USART_CR1_TXEIE | USART_CR1_TCIE | USART_CR1_PEIE);
    USART3->CR1 &= ~(USART_CR1_RXNEIE | USART_CR1_TXEIE | USART_CR1_TCIE | USART_CR1_PEIE);
    NVIC_DisableIRQ(USART1_IRQn);
    NVIC_DisableIRQ(USART3_IRQn);

    while (true) {
        // Forward PC (USART1) -> TYWE1S (USART3)
        uint32_t sr1 = USART1->SR;
        if (sr1 & USART_SR_RXNE) {
            uint8_t b = (uint8_t)(USART1->DR & 0xFF);
            while (!(USART3->SR & USART_SR_TXE));
            USART3->DR = b;
        } else if (sr1 & USART_SR_ORE) {
            volatile uint32_t dr1 = USART1->DR;
            (void)dr1;
        }

        // Forward TYWE1S (USART3) -> PC (USART1)
        uint32_t sr3 = USART3->SR;
        if (sr3 & USART_SR_RXNE) {
            uint8_t b = (uint8_t)(USART3->DR & 0xFF);
            while (!(USART1->SR & USART_SR_TXE));
            USART1->DR = b;
        } else if (sr3 & USART_SR_ORE) {
            volatile uint32_t dr3 = USART3->DR;
            (void)dr3;
        }
    }
}

// ==============================================================================
// TUYA MCU PROTOCOL ENGINE (USART3 @ 9600 BAUD)
// ==============================================================================

// Tuya Frame Header: 0x55 0xAA, Version: 0x00
void tuya_send_frame(uint8_t cmd, const uint8_t* payload, uint16_t len) {
    Serial3.write((uint8_t)0x55);
    Serial3.write((uint8_t)0xAA);
    Serial3.write((uint8_t)0x00); // Version 0x00
    Serial3.write(cmd);
    Serial3.write((uint8_t)(len >> 8));
    Serial3.write((uint8_t)(len & 0xFF));

    uint8_t chk = (uint8_t)(0x55 + 0xAA + 0x00 + cmd + (len >> 8) + (len & 0xFF));
    for (uint16_t i = 0; i < len; i++) {
        Serial3.write(payload[i]);
        chk += payload[i];
    }
    Serial3.write(chk);
    Serial3.flush();
}

void tuya_start_pairing_ap() {
    // CMD 0x04: Reset Wi-Fi credentials on TYWE1S
    tuya_send_frame(0x04, nullptr, 0);
    delay(40);
    // CMD 0x05: Select AP mode (0x01) -> Hotspot SmartLife-xxxx active
    uint8_t m = 1;
    tuya_send_frame(0x05, &m, 1);
    wifi_connected = false;
    tuya_wifi_state = 1;
    set_led_ctrl(3, 2, 500); // Blink 0.5s
    set_led_ctrl(1, 2, 500);
}

void tuya_start_pairing_ez() {
    // CMD 0x04: Reset Wi-Fi credentials on TYWE1S
    tuya_send_frame(0x04, nullptr, 0);
    delay(40);
    // CMD 0x05: Select EZ / SmartConfig mode (0x00) -> Rapid blink
    uint8_t m = 0;
    tuya_send_frame(0x05, &m, 1);
    wifi_connected = false;
    tuya_wifi_state = 0;
    set_led_ctrl(3, 2, 250); // Rapid blink
    set_led_ctrl(1, 2, 250);
}

void tuya_query_wifi_state() {
    tuya_send_frame(0x03, nullptr, 0);
}

void tuya_handle_rx_frame(uint8_t cmd, const uint8_t* payload, uint16_t len) {
    switch (cmd) {
        case 0x00: // Heartbeat response from TYWE1S
            last_tuya_heartbeat_recv = millis();
            break;

        case 0x01: { // Product Information query
            // Exact factory response: PID "qWGOYA4uqW9odEQk" (16 bytes) + version "3.0.3" (5 bytes) = 21 bytes
            const uint8_t prod_info[] = "qWGOYA4uqW9odEQk3.0.3";
            tuya_send_frame(0x01, prod_info, 21);
            Serial.print("[TUYA] INFO_REPLIED:qWGOYA4uqW9odEQk3.0.3\r\n");
            break;
        }

        case 0x02: { // Working Mode query
            // Length 0: cooperative network mode
            tuya_send_frame(0x02, nullptr, 0);
            Serial.print("[TUYA] WORK_MODE_REPLIED\r\n");
            break;
        }

        case 0x03: { // Wi-Fi Network Status report from TYWE1S
            if (len >= 1) {
                tuya_wifi_state = payload[0];
                if (tuya_wifi_state == 3 || tuya_wifi_state == 4) {
                    // Connected to Router (3) or Tuya Cloud (4)
                    wifi_connected = true;
                    set_led_ctrl(3, 1, 0); // Solid ON
                    set_led_ctrl(1, 1, 0);
                    Serial.print("WIFI:CONNECTED,STATE:");
                    Serial.print(tuya_wifi_state);
                    Serial.print("\r\n");
                } else {
                    // 0 = EZ/SmartConfig, 1 = AP pairing, 2 = Connecting, 5 = Timeout
                    wifi_connected = false;
                    uint16_t period = (tuya_wifi_state == 0) ? 250 : 500;
                    set_led_ctrl(3, 2, period); // Blink 0.5s or 0.25s
                    set_led_ctrl(1, 2, period);
                    Serial.print("WIFI:DISCONNECTED,STATE:");
                    Serial.print(tuya_wifi_state);
                    Serial.print("\r\n");
                }
            }
            // Reply ACK length 0 to report
            tuya_send_frame(0x03, nullptr, 0);
            break;
        }

        case 0x04: // Reset Wi-Fi ACK
            wifi_connected = false;
            tuya_wifi_state = 1;
            set_led_ctrl(3, 2, 500);
            Serial.print("WIFI:RESET_ACK\r\n");
            break;

        case 0x05: // Wi-Fi Mode ACK
            Serial.print("WIFI:MODE_ACK\r\n");
            break;

        case 0x06: // DP Command from Cloud/App (Downlink)
            // Send ACK length 0
            tuya_send_frame(0x06, nullptr, 0);
            Serial.print("[TUYA] DP_DOWNLINK\r\n");
            break;

        default:
            break;
    }
}

// State machine for Tuya protocol receiver
enum TuyaParserState {
    TP_WAIT_55 = 0,
    TP_WAIT_AA,
    TP_WAIT_VER,
    TP_WAIT_CMD,
    TP_WAIT_LEN_H,
    TP_WAIT_LEN_L,
    TP_WAIT_DATA,
    TP_WAIT_CHK
};

static TuyaParserState tuya_state = TP_WAIT_55;
static uint8_t tuya_rx_ver = 0;
static uint8_t tuya_rx_cmd = 0;
static uint16_t tuya_rx_len = 0;
static uint16_t tuya_rx_idx = 0;
static uint8_t tuya_rx_buf[256];
static uint8_t tuya_calc_chk = 0;

void process_tuya_serial() {
    while (Serial3.available()) {
        uint8_t b = (uint8_t)Serial3.read();
        switch (tuya_state) {
            case TP_WAIT_55:
                if (b == 0x55) {
                    tuya_state = TP_WAIT_AA;
                    tuya_calc_chk = 0x55;
                }
                break;

            case TP_WAIT_AA:
                if (b == 0xAA) {
                    tuya_state = TP_WAIT_VER;
                    tuya_calc_chk += 0xAA;
                } else if (b == 0x55) {
                    tuya_calc_chk = 0x55; // Keep waiting for AA
                } else {
                    tuya_state = TP_WAIT_55;
                }
                break;

            case TP_WAIT_VER:
                tuya_rx_ver = b;
                tuya_calc_chk += b;
                tuya_state = TP_WAIT_CMD;
                break;

            case TP_WAIT_CMD:
                tuya_rx_cmd = b;
                tuya_calc_chk += b;
                tuya_state = TP_WAIT_LEN_H;
                break;

            case TP_WAIT_LEN_H:
                tuya_rx_len = ((uint16_t)b) << 8;
                tuya_calc_chk += b;
                tuya_state = TP_WAIT_LEN_L;
                break;

            case TP_WAIT_LEN_L:
                tuya_rx_len |= b;
                tuya_calc_chk += b;
                tuya_rx_idx = 0;
                if (tuya_rx_len == 0) {
                    tuya_state = TP_WAIT_CHK;
                } else if (tuya_rx_len > sizeof(tuya_rx_buf)) {
                    tuya_state = TP_WAIT_55; // Buffer overflow safeguard
                } else {
                    tuya_state = TP_WAIT_DATA;
                }
                break;

            case TP_WAIT_DATA:
                tuya_rx_buf[tuya_rx_idx++] = b;
                tuya_calc_chk += b;
                if (tuya_rx_idx >= tuya_rx_len) {
                    tuya_state = TP_WAIT_CHK;
                }
                break;

            case TP_WAIT_CHK:
                if (b == tuya_calc_chk) {
                    tuya_handle_rx_frame(tuya_rx_cmd, tuya_rx_buf, tuya_rx_len);
                }
                tuya_state = TP_WAIT_55;
                break;
        }
    }
}

// ==============================================================================
// SETUP & MAIN LOOP
// ==============================================================================

void setup() {
    // Release PB4 (JNTRST) and PB3 (JTDO) from JTAG while keeping SWD debugging active
    __HAL_RCC_AFIO_CLK_ENABLE();
    __HAL_AFIO_REMAP_SWJ_NOJTAG();

    // Configure status LEDs (Active Low)
    pinMode(PIN_LED1, OUTPUT);
    pinMode(PIN_LED2, OUTPUT);
    pinMode(PB4, OUTPUT);
    pinMode(PB3, OUTPUT);
    pinMode(PC13, OUTPUT);
    digitalWrite(PIN_LED1, HIGH); // OFF initially
    digitalWrite(PIN_LED2, LOW);  // ON (MCU Alive)
    digitalWrite(PB4, HIGH);
    digitalWrite(PB3, HIGH);
    digitalWrite(PC13, HIGH);

    // Configure user buttons
    pinMode(PIN_KEY1, INPUT_PULLUP);
    pinMode(PIN_KEY2, INPUT_PULLUP);

    // Configure RGB LED pins on TIM4 with 12-bit PWM resolution (0..4095)
    analogWriteResolution(12);
    pinMode(PIN_RGB_RED, OUTPUT);
    pinMode(PIN_RGB_GREEN, OUTPUT);
    pinMode(PIN_RGB_BLUE, OUTPUT);
    set_rgb_pwm(0, 0, 0); // Initially off

    // Initialize ADC for 12-bit internal temperature sensor & VREFINT
    analogReadResolution(12);

    // Initialize Serial1 (PC USB @ 115200)
    Serial.setRx(PIN_UART1_RX);
    Serial.setTx(PIN_UART1_TX);
    Serial.begin(115200);

    // Initialize Serial3 (TYWE1S module @ 115200 BAUD)
    Serial3.setRx(PIN_UART3_RX);
    Serial3.setTx(PIN_UART3_TX);
    Serial3.begin(115200);

    // Initial temperature measurement
    int rt, rv; float vdd;
    current_temp_c = measure_temperature(rt, rv, vdd);
    update_temp_color(current_temp_c);
}

void loop() {
    uint32_t now = millis();

    // --- 1. Forward from TYWE1S (Serial3 @ 115200) to PC (Serial) & Track WiFi state ---
    static String s3_line = "";
    while (Serial3.available()) {
        int sc = Serial3.read();
        Serial.write((uint8_t)sc);
        if (sc == '\n') {
            s3_line.trim();
            if (s3_line.indexOf("WIFI:CONNECTED") >= 0) {
                wifi_connected = true;
                set_led_ctrl(3, 1, 500); // Solid ON
                set_led_ctrl(1, 1, 500);
            } else if (s3_line.indexOf("WIFI:DISCONNECTED") >= 0) {
                wifi_connected = false;
                set_led_ctrl(3, 2, 500); // Blink 0.5s
                set_led_ctrl(1, 2, 500);
            }
            s3_line = "";
        } else if (sc != '\r') {
            if (s3_line.length() < 96) s3_line += (char)sc;
        }
    }

    // --- 2. Process Commands from PC USB (Serial @ 115200 baud) ---
    while (Serial.available()) {
        int c = Serial.peek();
        if (c == '?') {
            Serial.read();
            Serial.print("OK\r\n");
            continue;
        } else if (c == 'T' || c == 't') {
            Serial.read();
            int rt, rv; float vdd;
            float temp = measure_temperature(rt, rv, vdd);
            current_temp_c = temp;
            if (current_rgb_mode == RGB_MODE_TEMP) {
                update_temp_color(temp);
            }
            char buf[64];
            snprintf(buf, sizeof(buf), "T:%04X,V:%04X,VDD:%.2fV,TEMP:%.1fC\r\n", 
                     rt, rv, vdd, temp);
            Serial.print(buf);
            continue;
        } else if (c == 'C' || c == 'c') {
            // RGB command: C:<r>,<g>,<b> or C:TEMP or C:RAINBOW or C:OFF or C:WIFI:<0|1> or C:B:<val>
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd.startsWith("C:") || cmd.startsWith("c:")) {
                String val = cmd.substring(2);
                val.toUpperCase();
                if (val == "TEMP") {
                    current_rgb_mode = RGB_MODE_TEMP;
                    update_temp_color(current_temp_c);
                    Serial.print("RGB:MODE_TEMP\r\n");
                } else if (val == "RAINBOW") {
                    current_rgb_mode = RGB_MODE_RAINBOW;
                    Serial.print("RGB:MODE_RAINBOW\r\n");
                } else if (val == "OFF") {
                    current_rgb_mode = RGB_MODE_OFF;
                    set_rgb_pwm(0, 0, 0);
                    Serial.print("RGB:OFF\r\n");
                } else if (val.startsWith("WIFI:") || val.startsWith("W:")) {
                    int w_val = val.substring(val.indexOf(':') + 1).toInt();
                    wifi_connected = (w_val == 1);
                    set_led_ctrl(3, wifi_connected ? 1 : 2, 500);
                    Serial.print(wifi_connected ? "WIFI:OK:CONNECTED\r\n" : "WIFI:OK:DISCONNECTED\r\n");
                } else if (val.startsWith("BRIGHT:") || val.startsWith("B:")) {
                    int b_idx = val.indexOf(':');
                    int b_val = val.substring(b_idx + 1).toInt();
                    current_brightness = constrain(b_val, 0, 100);
                    apply_rgb_brightness();
                    char b_buf[32];
                    snprintf(b_buf, sizeof(b_buf), "RGB:BRIGHT:%d\r\n", current_brightness);
                    Serial.print(b_buf);
                } else {
                    // Parse r,g,b
                    int first_comma = val.indexOf(',');
                    int second_comma = val.indexOf(',', first_comma + 1);
                    if (first_comma > 0 && second_comma > first_comma) {
                        int r = val.substring(0, first_comma).toInt();
                        int g = val.substring(first_comma + 1, second_comma).toInt();
                        int b = val.substring(second_comma + 1).toInt();
                        current_rgb_mode = RGB_MODE_MANUAL;
                        set_rgb_pwm(constrain(r, 0, 255), constrain(g, 0, 255), constrain(b, 0, 255));
                        Serial.print("RGB:OK\r\n");
                    } else {
                        Serial.print("RGB:ERR\r\n");
                    }
                }
                continue;
            }
        } else if (c == 'L' || c == 'l') {
            // LED command: L:<id>,<mode>,<period> or L:?
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd.equalsIgnoreCase("L:?")) {
                char qbuf[96];
                snprintf(qbuf, sizeof(qbuf), "LED:1=%d,%d;2=%d,%d;3=%d,%d\r\n",
                         leds[0].mode, leds[0].period_ms,
                         leds[1].mode, leds[1].period_ms,
                         leds[2].mode, leds[2].period_ms);
                Serial.print(qbuf);
            } else if (cmd.startsWith("L:") || cmd.startsWith("l:")) {
                String val = cmd.substring(2);
                int first_comma = val.indexOf(',');
                int second_comma = val.indexOf(',', first_comma + 1);
                if (first_comma > 0) {
                    int id = val.substring(0, first_comma).toInt();
                    String m_str = (second_comma > 0) ? val.substring(first_comma + 1, second_comma) : val.substring(first_comma + 1);
                    int period = (second_comma > 0) ? val.substring(second_comma + 1).toInt() : 500;
                    m_str.toUpperCase();
                    m_str.trim();
                    uint8_t mode = 0;
                    if (m_str == "ON" || m_str == "1") mode = 1;
                    else if (m_str == "BLINK" || m_str == "2") mode = 2;
                    else mode = 0;
                    set_led_ctrl(id, mode, period);
                    Serial.print("LED:OK\r\n");
                } else {
                    Serial.print("LED:ERR\r\n");
                }
            }
            continue;
        } else if (c == 'W' || c == 'w') {
            // Wi-Fi commands
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd.equalsIgnoreCase("W:?") || cmd.equalsIgnoreCase("W:STATUS")) {
                tuya_query_wifi_state();
                if (wifi_connected) {
                    Serial.print("WIFI:CONNECTED,STATE:");
                    Serial.print(tuya_wifi_state);
                    Serial.print(",LED3:SOLID_ON\r\n");
                } else {
                    Serial.print("WIFI:DISCONNECTED,STATE:");
                    Serial.print(tuya_wifi_state);
                    Serial.print(",LED3:BLINK_0_5S\r\n");
                }
                continue;
            } else if (cmd.equalsIgnoreCase("W:PAIR:AP") || cmd.equalsIgnoreCase("W:DISCONNECT") || cmd.equalsIgnoreCase("W:RESET")) {
                tuya_start_pairing_ap();
                continue;
            } else if (cmd.equalsIgnoreCase("W:PAIR:EZ") || cmd.equalsIgnoreCase("W:PAIR")) {
                tuya_start_pairing_ez();
                continue;
            } else if (cmd.startsWith("W:") || cmd.startsWith("w:")) {
                // Config sent from Web dashboard: trigger AP pairing mode so TYWE1S is visible for phone pairing
                tuya_start_pairing_ap();
                Serial.print("WIFI:CONFIG_SENT\r\n");
                continue;
            }
        } else if (c == 'P' || c == 'p') {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd.equalsIgnoreCase("P") || cmd.equalsIgnoreCase("PASSTHROUGH")) {
                enter_pure_passthrough();
                return;
            }
        } else if (c == 'B') {
            Serial.read();
            jump_to_bootloader();
            return;
        }
        
        // Pass any other byte (esptool / raw traffic) to TYWE1S
        c = Serial.read();
        Serial3.write((uint8_t)c);
    }

    // --- 3. Individual Status LEDs 1, 2, 3 Scheduler ---
    for (int i = 0; i < 3; i++) {
        if (leds[i].mode == 2) {
            if (now - leds[i].last_toggle >= leds[i].period_ms) {
                leds[i].last_toggle = now;
                leds[i].state = !leds[i].state;
                uint8_t lvl = leds[i].state ? LOW : HIGH;
                if (i == 0) digitalWrite(PIN_LED1, lvl);
                else if (i == 1) digitalWrite(PIN_LED2, lvl);
                else if (i == 2) {
                    digitalWrite(PB4, lvl);
                    digitalWrite(PB3, lvl);
                    digitalWrite(PC13, lvl);
                }
            }
        }
    }

    // --- 4. Periodic Temperature Update in TEMP mode (every 1s) ---
    if (current_rgb_mode == RGB_MODE_TEMP && (now - last_temp_update >= 1000)) {
        last_temp_update = now;
        int rt, rv; float vdd;
        current_temp_c = measure_temperature(rt, rv, vdd);
        update_temp_color(current_temp_c);
    }

    // --- 5. Smooth Rainbow Cycle in RAINBOW mode (every 20ms) ---
    if (current_rgb_mode == RGB_MODE_RAINBOW && (now - last_rainbow_update >= 20)) {
        last_rainbow_update = now;
        rainbow_hue = (rainbow_hue + 1) % 360;
        uint8_t r, g, b;
        hue_to_rgb(rainbow_hue, r, g, b);
        set_rgb_pwm(r, g, b);
    }

    // --- 6. User Button 2 (PA12): Cycle RGB modes physically ---
    bool key2 = digitalRead(PIN_KEY2);
    if (key2 == LOW && last_key2_state == HIGH) {
        delay(25); // Debounce
        current_rgb_mode = (RgbMode)((current_rgb_mode + 1) % 4);
        switch (current_rgb_mode) {
            case RGB_MODE_TEMP:
                update_temp_color(current_temp_c);
                break;
            case RGB_MODE_RAINBOW:
                break;
            case RGB_MODE_MANUAL:
                set_rgb_pwm(255, 255, 255); // White in manual mode
                break;
            case RGB_MODE_OFF:
                set_rgb_pwm(0, 0, 0);
                break;
        }
    }
    last_key2_state = key2;

    // --- 7. User Button 1 (PA11): Hold 3s to enter Wi-Fi Pairing, ramp brightness, or step on click ---
    bool key1 = digitalRead(PIN_KEY1);
    if (key1 == LOW && last_key1_state == HIGH) {
        // Button 1 just pressed down
        key1_press_start = now;
        key1_is_held = false;
    } else if (key1 == LOW && last_key1_state == LOW) {
        // Button 1 is continuously being held
        if (!key1_is_held && (now - key1_press_start >= 350)) {
            key1_is_held = true;
        }
        // Long hold (> 3000ms): Trigger Wi-Fi pairing reset (same as Tuya factory firmware!)
        if (now - key1_press_start >= 3000 && !wifi_connected) {
            tuya_start_pairing_ap();
            key1_press_start = now + 10000; // Prevent repeated triggers
        } else if (key1_is_held && (now - last_brightness_ramp >= 20)) {
            last_brightness_ramp = now;
            int new_bright = (int)current_brightness + brightness_dir;
            if (new_bright <= 5) {
                new_bright = 5;
                brightness_dir = 1; // Reverse: ramp up
            } else if (new_bright >= 100) {
                new_bright = 100;
                brightness_dir = -1; // Reverse: ramp down
            }
            current_brightness = (uint8_t)new_bright;
            apply_rgb_brightness();
        }
    } else if (key1 == HIGH && last_key1_state == LOW) {
        // Button 1 released
        if (!key1_is_held) {
            // Short click: step through fixed comfortable levels
            if (current_brightness <= 15) {
                current_brightness = 25;
            } else if (current_brightness <= 35) {
                current_brightness = 50;
            } else if (current_brightness <= 65) {
                current_brightness = 75;
            } else if (current_brightness <= 85) {
                current_brightness = 100;
            } else {
                current_brightness = 10;
            }
            apply_rgb_brightness();
        } else {
            brightness_dir = -brightness_dir;
        }
        key1_is_held = false;
    }
    last_key1_state = key1;
}
