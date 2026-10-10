/**
 * @file main.cpp
 * @brief Smart UART Bridge & Diagnostics Firmware for Tuya TYDE 3.0 IoT Dev Kit
 * 
 * Features:
 * 1. Bi-directional transparent UART passthrough between PC (USART1) and TYWE1S (USART3)
 * 2. Diagnostic command interception:
 *    - '?' -> responds "OK\r\n"
 *    - 'T' -> reads ADC1_IN16 & VREFINT, outputs JSON / structured telemetry
 *    - 'B' -> jumps to ST ROM system bootloader
 * 3. User buttons & status LED feedback
 */

#include <Arduino.h>
#include "board_pins.h"

static uint32_t last_heartbeat = 0;

void setup() {
    // Configure status LEDs
    pinMode(PIN_LED1, OUTPUT);
    pinMode(PIN_LED2, OUTPUT);
    digitalWrite(PIN_LED1, HIGH); // OFF (Active Low)
    digitalWrite(PIN_LED2, LOW);  // ON (MCU Running)

    // Configure user buttons
    pinMode(PIN_KEY1, INPUT_PULLUP);
    pinMode(PIN_KEY2, INPUT_PULLUP);

    // Configure RGB LED
    pinMode(PIN_RGB_RED, OUTPUT);
    pinMode(PIN_RGB_GREEN, OUTPUT);
    pinMode(PIN_RGB_BLUE, OUTPUT);
    digitalWrite(PIN_RGB_RED, LOW);
    digitalWrite(PIN_RGB_GREEN, LOW);
    digitalWrite(PIN_RGB_BLUE, LOW);

    // Initialize ADC for 12-bit resolution
    analogReadResolution(12);

    // Initialize Serial1 (PC USB @ 115200) and Serial3 (TYWE1S @ 115200)
    Serial.setRx(PIN_UART1_RX);
    Serial.setTx(PIN_UART1_TX);
    Serial.begin(115200);

    Serial3.setRx(PIN_UART3_RX);
    Serial3.setTx(PIN_UART3_TX);
    Serial3.begin(115200);
}

void read_temperature() {
    int raw_temp = analogRead(ATEMP);
    int raw_vref = analogRead(AVREF);

    float vdd = (raw_vref > 0) ? (1.20f * 4096.0f / (float)raw_vref) : 3.30f;
    float vsense = ((float)raw_temp / 4096.0f) * vdd;
    float temp_c = ((1.43f - vsense) / 0.0043f) + 25.0f;

    char buf[64];
    snprintf(buf, sizeof(buf), "T:%04X,V:%04X,VDD:%.2fV,TEMP:%.1fC\r\n", 
             raw_temp, raw_vref, vdd, temp_c);
    Serial.print(buf);
}

void loop() {
    // --- 1. Forward from PC (Serial) to Wi-Fi (Serial3) with command interception ---
    while (Serial.available()) {
        int c = Serial.read();
        if (c == '?') {
            Serial.print("OK\r\n");
            continue;
        } else if (c == 'T' || c == 't') {
            read_temperature();
            continue;
        } else if (c == 'B') {
            Serial.print("REBOOT_BOOTLOADER\r\n");
            delay(50);
            // Software jump to ST System Memory Bootloader (0x1FFFF000)
            void (*SysMemBootJump)(void) = (void (*)(void))(*((uint32_t *)0x1FFFF004));
            __set_MSP(*((uint32_t *)0x1FFFF000));
            SysMemBootJump();
        }
        Serial3.write((uint8_t)c);
    }

    // --- 2. Forward from Wi-Fi (Serial3) to PC (Serial) ---
    while (Serial3.available()) {
        int c = Serial3.read();
        Serial.write((uint8_t)c);
    }

    // --- 3. Heartbeat LED toggle every 500ms ---
    uint32_t now = millis();
    if (now - last_heartbeat > 500) {
        last_heartbeat = now;
        digitalWrite(PIN_LED2, !digitalRead(PIN_LED2));
    }
}
