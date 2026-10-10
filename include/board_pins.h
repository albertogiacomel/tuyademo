#pragma once

/**
 * @file board_pins.h
 * @brief Tuya TYDE 3.0 v1.0.3 Board Pinout & Peripheral Mapping
 * Target MCU: STM32F103C8T6 (72MHz ARM Cortex-M3, 64KB Flash, 20KB RAM)
 * 
 * Reconstructed 100% via binary disassembly of factory firmware (stm32_factory_tuya_backup.bin):
 * - Status LEDs: PB6 (LED1), PB5 (LED2) (Active Low)
 * - RGB LED: PB8 (Red), PB7 (Green), PB9 (Blue) (Active Low)
 * - User Buttons: PA11 (Key1), PA12 (Key2) (Input Pull-Up)
 * - Serial: PA9/PA10 (USART1), PB10/PB11 (USART3)
 */

#include <Arduino.h>

// --- Serial Ports ---
// USART1: Connected to Silicon Labs CP2102 USB Bridge via Jumper P2
#define PIN_UART1_TX      PA9
#define PIN_UART1_RX      PA10

// USART3: Connected to Tuya TYWE1S (ESP8266) Wi-Fi Module via Jumper P3
#define PIN_UART3_TX      PB10
#define PIN_UART3_RX      PB11

// --- On-Board Status LEDs (Active Low: LOW = ON, HIGH = OFF) ---
#define PIN_LED1          PB6   // LED1 (Wi-Fi Status Indicator)
#define PIN_LED2          PB5   // LED2 (MCU Status / Heartbeat Indicator)

// --- On-Board RGB LED (RGB彩灯 - Active Low: LOW = ON, HIGH = OFF) ---
#define PIN_RGB_RED       PB8   // Red Channel (TIM4_CH3)
#define PIN_RGB_GREEN     PB9   // Green Channel (TIM4_CH4 - Hardware verified)
#define PIN_RGB_BLUE      PB7   // Blue Channel (TIM4_CH2 - Hardware verified)

// --- On-Board User Push Buttons (Active Low with Pull-Up: LOW = Pressed) ---
#define PIN_KEY1          PA11  // 按键1: Mode / Toggle / Pairing Trigger
#define PIN_KEY2          PA12  // 按键2: Test / Color Cycle Trigger

// --- Internal Sensor ADC Channels ---
// ADC1_IN16: Internal Core Temperature Sensor
// ADC1_IN17: Internal VREFINT (1.20V nominal bandgap reference)
