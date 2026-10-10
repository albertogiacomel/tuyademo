/**
 * @file main.cpp
 * @brief Smart UART Bridge & Diagnostics Firmware for Tuya TYDE 3.0 IoT Dev Kit
 * 
 * Target MCU: STM32F103C8T6 (72MHz ARM Cortex-M3)
 * Exact Pinout reverse-engineered from factory firmware:
 * - Status LEDs (Active Low): PB6 (LED1), PB5 (LED2)
 * - RGB LED (Active Low): PB8 (Red), PB7 (Green), PB9 (Blue)
 * - User Buttons (Active Low): PA11 (Key1), PA12 (Key2)
 * - Serial: PA9/PA10 (USART1), PB10/PB11 (USART3)
 */

#include <Arduino.h>
#include "board_pins.h"

static uint32_t last_heartbeat = 0;
static uint8_t rgb_state = 0;
static bool last_key2_state = HIGH;

void set_rgb(bool r, bool g, bool b) {
    // Active Low: LOW = ON, HIGH = OFF
    digitalWrite(PIN_RGB_RED, r ? LOW : HIGH);
    digitalWrite(PIN_RGB_GREEN, g ? LOW : HIGH);
    digitalWrite(PIN_RGB_BLUE, b ? LOW : HIGH);
}

void setup() {
    // Configure status LEDs (Active Low: HIGH = OFF)
    pinMode(PIN_LED1, OUTPUT);
    pinMode(PIN_LED2, OUTPUT);
    digitalWrite(PIN_LED1, HIGH); // OFF initially
    digitalWrite(PIN_LED2, LOW);  // ON initially (MCU Alive)

    // Configure user buttons (Active Low with Pull-Up)
    pinMode(PIN_KEY1, INPUT_PULLUP);
    pinMode(PIN_KEY2, INPUT_PULLUP);

    // Configure RGB LED (Active Low: HIGH = OFF)
    pinMode(PIN_RGB_RED, OUTPUT);
    pinMode(PIN_RGB_GREEN, OUTPUT);
    pinMode(PIN_RGB_BLUE, OUTPUT);
    set_rgb(false, false, false); // All OFF

    // Initialize ADC for 12-bit internal temperature sensor & VREFINT
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

void jump_to_bootloader() {
    Serial.print("REBOOT_BOOTLOADER\r\n");
    Serial.flush();
    delay(50);
    // De-initialize peripherals
    Serial.end();
    Serial3.end();
    // Reset RCC and SysTick
    RCC->APB2RSTR = 0x00004200; // Reset USART1 & ADC1
    RCC->APB2RSTR = 0;
    SysTick->CTRL = 0;
    // Jump to ST System Memory Bootloader (0x1FFFF000)
    void (*SysMemBootJump)(void) = (void (*)(void))(*((uint32_t *)0x1FFFF004));
    __set_MSP(*((uint32_t *)0x1FFFF000));
    SysMemBootJump();
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
            jump_to_bootloader();
            return;
        }
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

    // --- 4. User Button 2 (PA12): Cycle RGB LED colors ---
    bool key2 = digitalRead(PIN_KEY2);
    if (key2 == LOW && last_key2_state == HIGH) {
        // Button 2 pressed (falling edge)
        delay(20); // Debounce
        rgb_state = (rgb_state + 1) % 4;
        switch (rgb_state) {
            case 0: set_rgb(false, false, false); break; // OFF
            case 1: set_rgb(true, false, false);  break; // RED
            case 2: set_rgb(false, true, false);  break; // GREEN
            case 3: set_rgb(false, false, true);  break; // BLUE
        }
    }
    last_key2_state = key2;

    // --- 5. User Button 1 (PA11): Mirror on LED1 (PB6) ---
    digitalWrite(PIN_LED1, digitalRead(PIN_KEY1)); // Pressed = LOW (LED ON)
}
