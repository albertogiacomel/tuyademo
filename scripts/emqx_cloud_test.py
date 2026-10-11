#!/usr/bin/env python3
"""emqx_cloud_test.py - Test unitari del client REST API v5 EMQX Cloud.

Esecuzione: python scripts/emqx_cloud_test.py
Nessuna chiamata di rete: requests e' mockato.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import emqx_cloud


def make_response(status_code: int, payload: Any = None, text: str = "") -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    return resp


class CredentialTest(unittest.TestCase):
    def test_env_fallback(self) -> None:
        with (
            mock.patch.object(emqx_cloud, "read_credential", return_value=None),
            mock.patch.dict(
                os.environ,
                {"EMQX_API_APP_ID": "app-1", "EMQX_API_APP_SECRET": "sec-1"},
            ),
        ):
            self.assertEqual(emqx_cloud.load_api_credentials(), ("app-1", "sec-1"))

    def test_credential_manager_first(self) -> None:
        with (
            mock.patch.object(
                emqx_cloud, "read_credential", return_value=("app-vault", "sec-vault")
            ),
            mock.patch.dict(
                os.environ,
                {"EMQX_API_APP_ID": "app-1", "EMQX_API_APP_SECRET": "sec-1"},
            ),
        ):
            self.assertEqual(
                emqx_cloud.load_api_credentials(), ("app-vault", "sec-vault")
            )

    def test_none_when_absent(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("EMQX_API_APP_ID", "EMQX_API_APP_SECRET")
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(emqx_cloud, "read_credential", return_value=None),
        ):
            self.assertIsNone(emqx_cloud.load_api_credentials())


class ClientTest(unittest.TestCase):
    def test_url_join(self) -> None:
        client = emqx_cloud.CloudClient(base="https://host:8443/api/v5/")
        self.assertEqual(client._url("status"), "https://host:8443/api/v5/status")
        self.assertEqual(client._url("/clients"), "https://host:8443/api/v5/clients")

    def test_get_json_basic_auth(self) -> None:
        client = emqx_cloud.CloudClient(base="https://host:8443/api/v5")
        with (
            mock.patch.object(
                emqx_cloud, "load_api_credentials", return_value=("app-1", "sec-1")
            ),
            mock.patch.object(emqx_cloud.requests, "request") as req,
        ):
            req.return_value = make_response(200, {"ok": True})
            data = client.get_json("status")
        self.assertEqual(data, {"ok": True})
        _, kwargs = req.call_args
        self.assertEqual(kwargs["auth"].username, "app-1")
        self.assertEqual(kwargs["auth"].password, "sec-1")
        self.assertTrue(kwargs["auth"].password)

    def test_error_mapping_401_and_404(self) -> None:
        client = emqx_cloud.CloudClient()
        with (
            mock.patch.object(
                emqx_cloud, "load_api_credentials", return_value=("app-1", "sec-1")
            ),
            mock.patch.object(emqx_cloud.requests, "request") as req,
        ):
            req.return_value = make_response(401, text="unauthorized")
            with self.assertRaises(RuntimeError) as ctx:
                client.get_json("status")
            self.assertIn("401", str(ctx.exception))
            req.return_value = make_response(404, text="not found")
            with self.assertRaises(RuntimeError) as ctx:
                client.get_json("clients")
            self.assertIn("404", str(ctx.exception))

    def test_post_publish_payload(self) -> None:
        client = emqx_cloud.CloudClient()
        with (
            mock.patch.object(
                emqx_cloud, "load_api_credentials", return_value=("app-1", "sec-1")
            ),
            mock.patch.object(emqx_cloud.requests, "request") as req,
        ):
            req.return_value = make_response(200, {"id": "abc"})
            data = client.post_json("publish", {"topic": "t/a", "payload": "hi"})
        self.assertEqual(data, {"id": "abc"})
        args, kwargs = req.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["json"], {"topic": "t/a", "payload": "hi"})


class PrintTest(unittest.TestCase):
    def test_clients_table(self) -> None:
        data = {
            "data": [
                {
                    "clientid": "tyde3-server-1",
                    "username": "tuya",
                    "ip_address": "192.168.1.125",
                    "connected": True,
                    "connected_at": "2026-10-10T10:00:00",
                }
            ],
            "meta": {"count": 1, "page": 1},
        }
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            emqx_cloud.print_clients(data)
        out = buf.getvalue()
        self.assertIn("tyde3-server-1", out)
        self.assertIn("192.168.1.125", out)
        self.assertIn("Totale: 1", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
