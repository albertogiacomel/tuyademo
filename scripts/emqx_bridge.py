#!/usr/bin/env python3
"""emqx_bridge.py - Bridge MQTT verso EMQX Cloud per Tuya TYDE 3.0.

Espone la telemetria della scheda (STM32F103 + TYWE1S) su un broker EMQX Cloud
e abilita il controllo remoto via topic MQTT. Il trasporto verso la scheda
resta la seriale USB (COM3) gestita dalla dashboard.

Sicurezza (R10):
- TLS 1.2+ verso il deployment EMQX Cloud (porta 8883).
- Credenziali MQTT lette dal Windows Credential Manager (target EMQX-MQTT,
  utente emqx-mqtt): mai in chiaro su file, log o artifact.
- Fail-soft: senza paho-mqtt o senza credenziali il bridge resta disattivato
  e la dashboard continua a funzionare in locale via USB.

Topic (default base tuya/tyde3):
- <base>/telemetry  : publish telemetria (qos 0, ogni telemetry_interval secondi)
- <base>/status     : retained online/offline (LWT online:false)
- <base>/command    : subscribe comandi JSON
- <base>/ack        : publish esito comandi
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from cyclopts import App, Parameter

logger = logging.getLogger("emqx_bridge")

DEFAULT_HOST = "r202df21.ala.eu-central-1.emqxsl.com"
DEFAULT_PORT = 8883
DEFAULT_TOPIC_BASE = "tuya/tyde3"
DEFAULT_INTERVAL = 5.0
CRED_TARGET = "EMQX-MQTT"
CRED_USERNAME = "emqx-mqtt"

TELEMETRY_FIELDS = (
    "temp_c",
    "vdd",
    "brightness",
    "mode",
    "r",
    "g",
    "b",
    "wifi_connected",
    "wifi_ip",
    "wifi_ssid",
    "wifi_rssi",
    "online",
)

app = App(name="emqx-bridge", help="Bridge MQTT (EMQX Cloud) per Tuya TYDE 3.0")


# --------------------------------------------------------------------------- #
# Windows Credential Manager (R10: nessun segreto su file o log)
# --------------------------------------------------------------------------- #
def read_credential(target: str = CRED_TARGET) -> tuple[str, str] | None:
    """Legge username/secret dal Windows Credential Manager. None se assente."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", wintypes.LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(ctypes.c_byte)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.restype = wintypes.BOOL
    cred_ptr = wintypes.LPWSTR()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        cred = ctypes.cast(cred_ptr, ctypes.POINTER(CREDENTIALW)).contents
        blob_size = int(cred.CredentialBlobSize)
        secret = ""
        if blob_size > 0:
            raw = ctypes.string_at(cred.CredentialBlob, blob_size)
            secret = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        return cred.UserName or "", secret
    finally:
        advapi32.CredFree(cred_ptr)


def write_credential(username: str, secret: str, target: str = CRED_TARGET) -> None:
    """Scrive una credenziale generica nel Credential Manager."""
    if sys.platform != "win32":
        raise OSError("Credential Manager disponibile solo su Windows")
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlobSizeMax", wintypes.DWORD),
            ("CredentialBlob", wintypes.LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(ctypes.c_byte)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob)
    cred = CREDENTIALW()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.UserName = username
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, wintypes.LPBYTE)
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW fallito")


def load_credentials() -> tuple[str, str] | None:
    """Credenziali da Credential Manager; fallback su variabili d'ambiente."""
    cred = read_credential(CRED_TARGET)
    if cred and cred[0] and cred[1]:
        return cred
    user = os.environ.get("EMQX_MQTT_USERNAME")
    password = os.environ.get("EMQX_MQTT_PASSWORD")
    if user and password:
        return user, password
    return None


def broker_tls_info(host: str, port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Info del certificato TLS del broker (probe senza credenziali)."""
    ctx = ssl.create_default_context()
    with (
        socket.create_connection((host, port), timeout=10) as sock,
        ctx.wrap_socket(sock, server_hostname=host) as tls,
    ):
        cert = tls.getpeercert()
        return {
            "host": host,
            "port": port,
            "tls_version": tls.version(),
            "subject": dict(x[0] for x in cert.get("subject", ())),
            "issuer": dict(x[0] for x in cert.get("issuer", ())),
            "not_after": cert.get("notAfter"),
        }


def build_telemetry(status: dict[str, Any]) -> dict[str, Any]:
    """Payload telemetria: whitelist campi + timestamp UTC (R51)."""
    payload: dict[str, Any] = {
        key: status[key] for key in TELEMETRY_FIELDS if key in status
    }
    leds = status.get("leds") or {}
    if isinstance(leds, dict):
        payload["leds"] = {
            str(led_id): {
                "mode": led.get("mode"),
                "period_ms": led.get("period_ms"),
            }
            for led_id, led in leds.items()
            if isinstance(led, dict)
        }
    payload["ts"] = datetime.now(UTC).isoformat()
    return payload


class EmqxBridge:
    """Client MQTT del server verso EMQX Cloud (thread-safe, fail-soft)."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        topic_base: str = DEFAULT_TOPIC_BASE,
        telemetry_interval: float = DEFAULT_INTERVAL,
        status_provider: Callable[[], dict[str, Any]] | None = None,
        command_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.topic_base = topic_base.rstrip("/")
        self.telemetry_interval = max(1.0, float(telemetry_interval))
        self._status_provider = status_provider
        self._command_handler = command_handler
        self.connect_timeout = max(1.0, float(connect_timeout))
        self._client: Any = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.enabled = True
        self.telemetry_sent = 0
        self.commands_received = 0
        self.last_payload: dict[str, Any] | None = None
        self.last_error: str | None = None

    # -- topic helpers ----------------------------------------------------- #
    @property
    def topic_telemetry(self) -> str:
        return f"{self.topic_base}/telemetry"

    @property
    def topic_command(self) -> str:
        return f"{self.topic_base}/command"

    @property
    def topic_status(self) -> str:
        return f"{self.topic_base}/status"

    @property
    def topic_ack(self) -> str:
        return f"{self.topic_base}/ack"

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> bool:
        """Avvia il bridge. Ritorna False (senza eccezione) se non pronto."""
        creds = load_credentials()
        if creds is None:
            self.enabled = False
            self.last_error = (
                "credenziali MQTT assenti (Credential Manager "
                f"'{CRED_TARGET}' o EMQX_MQTT_USERNAME/EMQX_MQTT_PASSWORD)"
            )
            logger.warning("EMQX bridge disattivato: %s", self.last_error)
            return False

        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            self.enabled = False
            self.last_error = f"paho-mqtt non installato: {exc}"
            logger.warning("EMQX bridge disattivato: %s", self.last_error)
            return False

        username, password = creds
        client_id = f"tyde3-server-{uuid.uuid4().hex[:8]}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        client.username_pw_set(username, password)
        client.tls_set()
        client.will_set(
            self.topic_status,
            payload=json.dumps({"online": False}),
            qos=1,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        try:
            client.connect_async(self.host, self.port, keepalive=60)
            client.loop_start()
        except Exception as exc:  # noqa: BLE001 - fail-soft con contesto (R67)
            self.enabled = False
            self.last_error = f"connessione EMQX fallita: {exc}"
            logger.warning("EMQX bridge disattivato: %s", self.last_error)
            return False

        self._client = client
        self.enabled = True
        self._stop_event.clear()
        self._wait_connected()
        self._thread = threading.Thread(
            target=self._telemetry_loop, name="emqx-telemetry", daemon=True
        )
        self._thread.start()
        logger.info(
            "EMQX bridge avviato: %s:%s client_id=%s topics=%s/#",
            self.host,
            self.port,
            client_id,
            self.topic_base,
        )
        return True

    def stop(self) -> None:
        """Ferma telemetria e client MQTT."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Stop EMQX bridge: %s", exc)
            self._client = None

    def _wait_connected(self) -> bool:
        """Attende il completamento del connect asincosto (evita publish a vuoto)."""
        if self._client is None:
            return False
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline:
            if self._client.is_connected():
                logger.info("EMQX connesso")
                return True
            time.sleep(0.1)
        logger.warning(
            "EMQX non ancora connesso dopo %.0fs (paho ritenta in background)",
            self.connect_timeout,
        )
        return False

    # -- callbacks paho ---------------------------------------------------- #
    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if str(reason_code) not in ("Success", "0"):
            self.last_error = f"CONNECT rifiutato: {reason_code}"
            logger.error("EMQX CONNECT rifiutato: rc=%s", reason_code)
            return
        client.subscribe(self.topic_command, qos=1)
        client.publish(
            self.topic_status,
            payload=json.dumps({"online": True}),
            qos=1,
            retain=True,
        )
        logger.info("EMQX connesso, subscribe %s", self.topic_command)

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        if self._stop_event.is_set():
            logger.info("EMQX disconnesso (arresto richiesto)")
            return
        logger.warning("EMQX disconnesso (paho ritenta automaticamente)")

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        self.commands_received += 1
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("payload comando non e' un oggetto JSON")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Comando MQTT non valido su %s: %s", msg.topic, exc)
            self.publish_json(self.topic_ack, {"ok": False, "error": str(exc)})
            return

        result: dict[str, Any]
        if self._command_handler is None:
            result = {"ok": False, "error": "command_handler non configurato"}
        else:
            try:
                result = {"ok": True, "result": self._command_handler(payload)}
            except Exception as exc:  # noqa: BLE001 - contesto preservato (R67)
                result = {"ok": False, "error": str(exc)}
        logger.info("Comando MQTT %s -> %s", payload, result)
        self.publish_json(self.topic_ack, result)

    # -- publish ----------------------------------------------------------- #
    def publish_json(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> bool:
        if self._client is None:
            return False
        try:
            info = self._client.publish(
                topic, payload=json.dumps(payload), qos=qos, retain=retain
            )
            return info.rc == 0
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.warning("Publish MQTT fallita su %s: %s", topic, exc)
            return False

    def publish_telemetry(self) -> bool:
        """Pubblica uno snapshot di telemetria dal provider di stato."""
        if self._client is None or self._status_provider is None:
            return False
        try:
            payload = build_telemetry(self._status_provider())
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"telemetria non generata: {exc}"
            logger.warning("%s", self.last_error)
            return False
        ok = self.publish_json(self.topic_telemetry, payload, qos=0)
        if ok:
            self.telemetry_sent += 1
            self.last_payload = payload
        return ok

    def _telemetry_loop(self) -> None:
        while not self._stop_event.wait(self.telemetry_interval):
            self.publish_telemetry()

    # -- status ------------------------------------------------------------ #
    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected())

    def status(self) -> dict[str, Any]:
        with self._lock:
            connected = bool(self._client is not None and self._client.is_connected())
            return {
                "enabled": self.enabled,
                "connected": connected,
                "broker": f"{self.host}:{self.port}",
                "topics": {
                    "telemetry": self.topic_telemetry,
                    "command": self.topic_command,
                    "status": self.topic_status,
                    "ack": self.topic_ack,
                },
                "telemetry_sent": self.telemetry_sent,
                "commands_received": self.commands_received,
                "interval_s": self.telemetry_interval,
                "last_error": self.last_error,
            }


def make_command_handler(board: Any) -> Callable[[dict[str, Any]], Any]:
    """Traduce comandi MQTT JSON in chiamate al bridge seriale della scheda."""

    def handle(payload: dict[str, Any]) -> Any:
        cmd = str(payload.get("cmd", "")).lower().strip()
        if cmd == "rgb":
            return board.set_rgb(
                int(payload.get("r", 0)),
                int(payload.get("g", 0)),
                int(payload.get("b", 0)),
            )
        if cmd == "brightness":
            return board.set_brightness(int(payload.get("brightness", 20)))
        if cmd == "mode":
            return {"ok": board.set_mode(str(payload.get("mode", "")))}
        if cmd == "led":
            return board.set_led(
                int(payload.get("led", 0)),
                str(payload.get("mode", "")),
                int(payload.get("period_ms", 500)),
            )
        if cmd == "ping":
            return board.ping()
        if cmd in ("wifi", "wifi_status"):
            return board.query_wifi()
        raise ValueError("cmd non supportato: rgb|brightness|mode|led|ping|wifi")

    return handle


# --------------------------------------------------------------------------- #
# CLI (Cyclopts - R5.1)
# --------------------------------------------------------------------------- #
@app.command
def setup() -> None:
    """Salva le credenziali MQTT nel Windows Credential Manager (target EMQX-MQTT).

    Accetta anche EMQX_MQTT_USERNAME / EMQX_MQTT_PASSWORD da ambiente
    (utile in sessioni non interattive); altrimenti richiede l'input.
    """
    import getpass

    username = (
        os.environ.get("EMQX_MQTT_USERNAME") or input("Username MQTT EMQX: ").strip()
    )
    secret = os.environ.get("EMQX_MQTT_PASSWORD") or getpass.getpass(
        "Password/secret MQTT EMQX (input nascosto): "
    )
    if not username or not secret:
        print("[X] username o secret vuoti: operazione annullata")
        return
    write_credential(username, secret)
    print(f"[+] Credenziale salvata: target={CRED_TARGET} username={username}")


@app.command
def check(
    host: Annotated[str, Parameter(name=["-H", "--host"])] = DEFAULT_HOST,
    port: Annotated[int, Parameter(name=["-p", "--port"])] = DEFAULT_PORT,
) -> None:
    """Verifica TLS del broker e, se presenti, CONNECT MQTT con le credenziali."""
    info = broker_tls_info(host, port)
    print("[+] TLS broker verificato:")
    print(f"    endpoint : {info['host']}:{info['port']}")
    print(f"    tls      : {info['tls_version']}")
    print(f"    subject  : {info['subject']}")
    print(f"    issuer   : {info['issuer']}")
    print(f"    scadenza : {info['not_after']}")

    creds = load_credentials()
    if creds is None:
        print("[!] Credenziali MQTT assenti: esegui 'emqx_bridge.py setup'")
        return

    bridge = EmqxBridge(host=host, port=port)
    try:
        import paho.mqtt.client as mqtt

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"tyde3-check-{uuid.uuid4().hex[:8]}",
                )
                client.username_pw_set(*creds)
                client.tls_set()
                client.connect(host, port, keepalive=30)
                print(f"[+] MQTT CONNECT accettato dal broker (tentativo {attempt})")
                client.disconnect()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - retry DNS intermittente
                last_exc = exc
                print(f"[!] tentativo {attempt}/3 fallito: {exc}")
                time.sleep(1.0)
        if last_exc is not None:
            print(f"[X] MQTT CONNECT fallito dopo 3 tentativi: {last_exc}")
    except ImportError:
        print("[!] paho-mqtt non installato (pip install paho-mqtt)")
    del bridge


ACL_PROBE_TOPICS = (
    "tuya/tyde3/telemetry",
    "tuya/tyde3/command",
    "tuya/tyde3/status",
    "tuya/tyde3/ack",
    "Telemetry",
)

_REASON_TEXT = {
    "0": "GRANTED (qos0)",
    "1": "GRANTED (qos1)",
    "2": "GRANTED (qos2)",
    "128": "errore non specificato",
    "135": "NOT AUTHORIZED (fuori ACL)",
}


def _describe_reason(reason: Any) -> str:
    text = str(reason)
    return f"{text} [{_REASON_TEXT.get(text, '?')}]"


@app.command
def acl(
    topics: Annotated[list[str], Parameter(name=["-t", "--topic"])] | None = None,
    host: Annotated[str, Parameter(name=["-H", "--host"])] = DEFAULT_HOST,
    port: Annotated[int, Parameter(name=["-p", "--port"])] = DEFAULT_PORT,
    verbose: Annotated[bool, Parameter(name=["-v", "--verbose"])] = False,
) -> None:
    """Verifica i permessi ACL: subscribe+publish sui topic e riporta l'esito.

    Default: i 4 topic del bridge piu' 'Telemetry' (la regola presente in console).
    Esito GRANTED = topic autorizzato; NOT AUTHORIZED = serve una regola ACL.
    """
    creds = load_credentials()
    if creds is None:
        print("[X] credenziali MQTT assenti: esegui 'emqx_bridge.py setup'")
        return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[!] paho-mqtt non installato (pip install paho-mqtt)")
        return

    if topics is None:
        topics = []
    probe = list(topics) or list(ACL_PROBE_TOPICS)
    sub_results: dict[int, str] = {}
    expected: dict[int, str] = {}
    pub_results: dict[int, tuple[str, str]] = {}
    disconnect_rc: list[str] = []

    def _on_connect(
        client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if str(reason_code) not in ("Success", "0"):
            print(f"[X] CONNECT rifiutato: {reason_code}")
            return
        print(f"[+] CONNECT accettato, probe su {len(probe)} topic")
        for topic in probe:
            res = client.subscribe(topic, qos=1)
            mid = res[1] if isinstance(res, tuple) else res
            expected[mid] = topic
        for topic in ("tuya/tyde3/telemetry", "Telemetry"):
            info = client.publish(topic, payload="acl-probe", qos=1)
            pub_results[info.mid] = (topic, "")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"tyde3-acl-{uuid.uuid4().hex[:8]}",
    )
    client.username_pw_set(*creds)
    client.tls_set()
    client.on_connect = _on_connect
    client.on_subscribe = lambda c, u, mid, rcs, props: sub_results.__setitem__(
        mid, _describe_reason(rcs[0] if rcs else "?")
    )
    client.on_publish = lambda c, u, mid, rc, props: pub_results.__setitem__(
        mid, (pub_results.get(mid, ("?", ""))[0], str(rc))
    )
    client.on_message = lambda c, u, msg: print(
        f"  MESSAGGIO su {msg.topic}: {msg.payload[:60]!r}"
    )
    if verbose:
        client.on_log = lambda c, u, level, buf: print(f"  [paho {level}] {buf}")
    client.on_disconnect = lambda c, u, *a: disconnect_rc.append(
        str(a[1] if a else "?")
    )

    try:
        client.connect(host, port, keepalive=30)
    except Exception as exc:  # noqa: BLE001
        print(f"[X] connessione fallita: {exc}")
        return
    client.loop_start()
    time.sleep(8.0)
    print(
        f"\n[debug] is_connected={client.is_connected()} suback_ricevuti={len(sub_results)}"
    )
    client.loop_stop()
    client.disconnect()

    print("\n--- SUBSCRIBE ---")
    for mid, topic in expected.items():
        print(f"  {topic:24} -> {sub_results.get(mid, 'nessuna risposta (timeout)')}")
    print("\n--- PUBLISH (qos 1) ---")
    for mid, (topic, rc) in pub_results.items():
        stato = rc if rc else "nessun PUBACK"
        print(f"  {topic:24} -> {stato}")
    if disconnect_rc:
        print(f"\n[!] disconnesso dal broker: rc={disconnect_rc[0]}")


@app.command
def send(
    topic: str,
    payload: Annotated[str, Parameter(name=["-m", "--message"])] = "hello",
    qos: Annotated[int, Parameter(name=["-q", "--qos"])] = 0,
    retain: Annotated[bool, Parameter(name=["-r", "--retain"])] = False,
) -> None:
    """Pubblica un messaggio di test su un topic del deployment EMQX."""
    bridge = EmqxBridge()
    if not bridge.start():
        print(f"[X] Bridge non avviato: {bridge.last_error}")
        return
    if not bridge.is_connected():
        print(f"[!] client non ancora connesso: {bridge.last_error or 'in corso'}")
    ok = bridge.publish_json(topic, {"msg": payload}, qos=qos, retain=retain)
    print(f"[{'+' if ok else 'X'}] publish {topic} -> {payload}")
    bridge.stop()


@app.command
def run(
    port: Annotated[str, Parameter(name=["-p", "--port"])] = "COM3",
    baud: Annotated[int, Parameter(name=["-b", "--baud"])] = 115200,
    interval: Annotated[float, Parameter(name=["-i", "--interval"])] = DEFAULT_INTERVAL,
) -> None:
    """Avvia il bridge standalone: seriale COM3 -> EMQX Cloud (senza dashboard)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tuya_web import BoardBridge

    board = BoardBridge(port=port, baud=baud)
    bridge = EmqxBridge(
        telemetry_interval=interval,
        status_provider=lambda: board.status,
        command_handler=make_command_handler(board),
    )
    if not bridge.start():
        print(f"[X] Bridge non avviato: {bridge.last_error}")
        return
    print(f"[+] Bridge attivo su {bridge.host}:{bridge.port}; Ctrl+C per uscire")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[!] Arresto richiesto")
    finally:
        bridge.stop()
        board.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    app()
