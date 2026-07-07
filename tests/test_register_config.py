from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import register


class RegisterConfigTests(unittest.TestCase):
    def test_gateway_v2_config_roundtrip_preserves_auth_and_upstream_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "accounts.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "public_token": "pub",
                        "admin_token": "adm",
                        "upstreams": [
                            {
                                "name": "existing",
                                "base_url": "https://proxy.example/v1",
                                "api_key": "legacy-token-field",
                                "models": ["*"],
                                "enabled": True,
                                "weight": 3,
                                "cooldown_seconds": 45,
                                "timeout_seconds": 90,
                                "max_retries": 0,
                                "extra_headers": {"X-Test": "yes"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            accounts = register.load_accounts(path)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].bearer_token, "legacy-token-field")
            self.assertEqual(accounts[0].base_url, "https://proxy.example/v1")
            self.assertEqual(accounts[0].models, ["*"])
            self.assertEqual(accounts[0].weight, 3)
            self.assertEqual(accounts[0].max_retries, 0)

            accounts.append(
                register.manual_import(
                    name="manual",
                    token="xai-manual-token",
                    base_url="https://api.x.ai",
                    models="grok-4,grok-4-reasoning",
                )
            )
            register.save_accounts(accounts, path)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["public_token"], "pub")
            self.assertEqual(saved["admin_token"], "adm")
            self.assertNotIn("accounts", saved)
            self.assertEqual(len(saved["upstreams"]), 2)
            self.assertEqual(saved["upstreams"][0]["bearer_token"], "legacy-token-field")
            self.assertEqual(saved["upstreams"][0]["extra_headers"], {"X-Test": "yes"})
            self.assertEqual(saved["upstreams"][1]["name"], "manual")
            self.assertEqual(saved["upstreams"][1]["models"], ["grok-4", "grok-4-reasoning"])

    def test_grok_url_import_keeps_base_url_and_models(self) -> None:
        accounts = register.import_from_subscription(
            "grok://xai-token@api.x.ai?name=alice&models=grok-4,grok-4-reasoning"
        )

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].name, "alice")
        self.assertEqual(accounts[0].base_url, "https://api.x.ai")
        self.assertEqual(accounts[0].bearer_token, "xai-token")
        self.assertEqual(accounts[0].models, ["grok-4", "grok-4-reasoning"])

    def test_provision_xai_api_key_calls_management_api(self) -> None:
        class FakeAsyncClient:
            calls: list[dict] = []

            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def post(self, url, headers=None, json=None):
                self.calls.append({"url": url, "headers": headers, "json": json})
                return httpx.Response(
                    201,
                    json={"apiKey": "sample-created-token", "apiKeyId": "key_123"},
                )

        with patch("httpx.AsyncClient", FakeAsyncClient):
            data = asyncio.run(
                register.provision_xai_api_key(
                    management_key="sample-management-token",
                    team_id="team_abc",
                    key_name="alice",
                    acls=["api-key:endpoint:*"],
                    qpm=60,
                    management_base_url="https://management-api.x.ai",
                )
            )

        self.assertEqual(data["apiKey"], "sample-created-token")
        self.assertEqual(len(FakeAsyncClient.calls), 1)
        call = FakeAsyncClient.calls[0]
        self.assertEqual(call["url"], "https://management-api.x.ai/auth/teams/team_abc/api-keys")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sample-management-token")
        self.assertEqual(call["json"]["name"], "alice")
        self.assertEqual(call["json"]["acls"], ["api-key:endpoint:*"])
        self.assertEqual(call["json"]["qpm"], 60)

    def test_redact_secret(self) -> None:
        self.assertEqual(register.redact_secret("sample-created-token"), "sample...-token")
        self.assertEqual(register.redact_secret("short"), "*****")


if __name__ == "__main__":
    unittest.main()
