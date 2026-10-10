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

| Comando | Nome | Direzione | Payload di Esempio | Descrizione |
| :---: | :--- | :---: | :--- | :--- |
| `0x00` | **Heartbeat** | MCU -> Wi-Fi | `55 AA 00 00 00 00 FF` | Ping periodico di presenza (inviato ogni 1-3 secondi). Il Wi-Fi risponde con il proprio stato. |
| `0x01` | **Product Info Query** | Wi-Fi -> MCU | `55 AA 00 01 00 00 00` | Richiede il Product ID (PID) e la versione software dell'MCU. |
| `0x02` | **Working Mode** | MCU <-> Wi-Fi | `55 AA 00 02 00 00 01` | Sincronizza la modalità di gestione degli stati di rete e dei LED. |
| `0x03` | **Wi-Fi State** | Wi-Fi -> MCU | `55 AA 00 03 00 01 SS KK` | Notifica stato rete: `00` SmartConfig, `01` AP mode, `02` Wi-Fi OK, `03` Cloud Connesso. |
| `0x06` | **Command Send** | Wi-Fi -> MCU | `55 AA 00 06 ...` | Invia comandi di controllo provenienti dall'App o dal Cloud (es. accendi relè). |
| `0x07` | **Status Report** | MCU -> Wi-Fi | `55 AA 00 07 ...` | Riporta al cloud il valore corrente dei sensori (Data Point - DP). |

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
