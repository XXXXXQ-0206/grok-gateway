#!/usr/bin/env python3
"""
Grok API key registration and import helper.

The public version intentionally supports only these maintained paths:
  - create an xAI API key through the official xAI Management API
  - import an API key/token the operator already owns and is authorized to use
  - import existing credentials from text, JSON, Base64 JSON, or grok:// URLs
  - list saved upstreams with redacted secrets

Browser automation, CAPTCHA handling, third-party account registration, and
web-login SSO token extraction are not implemented in this public release.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx


GROK_API_BASE = "https://api.x.ai"
XAI_MANAGEMENT_BASE = "https://management-api.x.ai"
DEFAULT_API_KEY_ACLS = ["api-key:endpoint:*", "api-key:model:*"]
DEFAULT_ACCOUNTS_FILE = Path("accounts.json")
PAUSED_BROWSER_MODES = {"semi-auto", "full-auto", "batch", "refresh"}


@dataclass
class GrokAccount:
    """A gateway upstream credential record."""

    name: str
    base_url: str = GROK_API_BASE
    bearer_token: str = ""
    sso_token: str = ""
    sso_rw_token: str = ""
    refresh_token: str = ""
    x_username: str = ""
    obtained_at: str = ""
    token_expires_at: float = 0.0
    enabled: bool = True
    models: list[str] = field(default_factory=list)
    weight: int = 1
    cooldown_seconds: int = 30
    timeout_seconds: int = 120
    max_retries: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GrokAccount":
        token = (
            str(data.get("bearer_token", "") or "")
            or str(data.get("api_key", "") or "")
            or str(data.get("token", "") or "")
        ).strip()

        raw_headers = data.get("extra_headers", {})
        extra_headers = {
            str(key): str(value)
            for key, value in raw_headers.items()
        } if isinstance(raw_headers, dict) else {}

        return cls(
            name=str(data.get("name", "") or data.get("id", "") or f"grok-{uuid.uuid4().hex[:6]}"),
            base_url=str(data.get("base_url", "") or GROK_API_BASE).strip(),
            bearer_token=token,
            sso_token=str(data.get("sso_token", "") or "").strip(),
            sso_rw_token=str(data.get("sso_rw_token", data.get("sso-rw", "")) or "").strip(),
            refresh_token=str(data.get("refresh_token", "") or "").strip(),
            x_username=str(data.get("x_username", "") or data.get("username", "") or "").strip(),
            obtained_at=str(data.get("obtained_at", "") or now_iso()),
            token_expires_at=float(data.get("token_expires_at", 0) or 0),
            enabled=bool(data.get("enabled", True)),
            models=parse_models(data.get("models", [])),
            weight=_int_field(data, "weight", 1, 1),
            cooldown_seconds=_int_field(data, "cooldown_seconds", 30, 0),
            timeout_seconds=_int_field(data, "timeout_seconds", 120, 1),
            max_retries=_int_field(data, "max_retries", 1, 0),
            extra_headers=extra_headers,
        )

    def to_upstream_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url or GROK_API_BASE,
            "bearer_token": self.bearer_token,
            "sso_token": self.sso_token,
            "enabled": self.enabled,
            "models": self.models,
            "weight": self.weight,
            "cooldown_seconds": self.cooldown_seconds,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "extra_headers": self.extra_headers,
        }


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_models(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _int_field(data: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if value is None or value == "":
        value = default
    return max(minimum, int(value))


def load_accounts(path: Path) -> list[GrokAccount]:
    """Load either gateway v2 'upstreams' or legacy 'accounts' records."""
    if not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        items = data.get("upstreams")
        if items is None:
            items = data.get("accounts", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    return [
        GrokAccount.from_dict(item)
        for item in items
        if isinstance(item, dict)
    ]


def save_accounts(accounts: list[GrokAccount], path: Path) -> None:
    """Save gateway v2 config while preserving existing public/admin auth."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    data = {
        "version": 2,
        "public_token": str(existing.get("public_token", "") or ""),
        "admin_token": str(existing.get("admin_token", "") or ""),
        "upstreams": [account.to_upstream_dict() for account in accounts],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def parse_grok_url(url: str) -> dict[str, str]:
    """
    Parse grok://<token>@api.x.ai?name=alice&models=grok-4 URLs.

    The URL format is only a transport format for credentials the user already
    owns. It does not fetch or generate credentials.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme != "grok":
        raise ValueError("expected grok:// URL")

    query = parse_qs(parsed.query)
    token = unquote(parsed.username or parsed.password or "")
    hostname = parsed.hostname or "api.x.ai"
    port = f":{parsed.port}" if parsed.port else ""
    base_url = f"https://{hostname}{port}".rstrip("/")

    return {
        "token": token,
        "name": (query.get("name", [""])[0] or f"grok-{uuid.uuid4().hex[:6]}").strip(),
        "models": query.get("models", [""])[0].strip(),
        "base_url": query.get("base_url", [base_url])[0].strip() or base_url,
    }


def _maybe_decode_base64_text(text: str) -> str:
    compact = "".join(text.split())
    if not compact or len(compact) % 4 != 0:
        return text
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8")
    except Exception:
        return text
    if any(marker in decoded for marker in ("grok://", "{", "[", "\n")):
        return decoded
    return text


def _accounts_from_json_text(text: str) -> list[GrokAccount]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    if isinstance(data, dict):
        if isinstance(data.get("upstreams"), list):
            items = data["upstreams"]
        elif isinstance(data.get("accounts"), list):
            items = data["accounts"]
        else:
            items = [data]
    elif isinstance(data, list):
        items = data
    else:
        items = []

    return [
        GrokAccount.from_dict(item)
        for item in items
        if isinstance(item, dict) and (
            item.get("bearer_token") or item.get("api_key") or item.get("token")
        )
    ]


def manual_import(
    name: str = "",
    token: str = "",
    base_url: str = GROK_API_BASE,
    models: str | list[str] = "",
    enabled: bool = True,
) -> GrokAccount:
    return GrokAccount(
        name=(name or f"grok-{uuid.uuid4().hex[:6]}").strip(),
        base_url=(base_url or GROK_API_BASE).strip().rstrip("/"),
        bearer_token=token.strip(),
        enabled=enabled,
        models=parse_models(models),
        obtained_at=now_iso(),
    )


def import_from_subscription(sub_text: str) -> list[GrokAccount]:
    """Import existing credentials from grok://, JSON, Base64 JSON, or text."""
    text = _maybe_decode_base64_text(sub_text.strip())
    if not text:
        return []

    json_accounts = _accounts_from_json_text(text)
    if json_accounts:
        return json_accounts

    accounts: list[GrokAccount] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("grok://"):
            info = parse_grok_url(line)
            if info["token"]:
                accounts.append(
                    manual_import(
                        name=info["name"],
                        token=info["token"],
                        base_url=info["base_url"],
                        models=info["models"],
                    )
                )
            continue

        parts = line.split()
        if len(parts) >= 2:
            name, token = parts[0], parts[1]
        else:
            name, token = f"grok-{len(accounts) + 1}", parts[0]

        if token:
            accounts.append(manual_import(name=name, token=token))

    return accounts


def redact_secret(value: str, keep: int = 6) -> str:
    """Return a log-safe representation of a secret value."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*****"
    return f"{value[:keep]}...{value[-keep:]}"


def print_paused_mode_message(mode: str) -> None:
    print(f"[PAUSED] Mode '{mode}' is not available in the public release.")
    print("Reason: browser automation, CAPTCHA handling, third-party account registration,")
    print("and web-login SSO token extraction are intentionally excluded.")
    print("Maintained alternatives:")
    print("  python register.py provision --name alice --append")
    print("  python register.py manual --name alice --token \"xai-...\" --append")
    print("  python register.py import --grok-url \"grok://xai-...@api.x.ai?name=alice\" --append")


async def provision_xai_api_key(
    management_key: str,
    team_id: str,
    key_name: str,
    acls: list[str] | None = None,
    qpm: int | None = None,
    tpm: int | None = None,
    management_base_url: str = XAI_MANAGEMENT_BASE,
) -> dict[str, Any]:
    """Create an API key through the official xAI Management API."""
    base = management_base_url.rstrip("/")
    url = f"{base}/auth/teams/{team_id}/api-keys"
    payload: dict[str, Any] = {
        "name": key_name,
        "acls": acls or DEFAULT_API_KEY_ACLS,
    }
    if qpm is not None:
        payload["qpm"] = qpm
    if tpm is not None:
        payload["tpm"] = tpm

    headers = {
        "Authorization": f"Bearer {management_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise RuntimeError(
            f"xAI Management API returned HTTP {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("xAI Management API returned a non-object JSON response")
    return data


async def wait_api_key_propagation(
    management_key: str,
    team_id: str,
    api_key_id: str,
    management_base_url: str = XAI_MANAGEMENT_BASE,
    attempts: int = 12,
    delay_seconds: float = 5.0,
) -> bool:
    """Poll the optional propagation endpoint when an API key id is available."""
    if not api_key_id:
        return False

    base = management_base_url.rstrip("/")
    url = f"{base}/auth/teams/{team_id}/api-keys/{api_key_id}/propagation"
    headers = {"Authorization": f"Bearer {management_key}"}

    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(max(1, attempts)):
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    return False
                if response.status_code < 400:
                    data = response.json()
                    if bool(data.get("propagated", data.get("ready", False))):
                        return True
            except (httpx.HTTPError, json.JSONDecodeError):
                continue
            await asyncio.sleep(delay_seconds)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import or create authorized Grok/xAI API credentials for gateway.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode")

    provision = sub.add_parser("provision", help="Create an xAI API key through the official Management API")
    provision.add_argument("--name", default="", help="API key alias")
    provision.add_argument("--management-key", default=os.getenv("XAI_MANAGEMENT_API_KEY", ""))
    provision.add_argument("--team-id", default=os.getenv("XAI_TEAM_ID", ""))
    provision.add_argument("--management-base-url", default=os.getenv("XAI_MANAGEMENT_BASE_URL", XAI_MANAGEMENT_BASE))
    provision.add_argument("--acl", action="append", default=[], help="ACL entry; can be provided multiple times")
    provision.add_argument("--qpm", type=int, default=None, help="Optional requests-per-minute limit")
    provision.add_argument("--tpm", type=int, default=None, help="Optional tokens-per-minute limit")
    provision.add_argument("--wait", action="store_true", help="Poll propagation if the API returns an API key id")
    provision.add_argument("--append", action="store_true", help="Append instead of overwriting accounts.json")
    provision.add_argument("--output", default=str(DEFAULT_ACCOUNTS_FILE))

    manual = sub.add_parser("manual", help="Import an existing authorized API key/token")
    manual.add_argument("--name", default="", help="Credential alias")
    manual.add_argument("--token", default="", help="API key/token value")
    manual.add_argument("--base-url", default=GROK_API_BASE)
    manual.add_argument("--models", default="grok-4,grok-4-reasoning")
    manual.add_argument("--append", action="store_true")
    manual.add_argument("--output", default=str(DEFAULT_ACCOUNTS_FILE))

    imp = sub.add_parser("import", help="Import existing credentials from text, file, JSON, Base64, or grok://")
    imp.add_argument("--text", default="", help="Raw credential text or Base64 JSON")
    imp.add_argument("--file", default="", help="Path to a text/JSON import file")
    imp.add_argument("--subscription", default="", help="Alias for --file")
    imp.add_argument("--grok-url", default="", help="Single grok:// credential URL")
    imp.add_argument("--append", action="store_true")
    imp.add_argument("--output", default=str(DEFAULT_ACCOUNTS_FILE))

    ls = sub.add_parser("list", help="List saved accounts with redacted secrets")
    ls.add_argument("--file", default=str(DEFAULT_ACCOUNTS_FILE))

    for mode in sorted(PAUSED_BROWSER_MODES):
        paused = sub.add_parser(mode, help="Paused in the public release")
        paused.add_argument("--output", default=str(DEFAULT_ACCOUNTS_FILE))

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        return

    if args.mode in PAUSED_BROWSER_MODES:
        print_paused_mode_message(args.mode)
        return

    if args.mode == "list":
        accounts = load_accounts(Path(args.file))
        if not accounts:
            print("(no saved accounts)")
            return
        for account in accounts:
            status = "enabled" if account.enabled else "disabled"
            print(
                f"[{status}] {account.name:20s} "
                f"base={account.base_url} token={redact_secret(account.bearer_token)}"
            )
        return

    output_path = Path(getattr(args, "output", str(DEFAULT_ACCOUNTS_FILE)))
    accounts = load_accounts(output_path) if getattr(args, "append", False) else []

    if args.mode == "manual":
        token = args.token or input("API key/token: ").strip()
        if not token:
            print("[FAIL] No token provided")
            return
        account = manual_import(
            name=args.name,
            token=token,
            base_url=args.base_url,
            models=args.models,
        )
        accounts.append(account)
        save_accounts(accounts, output_path)
        print(f"[OK] Saved {account.name} with token {redact_secret(account.bearer_token)} to {output_path}")
        return

    if args.mode == "provision":
        if not args.management_key:
            print("[FAIL] Missing --management-key or XAI_MANAGEMENT_API_KEY")
            return
        if not args.team_id:
            print("[FAIL] Missing --team-id or XAI_TEAM_ID")
            return

        key_name = args.name or f"grok-{uuid.uuid4().hex[:6]}"
        try:
            data = await provision_xai_api_key(
                management_key=args.management_key,
                team_id=args.team_id,
                key_name=key_name,
                acls=args.acl or DEFAULT_API_KEY_ACLS,
                qpm=args.qpm,
                tpm=args.tpm,
                management_base_url=args.management_base_url,
            )
        except Exception as exc:
            print(f"[FAIL] API key provisioning failed: {exc}")
            return

        api_key = str(data.get("apiKey", "") or data.get("api_key", "") or "")
        api_key_id = str(data.get("apiKeyId", data.get("id", "")) or "")
        if not api_key:
            print("[FAIL] Management API response did not include an API key")
            return

        if args.wait and api_key_id:
            propagated = await wait_api_key_propagation(
                management_key=args.management_key,
                team_id=args.team_id,
                api_key_id=api_key_id,
                management_base_url=args.management_base_url,
            )
            print(f"[INFO] Propagation status: {'ready' if propagated else 'not confirmed'}")

        account = manual_import(name=key_name, token=api_key, base_url=GROK_API_BASE)
        accounts.append(account)
        save_accounts(accounts, output_path)
        print("[OK] Created API key")
        print(f"[OK] Saved {account.name} to {output_path}; secret value was not printed")
        return

    if args.mode == "import":
        chunks: list[str] = []
        if args.grok_url:
            chunks.append(args.grok_url)
        if args.text:
            chunks.append(args.text)
        file_value = args.file or args.subscription
        if file_value:
            chunks.append(Path(file_value).read_text(encoding="utf-8"))

        imported = import_from_subscription("\n".join(chunks))
        if not imported:
            print("[FAIL] No credentials were imported")
            return

        accounts.extend(imported)
        save_accounts(accounts, output_path)
        print(f"[OK] Imported {len(imported)} account(s) to {output_path}")
        for account in imported:
            print(f"  - {account.name}: {redact_secret(account.bearer_token)}")
        return


if __name__ == "__main__":
    asyncio.run(main())
