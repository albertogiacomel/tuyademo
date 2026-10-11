# 💾 Firmware & Assembly Architecture — Tuya TYDE 3.0

Questo documento descrive la struttura del firmware di fabbrica originale, l'analisi del disassemblato e l'architettura dei micro-firmware ponte STM32 sviluppati per il controllo trasparente del bus seriale.

---

## 📦 1. Dump Firmware di Fabbrica Originale STM32

Il firmware originale installato da Tuya sulla MCU STM32F103C8T6 è stato interamente estratto via ST Bootloader ROM e preservato in:
* **File**: `firmware/stm32_factory_tuya_backup.bin`
* **Dimensione esatta**: **65.536 byte (64 KiB)**
* **SHA256**: `verificato e consistente`

### Analisi Vector Table Cortex-M3:
```text
Offset 0x00000000 (Initial Stack Pointer): 0x20000800 (SRAM 2 KiB stack boundary)
Offset 0x00000004 (Reset Handler Address): 0x0800084D (Thumb mode bit 0 attivo -> 0x0800084C)
```

Il firmware originale implementa il **Tuya MCU SDK**:
* Comunica con il modulo TYWE1S tramite protocollo seriale a frame su USART3.
* Gestisce lo stato della rete, l'heartbeat periodico, la sincronizzazione del clock di rete e il reporting dei Data Point (DP) verso il cloud Tuya.

---

## ⚡ 2. Architettura dei Ponti Seriali STM32 (Solution B)

Poiché il bridge USB Silicon Labs CP2102 è collegato fisicamente solo a USART1 dell'STM32 (PA9/PA10), per dialogare con il modulo Wi-Fi TYWE1S senza cavetti Dupont volanti l'STM32 viene temporaneamente programmato con un micro-firmware bare-metal in assembly ARM Thumb (256 byte) che esegue il forwarding bidirezionale a zero overhead tra:
* **USART1** (PA9 TX, PA10 RX) @ 115200 baud <—> **PC via USB**
* **USART3** (PB10 TX, PB11 RX) @ 115200 baud <—> **TYWE1S Wi-Fi**

```mermaid
flowchart LR
    PC["PC (COM3 / esptool)"] <-->|"USB CDC (115200)"| CP2102["CP2102 Bridge"]
    CP2102 <-->|"USART1 (PA9/PA10)"| STM32["STM32F103 (Smart Bridge)"]
    STM32 <-->|"USART3 (PB10/PB11)"| ESP["TYWE1S (ESP8266EX)"]
```

---

## 🧠 3. Risoluzione del Deadlock Hardware: Overrun Error (ORE)

### Il Problema Silicio del Boot ESP8266:
Quando il modulo TYWE1S viene riavviato (via `WIFI RST`), la sua ROM bootloader invia sulla seriale una stringa diagnostica a **74880 baud** (`ets Jan 8 2013,rst cause:2, boot mode:...`).
Poiché l'UART dell'STM32 è configurata a **115200 baud**:
1. I dati a 74880 baud provocano framing errors continui (`FE`).
2. La velocità di ricezione genera un **Overrun Error (`ORE`)** nel registro `USART_SR` dell'STM32.
3. **Comportamento da Reference Manual STM32 RM0008 (§27.3.3)**:
   > *"Quando si verifica un errore di Overrun, il flag ORE viene impostato. I dati successivi nel registro di scorrimento vengono scartati e nessun ulteriore carattere genera il flag RXNE fino a quando l'errore ORE non viene cancellato dal software."*
   > *"La cancellazione di ORE richiede una sequenza precisa: lettura di `USART_SR` seguita da una lettura di `USART_DR`."*

Se il codice del bridge controlla semplicemente `RXNE` e non azzera `ORE`, **il ricevitore hardware dell'STM32 rimane permanentemente muto e sordo per sempre**.

### La Soluzione nel Firmware `stm32_uart_smart_bridge.bin`:
Il ciclo principale implementa il controllo e la pulizia automatica di tutti i flag di errore hardware (`ORE`, `NE`, `FE`, `PE` = maschera `0x0F`):

```assembly
loop:
    // Controllo USART1 (dal PC)
    ldr r0, [r4, #0x00]      // Legge USART1->SR
    movs r2, #0x20           // Maschera bit 5 (RXNE)
    tst r0, r2
    beq check_u1_err         // Se nessun byte valido, controlla errori
    ldr r1, [r4, #0x04]      // Legge USART1->DR (consuma il byte)
    // ... gestione comando o inoltro ...

check_u1_err:
    movs r2, #0x0F           // Maschera bit 0..3 (ORE | NE | FE | PE)
    tst r0, r2
    beq check_u3             // Se nessun errore, passa a USART3
    ldr r1, [r4, #0x04]      // Lettura fittizia di DR per azzerare ORE hardware!

check_u3:
    // Controllo USART3 (dal Wi-Fi TYWE1S)
    ldr r0, [r5, #0x00]      // Legge USART3->SR
    movs r2, #0x20           // Maschera bit 5 (RXNE)
    tst r0, r2
    beq check_u3_err
    ldr r1, [r5, #0x04]      // Legge byte ricevuto da TYWE1S
    bl send_u1_byte          // Inoltra immediatamente al PC!
    b loop

check_u3_err:
    movs r2, #0x0F           // Azzeramento ORE/FE su ricezione Wi-Fi
    tst r0, r2
    beq loop
    ldr r1, [r5, #0x04]      // Lettura fittizia di DR: ricevitore ripristinato!
    b loop
```

---

## 🛠️ 4. Canale Diagnostico Non Invasivo (Smart Bridge)

Per garantire la massima affidabilità senza interrompere `esptool`:
* Se il PC invia il byte ASCII **`?`** (`0x3F`): l'STM32 risponde direttamente `OK\n`. Questo permette al comando `python tuya_cli.py ping` di verificare istantaneamente la salute del runtime STM32.
* Se il PC invia il byte ASCII **`T`** (`0x54`) o **`t`** (`0x74`): l'STM32 acquisisce il canale **`ADC1_IN16`** (temperatura die) e **`ADC1_IN17`** (tensione di riferimento interna VREFINT) e risponde istantaneamente con la stringa `T:<hex16>,V:<hex16>\n` (gestita dal comando `python tuya_cli.py read-temp`).
* Se il PC invia il byte ASCII **`B`** (`0x42`): l'STM32 effettua un salto hardware diretto al bootloader ST in System ROM (`0x1FFFF000`).
* Se il PC invia **`W:?`** o **`W:STATUS`**: il comando viene inoltrato trasparentemente al TYWE1S (senza alterare lo stato del LED 3); il modulo risponde con `WIFI:CONNECTED,IP:<ip>,SSID:<ssid>,RSSI:<dbm>` oppure `WIFI:DISCONNECTED`. Il firmware TYWE1S esporta autonomamente lo stesso frame seriale a ogni variazione di connessione o di IP DHCP, così la dashboard può visualizzare l'indirizzo LAN del modulo.
* Tutti gli altri pacchetti (inclusi i frame SLIP di `esptool` che iniziano e terminano con `0xC0`): vengono inoltrati trasparentemente senza alcuna alterazione o echo locale.

---

## 📐 5. Mappa Registri Hardware STM32F103 Utilizzati

| Registro | Indirizzo Fisico | Valore Scritto | Descrizione Funzionale |
| :--- | :--- | :--- | :--- |
| `RCC->APB2ENR` | `0x40021018` | `0x0000420C` | Abilita bus clock per `IOPAEN`, `IOPBEN`, `ADC1EN`, `USART1EN` |
| `RCC->APB1ENR` | `0x4002101C` | `0x00040000` | Abilita bus clock per `USART3EN` |
| `GPIOA->CRH` | `0x40010804` | Nibble `0x4B` | PA9 = AF-PP (50MHz), PA10 = Floating Input |
| `GPIOB->CRH` | `0x40010C04` | Nibble `0x4B` | PB10 = AF-PP (50MHz), PB11 = Floating Input |
| `USART1->BRR` | `0x40013808` | `0x00000045` | Divisore baudrate per 115200 baud su HSI 8 MHz |
| `USART1->CR1` | `0x4001380C` | `0x0000200C` | Abilita USART, Trasmettitore e Ricevitore (`UE \| TE \| RE`) |
| `USART3->BRR` | `0x40004808` | `0x00000045` | Divisore baudrate per 115200 baud su HSI 8 MHz |
| `USART3->CR1` | `0x4000480C` | `0x0000200C` | Abilita USART, Trasmettitore e Ricevitore (`UE \| TE \| RE`) |
| `ADC1->SMPR1` | `0x4001240C` | `0x00FC0000` | Sample time Ch16 e Ch17 impostato a 239.5 cicli ADC (>17.1 µs richiesti) |
| `ADC1->CR2` | `0x40012408` | `0x00800001` | Abilita ADC1 e sensore di temperatura (`TSVREFE \| ADON`) + calibrazione |
| `ADC1->SQR3` | `0x40012434` | `0x00000010` / `0x11` | Selezione canale di conversione (16 = Temp, 17 = Vrefint) |

