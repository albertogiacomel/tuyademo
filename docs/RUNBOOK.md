# 📘 Operational Runbook — Tuya TYDE 3.0

Guida operativa procedurale passo-passo per il collaudo, la diagnostica, il backup, il flashing e il ripristino della scheda Tuya TYDE 3.0 v1.0.3.

---

## 🚀 Scenario 1: Verifica Live dello Smart Bridge (Ping Diagnostico)

Verifica che l'STM32 sia alimentato, stia eseguendo il firmware ponte e risponda su COM3:

```powershell
python scripts/tuya_cli.py ping
```
* **Output atteso**: `[+] SUCCESSO: STM32 Smart Bridge attivo e rispondente! Risposta: b'OK\n'`
* **Se non risponde**: Premi e rilascia **`MCU RST`** (1° pulsante da sinistra) e riprova.

---

## 🔍 Scenario 2: Monitor Seriale in Ascolto Live

Ascolta il traffico generato dal modulo Wi-Fi TYWE1S (es. al reset o durante il funzionamento):

```powershell
python scripts/tuya_cli.py listen -d 30
```
Mentre il comando è in ascolto:
* Premi e rilascia **`WIFI RST`** (4° pulsante da sinistra).
* Il terminale mostrerà in tempo reale i pacchetti ricevuti dal modulo con timestamp e formattazione esadecimale/ASCII.

---

## 💾 Scenario 3: Backup Integrale Memoria Flash TYWE1S (2 MiB)

> [!IMPORTANT]
> Salvare il dump originale della memoria Flash del modulo TYWE1S prima di qualsiasi scrittura su ESP8266 è essenziale per non perdere la chiave di licenza Tuya Cloud pre-assegnata in fabbrica.

```powershell
python scripts/tuya_cli.py backup-esp -a 60
```

### Manovra sui Pulsanti durante la Connessione:
1. Avvia il comando sopra (tenterà la connessione per 60 secondi mostrando i punti di retry).
2. **Tieni premuto** il pulsante **`WIFI BOOT`** (3° da sinistra).
3. Con il pulsante ancora premuto, **premi e rilascia** il pulsante **`WIFI RST`** (4° da sinistra).
4. **Rilascia** il pulsante **`WIFI BOOT`**.
5. `esptool` aggancerà istantaneamente la ROM bootloader ed estrarrà il dump completo di 2 MiB in `firmware/tywe1s_factory_tuya_backup.bin`.

---

## 🔄 Scenario 4: Ripristino Firmware di Fabbrica Tuya su STM32

Quando hai terminato di operare sul modulo Wi-Fi e vuoi ripristinare la scheda esattamente nello stato di fabbrica originale Tuya:

1. **Metti l'STM32 in modalità Bootloader**:
   * Tieni premuto **`MCU BOOT`** (2° pulsante da sinistra).
   * Premi e rilascia **`MCU RST`** (1° pulsante da sinistra).
   * Rilascia **`MCU BOOT`**.
2. **Esegui il comando di ripristino**:
   ```powershell
   python scripts/tuya_cli.py restore-factory
   ```
3. Al termine della scrittura e verifica al 100%:
   * Premi e rilascia **`MCU RST`** (1° pulsante da sinistra) per avviare il firmware di fabbrica originale Tuya.

---

## 🛠️ Scenario 5: Ricompilazione e Verifica Binari Ponte

Per ricompilare da zero tutti i binari assembly (`smart_bridge`, `echo_bridge`) e verificarli con disassembler Capstone:

```powershell
python scripts/build_bridges.py
```

---

## ⚠️ Matrice di Troubleshooting

| Sintomo / Errore | Possibile Causa | Azione Correttiva |
| :--- | :--- | :--- |
| `FileNotFoundError: Could not open port COM3` | Cavo USB scollegato o driver CP2102 non caricato | Verificare collegamento micro-USB; verificare in Gestione Dispositivi che la porta Silicon Labs sia assegnata a `COM3`. |
| `tuya_cli.py ping` restituisce timeout o vuoto | L'STM32 è ancora fermo nella ST ROM bootloader | Premere e rilasciare **`MCU RST`** (1° pulsante da sinistra) per avviare l'esecuzione da Flash utente. |
| `esptool: No serial data received` | Il modulo TYWE1S non è entrato in UART Download Mode | Assicurarsi di tenere premuto `WIFI BOOT` **prima** di premere e rilasciare `WIFI RST`; verificare che tutti i 6 ponticelli (`P2`, `P3`, `P11`, `P13`) siano inseriti. |
| Byte `0x00` ricevuti a raffica al reset del Wi-Fi | Normale boot log ROM a 74880 baud campionato a 115200 | Il firmware `smart_bridge` azzera automaticamente l'errore hardware ORE; procedere regolarmente. |
