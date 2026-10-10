/**
 * @file main.cpp
 * @brief Smart UART Bridge & Advanced RGB/Sensors Firmware for Tuya TYDE 3.0 IoT Dev Kit
 * 
 * Target MCU: STM32F103C8T6 (72MHz ARM Cortex-M3)
 * Exact Pinout reverse-engineered from factory firmware:
 * - Status LEDs (Active Low): PB6 (LED1), PB5 (LED2)
 * - RGB LED (Active Low PWM on TIM4): PB8 (Red), PB7 (Green), PB9 (Blue)
 * - User Buttons (Active Low): PA11 (Key1), PA12 (Key2)
 * - Serial: PA9/PA10 (USART1), PB10/PB11 (USART3)
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
static uint32_t last_heartbeat = 0;
static uint32_t last_temp_update = 0;
static uint32_t last_rainbow_update = 0;
static uint16_t rainbow_hue = 0;
static bool last_key2_state = HIGH;
static float current_temp_c = 25.0f;

void set_rgb_pwm(uint8_t r, uint8_t g, uint8_t b) {
    // Active Low PWM on TIM4 channels: 255 = fully OFF, 0 = fully ON
    analogWrite(PIN_RGB_RED, 255 - r);
    analogWrite(PIN_RGB_GREEN, 255 - g);
    analogWrite(PIN_RGB_BLUE, 255 - b);
}

void update_temp_color(float temp_c) {
    // Visual temperature mapping:
    // < 30°C: Pure Blue (0, 0, 255)
    // 30 - 34°C: Cyan (0, 255, 255)
    // 34 - 38°C: Green (0, 255, 0)
    // 38 - 42°C: Yellow (255, 255, 0)
    // > 42°C: Red (255, 0, 0)
    if (temp_c < 30.0f) {
        set_rgb_pwm(0, 50, 255); // Cool Blue
    } else if (temp_c < 34.0f) {
        set_rgb_pwm(0, 200, 200); // Cyan
    } else if (temp_c < 38.0f) {
        set_rgb_pwm(0, 255, 50); // Vibrant Green (Normal operational range ~34-36°C)
    } else if (temp_c < 42.0f) {
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

void setup() {
    // Configure status LEDs (Active Low)
    pinMode(PIN_LED1, OUTPUT);
    pinMode(PIN_LED2, OUTPUT);
    digitalWrite(PIN_LED1, HIGH); // OFF
    digitalWrite(PIN_LED2, LOW);  // ON (MCU Alive)

    // Configure user buttons
    pinMode(PIN_KEY1, INPUT_PULLUP);
    pinMode(PIN_KEY2, INPUT_PULLUP);

    // Configure RGB LED pins on TIM4
    pinMode(PIN_RGB_RED, OUTPUT);
    pinMode(PIN_RGB_GREEN, OUTPUT);
    pinMode(PIN_RGB_BLUE, OUTPUT);
    set_rgb_pwm(0, 0, 0); // Initially off

    // Initialize ADC for 12-bit internal temperature sensor & VREFINT
    analogReadResolution(12);

    // Initialize Serial1 (PC USB @ 115200) and Serial3 (TYWE1S @ 115200)
    Serial.setRx(PIN_UART1_RX);
    Serial.setTx(PIN_UART1_TX);
    Serial.begin(115200);

    Serial3.setRx(PIN_UART3_RX);
    Serial3.setTx(PIN_UART3_TX);
    Serial3.begin(115200);

    // Initial temperature measurement
    int rt, rv; float vdd;
    current_temp_c = measure_temperature(rt, rv, vdd);
    update_temp_color(current_temp_c);
}

void loop() {
    // --- 1. Forward from PC (Serial) to Wi-Fi (Serial3) with command interception ---
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
            // RGB command: C:<r>,<g>,<b> or C:TEMP or C:RAINBOW or C:OFF
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
        } else if (c == 'B') {
            Serial.read();
            jump_to_bootloader();
            return;
        }
        
        // Pass byte to TYWE1S
        c = Serial.read();
        Serial3.write((uint8_t)c);
    }

    // --- 2. Forward from Wi-Fi (Serial3) to PC (Serial) ---
    while (Serial3.available()) {
        int c = Serial3.read();
        Serial.write((uint8_t)c);
    }

    // --- 3. Heartbeat on LED2 (PB5): Toggle every 500ms ---
    uint32_t now = millis();
    if (now - last_heartbeat >= 500) {
        last_heartbeat = now;
        digitalWrite(PIN_LED2, !digitalRead(PIN_LED2));
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

    // --- 7. User Button 1 (PA11): Mirror on LED1 (PB6) ---
    digitalWrite(PIN_LED1, digitalRead(PIN_KEY1));
}
