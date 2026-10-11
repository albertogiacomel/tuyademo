#!/usr/bin/env python3
"""emqx_cloud.py - Client REST API v5 per il deployment EMQX Cloud del kit Tuya.

Accede al Broker Deployment API del deployment:

    https://r202df21.ala.eu-central-1.emqxsl.com:8443/api/v5

Autenticazione HTTP Basic con App ID + App Secret del deployment.
Credenziali dal Windows Credential Manager (target EMQX-API, username = App ID),
mai in chiaro su file, log o artifact (R10).

Comandi read-only di default; la publish e' l'unica scrittura ed e' esplicita.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated, Any

import requests
from cyclopts import App, Parameter
from emqx_bridge import read_credential, write_credential

logger = logging.getLogger("emqx_cloud")

DEFAULT_BASE = "https://r202df21.ala.eu-central-1.emqssl.com:8443/api/v5"
CRED_TARGET_API = "EMQX-API"
DEFAULT_TIMEOUT = 15.0

app = App(name="emqx-cloud", help="Client REST API v5 del deployment EMQX Cloud")


def load_api_credentials() -> tuple[str, str] | None:
    """(App ID, App Secret) da Credential Manager o ambiente."""
    cred = read_credential(CRED_TARGET_API)
    if cred and cred[0] and cred[1]:
        return cred
    app_id = os.environ.get("EMQX_API_APP_ID")
    secret = os.environ.get("EMQX_API_APP_SECRET")
    if app_id and secret:
        return app_id, secret
    return None


class CloudClient:
    """Client minimale dell'API v5 (solo stdlib + requests)."""

    def __init__(
        self, base: str = DEFAULT_BASE, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> requests.Response:
        creds = load_api_credentials()
        if creds is None:
            raise RuntimeError(
                "credenziali REST assenti: esegui 'emqx_cloud.py setup' "
                "(Credential Manager EMQX-API o EMQX_API_APP_ID/EMQX_API_APP_SECRET)"
            )
        return requests.request(
            method,
            self._url(path),
            params=params,
            json=payload,
            auth=requests.auth.HTTPBasicAuth(creds[0], creds[1]),
            timeout=self.timeout,
            headers={"User-Agent": "tuya-tyde3-emqx-cloud/1.0"},
        )

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("GET", path, params=params)
        _raise_for_status(resp)
        return resp.json()

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        resp = self.request("POST", path, payload=payload)
        _raise_for_status(resp)
        return resp.json()


def _raise_for_status(resp: requests.Response) -> None:
    if resp.ok:
        return
    detail = ""
    try:
        detail = json.dumps(resp.json())[:200]
    except (ValueError, TypeError):
        detail = resp.text[:200]
    if resp.status_code == 401:
        raise RuntimeError(f"401 Unauthorized: credenziali non valide ({detail})")
    if resp.status_code == 404:
        raise RuntimeError(f"404 Not Found: endpoint inesistente ({detail})")
    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")


def print_clients(data: Any) -> None:
    """Tabella compatta dei client connessi."""
    rows = data.get("data", []) if isinstance(data, dict) else data
    if not rows:
        print("[!] nessun client nella risposta")
        return
    print(f"{'CLIENT ID':28} {'USERNAME':10} {'IP':16} {'CONNECTED':9} CONNECTED AT")
    for row in rows:
        print(
            f"{str(row.get('clientid', ''))[:27]:28} "
            f"{str(row.get('username') or '-')[:9]:10} "
            f"{str(row.get('ip_address') or '-')[:15]:16} "
            f"{row.get('connected', '?')!s:9} "
            f"{row.get('connected_at', '-')}"
        )
    meta = data.get("meta") if isinstance(data, dict) else None
    if isinstance(meta, dict):
        print(f"\nTotale: {meta.get('count')} client (pagina {meta.get('page')})")


# --------------------------------------------------------------------------- #
# CLI (Cyclopts - R5.1)
# --------------------------------------------------------------------------- #
@app.command
def setup() -> None:
    """Salva App ID/App Secret nel Credential Manager (target EMQX-API)."""
    app_id = os.environ.get("EMQX_API_APP_ID") or input("App ID EMQX: ").strip()
    secret = os.environ.get("EMQX_API_APP_SECRET")
    if not secret:
        import getpass

        secret = getpass.getpass("App Secret EMQX (input nascosto): ")
    if not app_id or not secret:
        print("[X] App ID o App Secret vuoti: operazione annullata")
        return
    write_credential(app_id, secret, target=CRED_TARGET_API)
    print(f"[+] Credenziale REST salvata: target={CRED_TARGET_API} app_id={app_id}")


@app.command
def check(
    base: Annotated[str, Parameter(name=["-b", "--base"])] = DEFAULT_BASE,
) -> None:
    """Verifica endpoint e autenticazione (GET /api/v5/status)."""
    client = CloudClient(base=base)
    data = client.get_json("status")
    print("[+] Autenticazione OK, risposta /status:")
    print(json.dumps(data, indent=2)[:600])


@app.command
def clients(
    limit: Annotated[int, Parameter(name=["-l", "--limit"])] = 20,
    base: Annotated[str, Parameter(name=["-b", "--base"])] = DEFAULT_BASE,
) -> None:
    """Elenca i client visti dal deployment (GET /api/v5/clients)."""
    client = CloudClient(base=base)
    print_clients(client.get_json("clients", params={"_limit": limit}))


@app.command
def client(
    clientid: str,
    base: Annotated[str, Parameter(name=["-b", "--base"])] = DEFAULT_BASE,
) -> None:
    """Dettaglio di un client (GET /api/v5/clients/{clientid})."""
    client = CloudClient(base=base)
    print(json.dumps(client.get_json(f"clients/{clientid}"), indent=2)[:1200])


@app.command
def publish(
    topic: str,
    payload: Annotated[str, Parameter(name=["-m", "--message"])] = "hello",
    qos: Annotated[int, Parameter(name=["-q", "--qos"])] = 0,
    retain: Annotated[bool, Parameter(name=["-r", "--retain"])] = False,
    base: Annotated[str, Parameter(name=["-b", "--base"])] = DEFAULT_BASE,
) -> None:
    """Pubblica un messaggio MQTT via REST (scrittura esplicita)."""
    client = CloudClient(base=base)
    data = client.post_json(
        "publish",
        {
            "topic": topic,
            "payload": payload,
            "qos": qos,
            "retain": retain,
        },
    )
    print(f"[+] Publish accettata: {data}")


@app.command
def get(
    path: str,
    base: Annotated[str, Parameter(name=["-b", "--base"])] = DEFAULT_BASE,
) -> None:
    """GET generico su un percorso dell'API v5 (es. nodes, subscriptions)."""
    client = CloudClient(base=base)
    print(json.dumps(client.get_json(path), indent=2)[:2000])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    try:
        app()
    except RuntimeError as exc:
        print(f"[X] {exc}")
        sys.exit(2)
    except requests.exceptions.RequestException as exc:
        causa = exc.__cause__ or exc
        print(f"[X] Errore di rete verso l'API EMQX: {type(causa).__name__}: {causa}")
        print("    Verificare DNS/connettivita' verso l'host del deployment.")
        sys.exit(3)
