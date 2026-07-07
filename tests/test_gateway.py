from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import gateway


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = Path(self.tmpdir.name) / "accounts.json"
        gateway.state = gateway.GatewayState(self.config_path)

    def write_config(self, data: dict) -> None:
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 导入解析测试 ----

    def test_parse_text_import(self) -> None:
        """纯文本 | 分隔格式导入。"""
        text = "demo|https://api.x.ai|xai-test-token|grok-4|2|15|45"
        items = gateway.parse_import_payload(text, "text")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "demo")
        self.assertEqual(items[0].bearer_token, "xai-test-token")
        self.assertEqual(items[0].models, ["grok-4"])
        self.assertEqual(items[0].weight, 2)

    def test_parse_base64_json_import(self) -> None:
        """Base64 编码 JSON 导入。"""
        payload = base64.b64encode(
            json.dumps({
                "upstreams": [{
                    "name": "alice",
                    "base_url": "https://api.x.ai",
                    "bearer_token": "xai-alice-token",
                    "models": ["grok-4", "grok-4-reasoning"],
                }]
            }, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        parsed = gateway.parse_import_payload(payload, "base64")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "alice")
        self.assertEqual(parsed[0].bearer_token, "xai-alice-token")

    def test_parse_grok_url_import(self) -> None:
        """grok:// URL 格式导入。"""
        urls = "grok://xai-token@api.x.ai?name=alice&models=grok-4,grok-4-reasoning"
        parsed = gateway.parse_import_payload(urls, "grok_url")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "alice")
        self.assertEqual(parsed[0].bearer_token, "xai-token")
        self.assertEqual(parsed[0].models, ["grok-4", "grok-4-reasoning"])

    def test_parse_plain_token(self) -> None:
        """纯文本单 token 导入。"""
        text = "my-account sample-token-for-test-only"
        parsed = gateway.parse_import_payload(text, "text")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "my-account")
        self.assertEqual(parsed[0].bearer_token, "sample-token-for-test-only")

    def test_parse_auto_detect_grok_url(self) -> None:
        """自动检测 grok:// URL (不指定 format)。"""
        text = "grok://token123@api.x.ai?name=test"
        parsed = gateway.parse_import_payload(text, "auto")
        self.assertGreaterEqual(len(parsed), 1)
        self.assertEqual(parsed[0].bearer_token, "token123")

    # ---- API 端点测试 ----

    def test_models_endpoint_with_auth(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "pub-token",
            "admin_token": "adm-token",
            "upstreams": [
                {
                    "name": "alice",
                    "base_url": "https://api.x.ai",
                    "bearer_token": "xai-token",
                    "models": ["grok-4"],
                    "enabled": True,
                }
            ],
        })

        with TestClient(gateway.app) as client:
            # 无 token 应返回 401
            self.assertEqual(client.get("/v1/models").status_code, 401)

            # 正确 token
            resp = client.get("/v1/models", headers={"Authorization": "Bearer pub-token"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertTrue(any(m["id"] == "grok-4" for m in data))

    def test_admin_accounts_crud(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "adm-token",
            "upstreams": [],
        })

        with TestClient(gateway.app) as client:
            # 添加账号
            resp = client.post(
                "/admin/accounts",
                headers={"X-Admin-Token": "adm-token"},
                json={
                    "name": "charlie",
                    "base_url": "https://api.x.ai",
                    "bearer_token": "xai-charlie",
                    "models": ["grok-4.1"],
                    "weight": 2,
                },
            )
            self.assertEqual(resp.status_code, 200)

            # 查看账号
            accounts = client.get("/admin/accounts", headers={"X-Admin-Token": "adm-token"})
            self.assertEqual(accounts.status_code, 200)
            self.assertEqual(len(accounts.json()["accounts"]), 1)
            self.assertEqual(accounts.json()["accounts"][0]["name"], "charlie")

            # 删除账号
            del_resp = client.delete("/admin/accounts/charlie", headers={"X-Admin-Token": "adm-token"})
            self.assertEqual(del_resp.status_code, 200)

            # 确认已删除
            accounts = client.get("/admin/accounts", headers={"X-Admin-Token": "adm-token"})
            self.assertEqual(len(accounts.json()["accounts"]), 0)

    def test_reload(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "adm",
            "upstreams": [],
        })

        with TestClient(gateway.app) as client:
            resp = client.post("/admin/reload", headers={"X-Admin-Token": "adm"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["accounts"], 0)

    def test_admin_import_endpoint(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "adm",
            "upstreams": [],
        })

        with TestClient(gateway.app) as client:
            resp = client.post(
                "/admin/import",
                headers={"X-Admin-Token": "adm"},
                json={
                    "payload": "grok://xai-token@api.x.ai?name=imported&models=grok-4",
                    "format": "grok_url",
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["imported"], 1)

            accounts = client.get("/admin/accounts", headers={"X-Admin-Token": "adm"})
            self.assertEqual(accounts.json()["accounts"][0]["name"], "imported")

    def test_health_endpoint(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "",
            "upstreams": [
                {"name": "a", "bearer_token": "t1", "base_url": "https://api.x.ai", "enabled": True},
                {"name": "b", "bearer_token": "t2", "base_url": "https://api.x.ai", "enabled": False},
            ],
        })

        with TestClient(gateway.app) as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["total_accounts"], 2)
            self.assertEqual(data["enabled"], 1)

    # ---- 故障转移测试 ----

    def test_chat_completions_failover(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "",
            "upstreams": [
                {
                    "name": "bad-upstream",
                    "base_url": "https://api.x.ai",
                    "bearer_token": "bad-token",
                    "models": ["grok-4"],
                    "enabled": True,
                },
                {
                    "name": "good-upstream",
                    "base_url": "https://api.x.ai",
                    "bearer_token": "good-token",
                    "models": ["grok-4"],
                    "enabled": True,
                },
            ],
        })

        with TestClient(gateway.app) as client, patch.object(
            gateway,
            "dispatch_to_upstream",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("simulated upstream failure"),
                    gateway.Response(
                        content=b'{"id":"chatcmpl-test","object":"chat.completion","choices":[]}',
                        media_type="application/json",
                        status_code=200,
                    ),
                ]
            ),
        ) as mocked:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-4",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["object"], "chat.completion")
            # 第一次失败, 第二次成功, 所以调用了 2 次
            self.assertEqual(mocked.await_count, 2)

    def test_chat_completions_no_available_upstream(self) -> None:
        self.write_config({
            "version": 2,
            "public_token": "",
            "admin_token": "",
            "upstreams": [],
        })

        with TestClient(gateway.app) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-4",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            self.assertEqual(resp.status_code, 503)

    # ---- GrokUpstream 兼容性测试 ----

    def test_upstream_from_dict_compat(self) -> None:
        """测试字段名兼容 (api_key → bearer_token)。"""
        data = {
            "name": "test",
            "base_url": "https://api.x.ai",
            "api_key": "old-field-name",  # 旧字段名
            "models": ["grok-4"],
        }
        u = gateway.GrokUpstream.from_dict(data)
        self.assertEqual(u.bearer_token, "old-field-name")

        data2 = {
            "name": "test2",
            "token": "another-old-name",  # 另一种旧字段名
        }
        u2 = gateway.GrokUpstream.from_dict(data2)
        self.assertEqual(u2.bearer_token, "another-old-name")

    def test_upstream_model_matching(self) -> None:
        """测试模型匹配逻辑。"""
        u = gateway.GrokUpstream(name="test", bearer_token="t", models=["grok-4", "grok-4-reasoning"])
        self.assertTrue(gateway.state.model_allowed(u, "grok-4"))
        self.assertTrue(gateway.state.model_allowed(u, "grok-4-reasoning"))
        # grok-4 不应匹配 grok-4.1 (精确匹配)
        self.assertFalse(gateway.state.model_allowed(u, "grok-4.1"))

        # 通配符
        u2 = gateway.GrokUpstream(name="test2", bearer_token="t", models=["*"])
        self.assertTrue(gateway.state.model_allowed(u2, "grok-4"))
        self.assertTrue(gateway.state.model_allowed(u2, "grok-anything"))

        # 空列表 = 全部允许
        u3 = gateway.GrokUpstream(name="test3", bearer_token="t", models=[])
        self.assertTrue(gateway.state.model_allowed(u3, "any-model"))

        # 前缀通配: grok-4.* 匹配 grok-4, grok-4.1, grok-4-reasoning
        u4 = gateway.GrokUpstream(name="test4", bearer_token="t", models=["grok-4.*"])
        self.assertTrue(gateway.state.model_allowed(u4, "grok-4"))
        self.assertTrue(gateway.state.model_allowed(u4, "grok-4.1"))
        self.assertTrue(gateway.state.model_allowed(u4, "grok-4-reasoning"))
        self.assertFalse(gateway.state.model_allowed(u4, "grok-3"))


if __name__ == "__main__":
    unittest.main()
