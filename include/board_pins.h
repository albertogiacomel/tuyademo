#pragma once

/**
 * @file board_pins.h
 * @brief Tuya TYDE 3.0 v1.0.3 Board Pinout & Peripheral Mapping
 * Target MCU: STM32F103C8T6 (72MHz ARM Cortex-M3, 64KB Flash, 20KB RAM)
 */

#include <Arduino.h>

// --- Serial Ports ---
// USART1: Connected to Silicon Labs CP2102 USB Bridge via Jumper P2
#define PIN_UART1_TX      PA9
#define PIN_UART1_RX      PA10

// USART3: Connected to Tuya TYWE1S (ESP8266) Wi-Fi Module via Jumper P3
#define PIN_UART3_TX      PB10
#define PIN_UART3_RX      PB11

// --- On-Board Status LEDs (Active Low) ---
#define PIN_LED1          PA2   // Wi-Fi Connection Status Indicator
#define PIN_LED2          PA3   // MCU General Status Indicator

// --- On-Board RGB LED (RGB彩灯 - Low-Side / PWM Driven) ---
#define PIN_RGB_RED       PB0
#define PIN_RGB_GREEN     PB1
#define PIN_RGB_BLUE      PA1

// --- On-Board User Push Buttons (Active Low with Pull-Up) ---
#define PIN_KEY1          PA0   // 按键1: Mode / Toggle / Pairing Trigger
#define PIN_KEY2          PA4   // 按键2: Test / Color Cycle Trigger

// --- Internal Sensor ADC Channels ---
// ADC1_IN16: Internal Core Temperature Sensor
// ADC1_IN17: Internal VREFINT (1.20V nominal bandgap reference)
