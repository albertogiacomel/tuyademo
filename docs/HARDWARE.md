# 🔌 Hardware Reference — Tuya TYDE 3.0 v1.0.3 IoT Dev Kit

Questo documento descrive in dettaglio la topologia hardware, i pinout, i blocchi di ponticelli e la mappatura dei pulsanti della scheda di sviluppo Tuya TYDE 3.0 v1.0.3.

---

## 📋 1. Specifiche Componenti Principali

| Componente | Sigla / Modello | Caratteristiche Tecniche | Ruolo nel Sistema |
| :--- | :--- | :--- | :--- |
| **Microcontrollore (MCU)** | **STM32F103C8T6** | ARM Cortex-M3 @ 72 MHz, 64 KiB Flash, 20 KiB SRAM, LQFP48 | Gestione logica sensori / Tuya MCU SDK / Bridge Passthrough |
| **Modulo Wi-Fi** | **TYWE1S** | Espressif ESP8266EX, 2 MiB SPI Flash, antenna PCB integrata | Connettività cloud Tuya / Wi-Fi 802.11 b/g/n / UART seriale |
| **Ponte USB-UART** | **Silicon Labs CP2102** | USB 2.0 Full Speed to UART Bridge, fino a 921600 baud | Interfaccia PC via cavo micro-USB (`COM3`) |
| **Alimentazione** | LDO 3.3V | Regolatore lineare on-board da 5V USB a 3.3V logica | LED4 acceso fisso indica rail 3.3V attivo |

### Dati Univoci Dispositivo Rilevati via Bootloader
* **Chip ID**: `0x410` (STM32F10x Medium-density)
* **Bootloader Version**: `0x22` (ST System ROM)
* **Device UID**: `FF37-0670-3137564E-43063541` (96-bit Unique Silicon Identifier)
* **Flash Size registrata**: `64 KiB` (`0x08000000` - `0x08010000`)

---

## 🎛️ 2. Mappatura Pulsanti (Layout Superiore)

Sulla parte superiore della scheda sono posizionati 4 pulsanti a pressione (da sinistra a destra):

```text
[ S1: MCU RST ]   [ S2: MCU BOOT ]   [ S3: WIFI BOOT ]   [ S4: WIFI RST ]
      (1)               (2)                 (3)                (4)
```

| N° | Serigrafia PCB | Segnale Fisico | Funzione Operativa |
| :---: | :--- | :--- | :--- |
| **1** | `RST` (sotto `MCU`) | STM32 `NRST` (Pin 7) | **Reset STM32**: Riavvia l'esecuzione del microcontrollore. Se rilasciato con BOOT0=0, esegue il firmware su Flash (`0x08000000`). |
| **2** | `BOOT` (sotto `MCU`) | STM32 `BOOT0` (Pin 44) | **Boot Mode STM32**: Tenuto premuto durante il reset, forza l'avvio nel Bootloader di Sistema ST in ROM (`0x1FFFF000`) per flashing via UART1. |
| **3** | `BOOT` (sotto `WIFI`) | TYWE1S `GPIO0` (Pin 12) | **Flash Mode ESP8266**: Tenuto premuto durante il reset del Wi-Fi, imposta il boot mode `(1,x)` (UART Download Mode) per `esptool`. |
| **4** | `RST` (sotto `WIFI`) | TYWE1S `RST` / `EXT_RSTB` | **Reset Modulo Wi-Fi**: Riavvia l'ESP8266EX. |

---

## 📍 3. Matrice Completa dei Ponticelli (Jumper Caps)

Per consentire l'interfacciamento completo via software tramite l'STM32 senza cavetti Dupont esterni (Soluzione B), **tutti e 6 i ponticelli devono essere inseriti** come da configurazione di fabbrica:

```text
        +-------------------------------------------------+
        |  P2 (USB <-> STM32)       P3 (STM32 <-> Wi-Fi)  |
        |  [M_TX1] [.] [M_RX1]       [.]     [.]  (Riga 1)|
        |    ||     .    ||          [M_TX3]=[W_RX](Riga 2)|
        |  [ U_R ] [G] [ U_T ]       [.]     [.]  (Riga 3)|
        |                            [M_RX3]=[W_TX](Riga 4)|
        |                            [.]     [.]  (Riga 5)|
        +-------------------------------------------------+
        |  P11 (Pass-thru E_RX)      P13 (Pass-thru E_TX) |
        |  [ E_RX ] (1 jumper)       [.]     (aperto)     |
        |  [ A_RX ]                  [.]                  |
        |  [ A_TX ]                  [ A_TX ]             |
        |  [.]      (aperto)         [ E_TX ](1 jumper)   |
        +-------------------------------------------------+
```

### Dettaglio Connettori:
1. **Connettore `P2` (USB Bridge CP2102 <-> STM32 USART1)**:
   * **Jumper 1 (Verticale a sinistra)**: Collega `U_R` (CP2102 RX) con `M_TX1` (STM32 PA9 USART1_TX).
   * **Jumper 2 (Verticale a destra)**: Collega `U_T` (CP2102 TX) con `M_RX1` (STM32 PA10 USART1_RX).
   * **Pin centrale**: Ground (`GND`).

2. **Connettore `P3` (STM32 USART3 <-> Modulo Wi-Fi TYWE1S)**:
   * **Jumper 3 (Orizzontale su Riga 2)**: Collega `M_TX3` (STM32 PB10 USART3_TX) con `W_RX` (TYWE1S UART0_RX).
   * **Jumper 4 (Orizzontale su Riga 4)**: Collega `M_RX3` (STM32 PB11 USART3_RX) con `W_TX` (TYWE1S UART0_TX).

3. **Connettori di Routing `P11` & `P13`**:
   * **Jumper 5 su `P11` (Verticale in alto)**: Inserito su `E_RX` per consentire la continuazione del trace verso il modulo TYWE1S.
   * **Jumper 6 su `P13` (Verticale in basso)**: Inserito su `E_TX` per consentire la continuazione del trace di trasmissione dal modulo TYWE1S.

---

## 📡 4. Header Standalone Modulo Wi-Fi (`E1S`)

Nell'angolo superiore destro della scheda è presente un header maschio standard a 5 pin dedicato al modulo TYWE1S per programmazione o debug diretto:

| Pin N° | Serigrafia | Descrizione | Livello Logico |
| :---: | :--- | :--- | :---: |
| 1 | `3.3V` | Alimentazione VCC modulo | 3.3V DC |
| 2 | `GND` | Massa di riferimento comune | 0V |
| 3 | `TX1` | ESP8266 GPIO2 / UART1 TX (Debug log ESP) | 3.3V TTL |
| 4 | `RX0` | ESP8266 UART0 RX (Ricezione flashing e MCU) | 3.3V TTL |
| 5 | `TX0` | ESP8266 UART0 TX (Trasmissione flashing e MCU) | 3.3V TTL |
