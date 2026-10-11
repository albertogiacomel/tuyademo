#!/usr/bin/env python3
"""tuya_cli.py - Master CLI for Tuya TYDE 3.0 IoT Development Board.

Refactored to Cyclopts following SOUL_GLOBAL / QwenPaw / CoPaw methodology:
- Cyclopts App & Annotated parameters (R-QCLI / canonical CLI architecture)
- Explicit UTF-8 encoding (R49)
- Structured error handling (R67)
- Zero broken code & zero warnings (R58, R24)
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import serial
from cyclopts import App, Parameter

# R49: Explicit UTF-8 console streams on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = BASE_DIR / "firmware"
DEFAULT_PORT = "COM3"
DEFAULT_BAUD = 115200

app = App(
    name="tuya-cli",
    help="Tuya TYDE 3.0 Board Toolchain CLI (SOUL_GLOBAL compliant, Cyclopts-powered)",
)


def get_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.command
def ping(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
    timeout: Annotated[
        float, Parameter(name=["-t", "--timeout"], help="Timeout in secondi")
    ] = 0.5,
) -> None:
    """Invia il byte diagnostico '?' allo Smart Bridge STM32 e attende 'OK\\n'."""
    print(
        f"[{get_timestamp()}] Inviando ping diagnostico '?' su {port} ({baud} baud)..."
    )
    try:
        with serial.Serial(port, baud, timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b"?")
            ser.flush()
            time.sleep(0.05)
            resp = ser.read(16)
            if resp == b"OK\n" or b"OK" in resp:
                print(
                    f"[+] SUCCESSO: STM32 Smart Bridge attivo e rispondente! Risposta: {resp!r}"
                )
            elif resp:
                print(f"[!] RICEVUTO DATO IMPREVISTO: {resp!r} (HEX: {resp.hex()})")
                sys.exit(1)
            else:
                print(
                    "[-] NESSUNA RISPOSTA: verificare alimentazione, ponticelli P2 o riavviare con MCU RST."
                )
                sys.exit(1)
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore apertura porta seriale {port}: {exc}")
        sys.exit(2)


@app.command(name="diag")
def diag(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
) -> None:
    """Esegue diagnosi hardware pin PB11 (TYWE1S TX), test 9600 Tuya e 115200 ESP bootloader."""
    print(f"[{get_timestamp()}] Avvio diagnostica hardware su {port} ({baud} baud)...")
    try:
        with serial.Serial(port, baud, timeout=3.0) as ser:
            ser.reset_input_buffer()
            ser.write(b"D\n")
            ser.flush()
            t0 = time.time()
            while time.time() - t0 < 4.0:
                line = ser.readline().decode("ascii", errors="replace")
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if "=== HARDWARE DIAGNOSTIC END ===" in line:
                        break
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore apertura porta seriale {port}: {exc}")
        sys.exit(2)


@app.command(name="sniff")
def sniff(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
) -> None:
    """Attiva il Boot Sniffer ESP8266 a 74880 baud per 15s su STM32."""
    print(f"[{get_timestamp()}] Avvio Boot Sniffer ESP8266 su {port}...")
    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            ser.reset_input_buffer()
            ser.write(b"S\n")
            ser.flush()
            t0 = time.time()
            while time.time() - t0 < 16.0:
                data = ser.read(128)
                if data:
                    text = data.decode("ascii", errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    if "=== SNIFFER END ===" in text:
                        break
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore apertura porta seriale {port}: {exc}")
        sys.exit(2)


@app.command(name="read-temp")
def read_temp(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
    timeout: Annotated[
        float, Parameter(name=["-t", "--timeout"], help="Timeout in secondi")
    ] = 1.0,
) -> None:
    """Interroga il sensore di temperatura interno dell'STM32 (ADC1_IN16) e VREFINT (ADC1_IN17)."""
    print(f"[{get_timestamp()}] Richiesta lettura ADC1_IN16 su {port} ({baud} baud)...")
    try:
        with serial.Serial(port, baud, timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b"T")
            ser.flush()
            line = ser.readline().decode("ascii", errors="replace").strip()
            if not line:
                print("[-] Nessuna risposta ricevuta entro il timeout.")
                print(
                    "[*] NOTA: Assicurarsi che lo Smart Bridge aggiornato sia stato caricato su STM32."
                )
                sys.exit(1)

            # Formato atteso: T:XXXX,V:YYYY
            if not (line.startswith("T:") and ",V:" in line):
                print(f"[!] Risposta non conforme o byte bridge: {line!r}")
                sys.exit(1)

            parts = line.split(",")
            hex_temp = parts[0].split(":")[1]
            hex_vref = parts[1].split(":")[1]

            raw_temp = int(hex_temp, 16)
            raw_vref = int(hex_vref, 16)

            # Calcolo VDD reale tramite canale 17 VREFINT (1.20V nominale STM32F103)
            vdd = (1.20 * 4096.0 / raw_vref) if raw_vref > 0 else 3.30

            # Tensione rilevata su sensore di temperatura
            vsense = (raw_temp / 4096.0) * vdd

            # Formula Reference Manual ST RM0008 §11.10:
            # Temperature (°C) = ((V25 - VSENSE) / Avg_Slope) + 25
            # V25 tipico = 1.43V, Avg_Slope tipico = 4.3 mV/°C (0.0043 V/°C)
            temp_c = ((1.43 - vsense) / 0.0043) + 25.0

            print("\n" + "=" * 56)
            print("  🌡️  LETTURA SENSORE TEMPERATURA STM32 (ADC1_IN16)")
            print("=" * 56)
            print(f"  • Raw ADC16 (Temp)       : {raw_temp:5d}  (0x{hex_temp})")
            print(f"  • Raw ADC17 (Vrefint)    : {raw_vref:5d}  (0x{hex_vref})")
            print(f"  • Tensione VDD Misurata  : {vdd:.3f} V")
            print(f"  • Tensione VSENSE        : {vsense:.3f} V")
            print(f"  • Temperatura Core MCU   : {temp_c:+.1f} °C")
            print("=" * 56 + "\n")
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore apertura porta seriale {port}: {exc}")
        sys.exit(2)


@app.command(name="rgb")
def rgb(
    color: Annotated[
        str | None,
        Parameter(
            name=["-c", "--color"],
            help="Colore predefinito (red, green, blue, yellow, cyan, magenta, white, off, temp, rainbow)",
        ),
    ] = None,
    r: Annotated[
        int | None, Parameter(name=["-r"], help="Canale Rosso PWM (0-255)")
    ] = None,
    g: Annotated[
        int | None, Parameter(name=["-g"], help="Canale Verde PWM (0-255)")
    ] = None,
    b: Annotated[
        int | None, Parameter(name=["-b"], help="Canale Blu PWM (0-255)")
    ] = None,
    brightness: Annotated[
        int | None,
        Parameter(
            name=["-l", "--brightness"],
            help="Luminosità globale in percentuale (0-100%)",
        ),
    ] = None,
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
) -> None:
    """Controlla il LED RGB on-board su TIM4 (colori, PWM, luminosità o termometro visivo)."""
    color_map = {
        "red": "C:255,0,0\n",
        "green": "C:0,255,0\n",
        "blue": "C:0,0,255\n",
        "yellow": "C:255,200,0\n",
        "cyan": "C:0,255,255\n",
        "magenta": "C:255,0,255\n",
        "white": "C:255,255,255\n",
        "off": "C:0,0,0\n",
        "temp": "C:TEMP\n",
        "rainbow": "C:RAINBOW\n",
    }

    commands: list[str] = []

    if brightness is not None:
        b_val = max(0, min(100, brightness))
        commands.append(f"C:B:{b_val}\n")

    if color:
        c_lower = color.lower()
        if c_lower in color_map:
            commands.append(color_map[c_lower])
        else:
            print(
                f"[-] Colore non riconosciuto: {color}. Opzioni: {list(color_map.keys())}"
            )
            sys.exit(1)
    elif r is not None or g is not None or b is not None:
        r_val = max(0, min(255, r or 0))
        g_val = max(0, min(255, g or 0))
        b_val = max(0, min(255, b or 0))
        commands.append(f"C:{r_val},{g_val},{b_val}\n")

    if not commands:
        print("[!] Specificare --color <nome>, -r/-g/-b oppure --brightness <0-100>.")
        sys.exit(1)

    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            ser.reset_input_buffer()
            for msg in commands:
                print(
                    f"[{get_timestamp()}] Invio comando RGB su {port}: {msg.strip()!r}..."
                )
                ser.write(msg.encode("ascii"))
                ser.flush()
                resp = ser.readline().decode("ascii", errors="replace").strip()
                print(f"[+] Risposta STM32: {resp!r}")
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore apertura porta seriale {port}: {exc}")
        sys.exit(2)


@app.command
def listen(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
    duration: Annotated[
        float, Parameter(name=["-d", "--duration"], help="Durata in secondi")
    ] = 15.0,
) -> None:
    """Monitor seriale passivo: cattura e decodifica tutti i frame ricevuti."""
    print(
        f"[{get_timestamp()}] In ascolto su {port} ({baud} baud) per {duration} secondi..."
    )
    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            ser.reset_input_buffer()
            t0 = time.time()
            total_bytes = 0
            while time.time() - t0 < duration:
                data = ser.read(128)
                if data:
                    total_bytes += len(data)
                    ts = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
                    print(
                        f"[{ts}] +{len(data):3d}B | HEX: {data.hex():40s} | ASCII: {data!r}"
                    )
            print(
                f"[{get_timestamp()}] Ascolto terminato. Totale byte ricevuti: {total_bytes}"
            )
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore porta {port}: {exc}")
        sys.exit(2)


@app.command
def heartbeat(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
) -> None:
    """Invia frame Tuya MCU SDK Heartbeat standard (55AA00000000FF) a TYWE1S."""
    frame = bytes.fromhex("55AA00000000FF")
    print(f"[{get_timestamp()}] Invio Tuya MCU Heartbeat ({frame.hex()}) su {port}...")
    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            ser.reset_input_buffer()
            ser.write(frame)
            ser.flush()
            resp = ser.read(128)
            if resp:
                print(
                    f"[+] Ricevuti {len(resp)} byte in risposta: HEX={resp.hex()} | {resp!r}"
                )
            else:
                print("[-] Nessuna risposta ricevuta dal modulo Wi-Fi.")
                sys.exit(1)
    except (serial.SerialException, OSError) as exc:
        print(f"[-] Errore: {exc}")
        sys.exit(2)


@app.command(name="flash-bridge")
def flash_bridge(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
) -> None:
    """Carica il firmware Smart Bridge su STM32 via stm32loader."""
    bin_file = FIRMWARE_DIR / "stm32_uart_smart_bridge.bin"
    if not bin_file.exists():
        print(f"[-] File binario non trovato: {bin_file}")
        sys.exit(1)
    cmd = [
        sys.executable,
        "-m",
        "stm32loader",
        "-p",
        port,
        "-f",
        "F1",
        "-e",
        "-w",
        "-v",
        str(bin_file),
    ]
    print(f"[{get_timestamp()}] Esecuzione flash ponte STM32: {' '.join(cmd)}")
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        sys.exit(res.returncode)


@app.command(name="build-pio")
def build_pio() -> None:
    """Compila il firmware C++ STM32 tramite PlatformIO Core e aggiorna firmware/."""
    print(f"[{get_timestamp()}] Compilazione firmware PlatformIO (stm32_bridge)...")
    cmd = [sys.executable, "-m", "platformio", "run", "-d", str(BASE_DIR)]
    res = subprocess.run(cmd, cwd=BASE_DIR, check=False)
    if res.returncode != 0:
        print("[-] Compilazione fallita.")
        sys.exit(res.returncode)

    src_bin = BASE_DIR / ".pio" / "build" / "stm32_bridge" / "firmware.bin"
    dst_bin = FIRMWARE_DIR / "stm32_pio_smart_bridge.bin"
    if src_bin.exists():
        shutil.copy2(src_bin, dst_bin)
        h = hashlib.sha256(dst_bin.read_bytes()).hexdigest()
        print(
            f"[+] Firmware C++ generato con successo: {dst_bin} ({dst_bin.stat().st_size} bytes)"
        )
        print(f"    SHA256: {h}")


@app.command(name="flash-pio")
def flash_pio(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    attempts: Annotated[
        int, Parameter(name=["-a", "--attempts"], help="Finestra tentativi (secondi)")
    ] = 30,
) -> None:
    """Carica il firmware C++ PlatformIO (stm32_pio_smart_bridge.bin) su STM32 via stm32loader."""
    bin_file = FIRMWARE_DIR / "stm32_pio_smart_bridge.bin"
    if not bin_file.exists():
        print(
            f"[-] File non trovato: {bin_file}. Eseguire prima 'python scripts/tuya_cli.py build-pio'."
        )
        sys.exit(1)
    cmd = [
        sys.executable,
        "-m",
        "stm32loader",
        "-p",
        port,
        "-f",
        "F1",
        "-e",
        "-w",
        "-v",
        str(bin_file),
    ]
    print(
        f"[{get_timestamp()}] Flashing PlatformIO C++ firmware su STM32 ({attempts}s finestra)..."
    )

    # Tentativo software di salto in bootloader (comando 'B')
    try:
        with serial.Serial(port, DEFAULT_BAUD, timeout=0.1) as ser:
            ser.write(b"B")
            ser.flush()
            time.sleep(0.1)
    except (serial.SerialException, OSError):
        pass  # MCU già in bootloader o spento: prosegue con tentativi standard

    print(
        "  [👉 Se non si connette in automatico: tenere premuto S2: MCU BOOT e cliccare S1: MCU RST]"
    )

    t0 = time.time()
    success = False
    while time.time() - t0 < attempts:
        try:
            with serial.Serial(port, DEFAULT_BAUD, timeout=0.05) as ser:
                ser.write(b"B")
                ser.flush()
                time.sleep(0.05)
        except (serial.SerialException, OSError):
            pass
        res = subprocess.run(cmd, check=False)
        if res.returncode == 0:
            success = True
            break
        time.sleep(0.5)

    if not success:
        print("[-] Timeout connessione bootloader STM32.")
        sys.exit(1)

    print(f"[{get_timestamp()}] Flashing completato! Avvio applicazione in Flash...")
    # Auto-boot application via Go command
    boot_cmd = [
        sys.executable,
        "-m",
        "stm32loader",
        "-p",
        port,
        "-f",
        "F1",
        "-g",
        "0x08000000",
    ]
    subprocess.run(boot_cmd, check=False)


@app.command(name="restore-factory")
def restore_factory(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    attempts: Annotated[
        int, Parameter(name=["-a", "--attempts"], help="Finestra tentativi (secondi)")
    ] = 45,
) -> None:
    """Ripristina il firmware di fabbrica originale Tuya su STM32."""
    bin_file = FIRMWARE_DIR / "stm32_factory_tuya_backup.bin"
    if not bin_file.exists():
        print(f"[-] File backup di fabbrica non trovato: {bin_file}")
        sys.exit(1)
    cmd = [
        sys.executable,
        "-m",
        "stm32loader",
        "-p",
        port,
        "-f",
        "F1",
        "-e",
        "-w",
        "-v",
        str(bin_file),
    ]
    print(
        f"[{get_timestamp()}] Ripristino firmware di fabbrica Tuya su STM32 ({attempts}s finestra)..."
    )
    print(
        "  [👉 Tieni premuto S2: MCU BOOT, clicca S1: MCU RST, poi rilascia S2: MCU BOOT]"
    )

    t0 = time.time()
    success = False
    while time.time() - t0 < attempts:
        res = subprocess.run(cmd, check=False)
        if res.returncode == 0:
            success = True
            break
        time.sleep(0.5)

    if not success:
        print("[-] Timeout connessione bootloader STM32.")
        sys.exit(1)

    print(
        f"[{get_timestamp()}] Ripristino di fabbrica completato con successo! Avvio applicazione..."
    )
    boot_cmd = [
        sys.executable,
        "-m",
        "stm32loader",
        "-p",
        port,
        "-f",
        "F1",
        "-g",
        "0x08000000",
    ]
    subprocess.run(boot_cmd, check=False)


@app.command(name="backup-esp")
def backup_esp(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
    attempts: Annotated[
        int, Parameter(name=["-a", "--attempts"], help="Tentativi di connessione")
    ] = 30,
) -> None:
    """Esegue dump completo di 2 MiB della memoria flash del TYWE1S via esptool."""
    out_file = FIRMWARE_DIR / "tywe1s_factory_tuya_backup.bin"

    # Attiva la modalità Passthrough pura su STM32 (Serial3 <-> Serial @ 115200)
    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            ser.write(b"P\n")
            ser.flush()
            time.sleep(0.05)
    except (serial.SerialException, OSError):
        pass

    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "-p",
        port,
        "-c",
        "esp8266",
        "-b",
        str(baud),
        "--before",
        "no-reset",
        "-a",
        "no-reset",
        "--connect-attempts",
        str(attempts),
        "read-flash",
        "0",
        "0x200000",
        str(out_file),
    ]
    print(
        f"[{get_timestamp()}] Avvio backup flash TYWE1S (ESP8266, 2 MiB) su {out_file}..."
    )
    print(
        ">>> SUGGERIMENTO: Mentre esptool tenta la connessione, tieni premuto S3: WIFI BOOT e clicca S4: WIFI RST!"
    )
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        sys.exit(res.returncode)


@app.command(name="flash-esp")
def flash_esp(
    port: Annotated[
        str, Parameter(name=["-p", "--port"], help="Porta seriale COM")
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate")
    ] = DEFAULT_BAUD,
    attempts: Annotated[
        int, Parameter(name=["-a", "--attempts"], help="Tentativi di connessione")
    ] = 120,
) -> None:
    """Carica il firmware Wi-Fi C++ PlatformIO (tywe1s_wifi_firmware.bin) sul modulo TYWE1S via esptool."""
    bin_file = FIRMWARE_DIR / "tywe1s_wifi_firmware.bin"
    if not bin_file.exists():
        print(f"[-] File binario non trovato: {bin_file}")
        sys.exit(1)

    # Attiva la modalità Passthrough pura su STM32 (Serial3 <-> Serial @ 115200)
    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            ser.write(b"P\n")
            ser.flush()
            time.sleep(0.05)
    except (serial.SerialException, OSError):
        pass

    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "-p",
        port,
        "-c",
        "esp8266",
        "-b",
        str(baud),
        "--before",
        "no-reset",
        "-a",
        "no-reset",
        "--connect-attempts",
        str(attempts),
        "write-flash",
        "--flash-size",
        "detect",
        "0x0",
        str(bin_file),
    ]
    print(
        f"[{get_timestamp()}] Avvio scrittura flash TYWE1S (ESP8266) con {bin_file.name}..."
    )
    print(
        ">>> SUGGERIMENTO: Mentre esptool tenta la connessione, tieni premuto S3: WIFI BOOT e clicca S4: WIFI RST!"
    )
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        sys.exit(res.returncode)
    print(
        f"[{get_timestamp()}] Flashing TYWE1S completato con successo! Clicca S4: WIFI RST per avviare."
    )


@app.command(name="serve")
def serve(
    host: Annotated[
        str, Parameter(name=["-H", "--host"], help="Indirizzo IP host di ascolto")
    ] = "0.0.0.0",
    port: Annotated[
        int,
        Parameter(
            name=["-p", "--port"],
            help="Porta HTTP per Web Dashboard e REST API",
        ),
    ] = 8080,
    serial_port: Annotated[
        str,
        Parameter(name=["-s", "--serial-port"], help="Porta seriale STM32 COM"),
    ] = DEFAULT_PORT,
    baud: Annotated[
        int, Parameter(name=["-b", "--baud"], help="Baud rate seriale")
    ] = DEFAULT_BAUD,
) -> None:
    """Avvia il server Web Dashboard e REST API per controllo da smartphone/PC su rete Wi-Fi."""
    from tuya_web import BoardBridge, create_app

    print("=" * 64)
    print("  🌐 TUYA TYDE 3.0 — WEB DASHBOARD & REST API SERVER")
    print("=" * 64)
    print(f"  • Seriale target    : {serial_port} @ {baud} baud")
    print(f"  • Dashboard Web     : http://localhost:{port}/")
    print(
        f"  • Accesso Wi-Fi/LAN : http://0.0.0.0:{port}/ (oppure IP locale del tuo PC)"
    )
    print(f"  • REST API Status   : http://localhost:{port}/api/status")
    print("=" * 64 + "\n")

    bridge = BoardBridge(port=serial_port, baud=baud)
    server_app = create_app(bridge)
    server_app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    app()
