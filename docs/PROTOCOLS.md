# 📡 Protocol & Communication Specification

Questo documento descrive le specifiche di comunicazione di basso livello utilizzate dalla scheda Tuya TYDE 3.0: il protocollo di bootloader ST, il Tuya MCU Serial Protocol e il protocollo di bootloader Espressif SLIP.

---

## 🏛️ 1. STMicroelectronics System ROM Bootloader Protocol (AN3155)

L'STM32F103 contiene nella memoria ROM di sistema (`0x1FFFF000`) un bootloader di fabbrica ST accessibile via `USART1` (PA9/PA10).

### Parametri di Connessione:
* **Baudrate**: 115200 baud (o autobaud)
* **Formato**: **8 bit dati, Parità Even (8E1), 1 stop bit**
* **Attivazione**: Tenere premuto `MCU BOOT` durante un impulso basso su `MCU RST`.

### Sequenza di Handshake:
1. Il PC invia il byte di sincronizzazione: `0x7F`
2. L'STM32 risponde con ACK: `0x79` (se parity o baud non corretti, non risponde o manda NACK `0x1F`).
3. Ogni comando successivo invia il codice comando e il suo complemento a 1 (es. `Cmd` XOR `0xFF`).

### Comandi Principali Utilizzati da `stm32loader`:
* `0x00` / `0xFF`: **Get Command** — Restituisce versione bootloader e comandi supportati.
* `0x02` / `0xFD`: **Get ID** — Restituisce il Product ID del microcontrollore (`0x0410` per STM32F103 Medium-Density).
* `0x11` / `0xEE`: **Read Memory** — Lettura blocchi di memoria da 256 byte (usato per il backup 64 KiB).
* `0x31` / `0xCE`: **Write Memory** — Scrittura blocchi di codice flash.
* `0x43` / `0xBC`: **Erase Page / Extended Erase** — Cancellazione pagine prima della scrittura.
* `0x21` / `0xDE`: **Go** — Salto all'indirizzo utente (`0x08000000`) per avviare il firmware senza riavvio fisico.

---

## ⚡ 2. Tuya MCU Serial Communication Protocol

Protocollo seriale a pacchetti utilizzato tra la MCU STM32 (su USART3) e il modulo Wi-Fi TYWE1S per la telemetria IoT e il controllo remoto Tuya.

### Struttura Generale del Frame:

```text
+------------------+---------+---------+-------------+--------------+----------+
|  HEADER (2 Byte) | VER(1B) | CMD(1B) | LENGTH(2B)  |  DATA (N B)  | CHKSUM(1)|
|   0x55   0xAA    |  0x00   |  0xNN   | LEN_H LEN_L | D0 ... DN-1  |   0xKK   |
+------------------+---------+---------+-------------+--------------+----------+
```

* **Header fisso**: Sempre `0x55 0xAA`
* **Version**: `0x00`
* **Length**: Numero di byte presenti nel campo `DATA` (escluso il checksum).
* **Checksum**: Calcolato come: `(Header1 + Header2 + Ver + Cmd + Len_H + Len_L + sum(Data)) % 256`

### Comandi Standard Tuya MCU:

### Parametri di Connessione Tuya MCU (USART3 PB10/PB11 ↔ TYWE1S):
* **Baudrate**: **9600 baud** (8N1) — verificato per disassemblaggio del firmware di fabbrica STM32 (`mov.w r0, #0x2580` = 9600 dec).
* **Product ID (PID)**: `"qWGOYA4uqW9odEQk"`
* **Versione Firmware MCU**: `"3.0.3"`

### Comandi Standard Tuya MCU (9600 baud, framing 0x55 0xAA):

| Comando | Nome | Direzione | Payload / Frame di Esempio | Descrizione |
| :---: | :--- | :---: | :--- | :--- |
| `0x00` | **Heartbeat** | MCU ↔ Wi-Fi | `55 AA 00 00 00 00 FF` | Ping periodico di presenza (inviato ogni 1.5 secondi da STM32). Il TYWE1S risponde con ACK. |
| `0x01` | **Product Info Query** | Wi-Fi -> MCU | `55 AA 00 01 00 00 00` | Richiede PID e versione software. STM32 risponde con `qWGOYA4uqW9odEQk3.0.3` (21 byte). |
| `0x02` | **Working Mode** | MCU <-> Wi-Fi | `55 AA 00 02 00 00 01` | Notifica modalità cooperativa MCU/Wi-Fi (lunghezza 0). |
| `0x03` | **Wi-Fi State** | Wi-Fi -> MCU | `55 AA 00 03 00 01 <state> <chk>` | Notifica stato rete: `0` SmartConfig/EZ, `1` AP mode, `2` Connecting, `3` Router connesso, `4` Cloud Tuya connesso, `5` Timeout. |
| `0x04` | **Reset Wi-Fi** | MCU -> Wi-Fi | `55 AA 00 04 00 00 03` | Cancella credenziali Wi-Fi correnti sul TYWE1S e avvia pairing. |
| `0x05` | **Select Mode** | MCU -> Wi-Fi | `55 AA 00 05 00 01 01 06` (AP) / `00 05` (EZ) | Seleziona modalità pairing AP (Hotspot SmartLife-xxxx) o SmartConfig rapido EZ. |
| `0x06` | **Command Send** | Wi-Fi -> MCU | `55 AA 00 06 ...` | Invia comandi di controllo provenienti dall'App Tuya / Cloud (DP stream). |

### Comportamento LED 3 (Indicatore Wi-Fi):
* **Fisico sul modulo TYWE1S**: LED3 è fisicamente pilotato dal modulo Wi-Fi TYWE1S.
* **In Connessione / Connesso**: Se connesso al router (`STATE 3` o `4`), LED 3 rimane **ACCESO FISSO**.
* **In Ricerca / Pairing / Disconnesso**: Se disconnesso o in pairing AP/EZ (`STATE 0`, `1`, `2`), LED 3 **lampeggia con frequenza 0.5 sec** (o 0.25 sec in EZ).

---

Sulla scheda Tuya TYDE 3.0, il canale PC ↔ STM32 (USART1 PA9/PA10 @ 115200 baud) espone la console di controllo per la Web Dashboard:

### Comandi PC → STM32 (USART1 @ 115200 baud)

| Comando | Risposta | Descrizione |
| :--- | :--- | :--- |
| `?` | `OK` | Ping diagnostico del runtime STM32 |
| `T` | `T:XXXX,V:YYYY,VDD:3.29V,TEMP:35.9C` | Telemetria ADC (temperatura die + VREFINT) |
| `C:<r>,<g>,<b>` | `RGB:OK` | Colore LED RGB on-board |
| `C:TEMP` / `C:RAINBOW` / `C:OFF` | `RGB:...` | Modalità RGB |
| `C:B:<0-100>` | `RGB:BRIGHT:<n>` | Luminosità |
| `L:<id>,<mode>,<ms>` | `LED:OK` | LED di stato 1-3 (ON/OFF/BLINK) |
| `L:?` | `LED:1=2,500;2=1,500;3=2,500` | Stato LED |
| `W:PAIR:AP` / `W:DISCONNECT` | `WIFI:OK:PAIRING_AP` | Disconnette dalla LAN e avvia pairing AP (hotspot cellulare) |
| `W:PAIR:EZ` / `W:PAIR` | `WIFI:OK:PAIRING_EZ` | Avvia pairing rapido EZ per app Tuya Smart |
| `W:?` / `W:STATUS` | `WIFI:CONNECTED,STATE:<n>,LED3:SOLID_ON` | Query stato Wi-Fi nativo Tuya |

---

## 📶 3. Espressif ESP8266 ROM Bootloader & SLIP Protocol (`esptool`)

Il modulo TYWE1S integra il chip Espressif ESP8266EX. All'avvio della ROM interna, il chip campiona tre pin di strapping hardware:

### Tabella Modalità di Avvio (Strapping Pins ESP8266):

| Modalità di Boot | GPIO15 (MTDO) | GPIO0 | GPIO2 | Uscita Log ROM (@ 74880 baud) |
| :--- | :---: | :---: | :---: | :--- |
| **UART Download Mode (Flashing)** | **0 (Low)** | **0 (Low)** | **1 (High)** | `boot mode:(1,x)` — In attesa di comandi seriali |
| **Normal SPI Flash Boot** | **0 (Low)** | **1 (High)** | **1 (High)** | `boot mode:(3,x)` — Esegue il firmware Tuya salvato in Flash |
| **SDIO Mode** | 0 (Low) | 0 (Low) | 0 (Low) | `boot mode:(2,x)` |

Sulla scheda Tuya TYDE 3.0:
* `GPIO15` è mantenuto permanentemente a massa da un resistore di pull-down integrato nel modulo TYWE1S.
* `GPIO2` è mantenuto ad alimentazione da pull-up (ed emette il log di debug hardware a 74880 baud su `TX1`).
* `GPIO0` è collegato al pulsante **`WIFI BOOT`** (premuto = LOW; rilasciato = HIGH).

### Protocollo SLIP (Serial Line Internet Protocol):
`esptool` comunica con la ROM dell'ESP8266 usando il framing SLIP (RFC 1055):
* Delimitatore di inizio e fine frame: `0xC0`
* Caratteri di escape: `0xDB 0xDC` (sostituisce `0xC0`) e `0xDB 0xDD` (sostituisce `0xDB`).
* **Pacchetto di sincronizzazione (SYNC)**:
  `0xC0 0x00 0x08 0x24 0x00 0x00 0x00 0x00 0x00 0x07 0x07 0x12 0x20` + 32 byte di `0x55` + `0xC0`.
  Quando l'ESP8266 è in modalità bootloader, risponde immediatamente con un frame SLIP di ACK confermando il chipset e le capacità SPI.
