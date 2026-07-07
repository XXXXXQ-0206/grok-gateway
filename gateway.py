#!/usr/bin/env python3
"""
Grok 中转站 — OpenAI 兼容 API 网关
====================================

把多个 Grok 账号的 API 封装为统一的 OpenAI 兼容接口。

功能:
  - POST /v1/chat/completions    OpenAI 兼容的聊天接口 (支持 stream)
  - GET  /v1/models               列出可用模型
  - GET  /health                  健康检查
  - GET  /admin/accounts          查看所有账号状态 (需要 admin_token)
  - POST /admin/accounts          添加/替换账号
  - DELETE /admin/accounts/{name} 删除账号
  - POST /admin/import            批量导入账号 (支持多种格式)
  - POST /admin/reload            从磁盘重新加载配置

账号池机制:
  - 加权轮询 (weighted round-robin)
  - 失败自动冷却 (可配置冷却时间)
  - 401/403/429/5xx 自动重试下一个账号
  - 内存状态 + JSON 文件持久化

订阅导入支持 (grok:// 格式):
  grok://<api_key>@api.x.ai?name=alice&models=grok-4,grok-4-reasoning
  以及 Base64 编码的账号列表

启动:
  python gateway.py --config accounts.json --host 0.0.0.0 --port 8000

环境变量:
  GATEWAY_CONFIG=accounts.json
  GATEWAY_HOST=0.0.0.0
  GATEWAY_PORT=8000
  GATEWAY_LOG_LEVEL=info
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse


# ============================================================================
# 常量
# ============================================================================

APP_NAME = "grok-gateway"
VERSION = "2.0.0"

DEFAULT_CONFIG_PATH = Path(os.getenv("GATEWAY_CONFIG", "accounts.json"))
DEFAULT_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("GATEWAY_PORT", "8000"))

# 默认 Grok API 地址
GROK_API_BASE = "https://api.x.ai"
GROK_AUTH_BASE = "https://auth.x.ai"

# 已知 Grok 模型列表 (2025 年可用)
KNOWN_GROK_MODELS = [
    "grok-4",
    "grok-4-reasoning",
    "grok-4.1",
    "grok-4.1-reasoning",
    "grok-3",
    "grok-3-reasoning",
    "grok-2",
    "grok-4.3",
]

# 需要逐跳处理的响应头 (不转发给客户端)
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
}

# 触发重试的 HTTP 状态码
RETRYABLE_STATUS = {401, 403, 408, 425, 429, 500, 502, 503, 504}


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class GrokUpstream:
    """
    一个 Grok 上游账号的完整配置。

    字段说明:
      name            — 账号别名 (唯一标识)
      base_url        — Grok API 的 base URL (默认 https://api.x.ai)
      bearer_token    — API key/token used for Authorization: Bearer
      sso_token       — legacy compatibility field; prefer official API keys
      enabled         — 是否启用
      models          — 该账号支持的模型列表 (空=所有模型)
      weight          — 轮询权重 (越大越容易被选中)
      cooldown_seconds— 失败后的冷却时间
      timeout_seconds — 单次请求的超时时间
      max_retries     — 该账号单次请求的最大重试次数
      extra_headers   — 额外的请求头 (可选)
    """

    name: str
    base_url: str = GROK_API_BASE
    bearer_token: str = ""
    sso_token: str = ""
    enabled: bool = True
    models: list[str] = field(default_factory=list)
    weight: int = 1
    cooldown_seconds: int = 30
    timeout_seconds: int = 120
    max_retries: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "GrokUpstream":
        """从 JSON 字典构造。兼容多种字段名 (api_key / bearer_token / token)。"""
        token = (
            str(data.get("bearer_token", "") or "")
            or str(data.get("api_key", "") or "")
            or str(data.get("token", "") or "")
        )
        return GrokUpstream(
            name=str(data.get("name", "")).strip(),
            base_url=str(data.get("base_url", GROK_API_BASE)).strip().rstrip("/"),
            bearer_token=token.strip(),
            sso_token=str(data.get("sso_token", "") or "").strip(),
            enabled=bool(data.get("enabled", True)),
            models=[str(m).strip() for m in data.get("models", []) if str(m).strip()],
            weight=max(1, int(data.get("weight", 1) or 1)),
            cooldown_seconds=max(1, int(data.get("cooldown_seconds", 30) or 30)),
            timeout_seconds=max(1, int(data.get("timeout_seconds", 120) or 120)),
            max_retries=max(0, int(data.get("max_retries", 1) or 1)),
            extra_headers={
                str(k): str(v)
                for k, v in (data.get("extra_headers", {}) or {}).items()
                if str(k).strip()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
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


@dataclass
class RuntimeState:
    """账号运行时状态 (仅内存, 不持久化)。"""

    fail_count: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_success_at: float = 0.0
    last_used_at: float = 0.0
    success_count: int = 0


@dataclass
class GatewayConfig:
    """全局配置。"""

    version: int = 2
    public_token: str = ""       # 公共接口的鉴权 token (空=不验证)
    admin_token: str = ""        # 管理接口的鉴权 token (空=不验证)
    upstreams: list[GrokUpstream] = field(default_factory=list)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "GatewayConfig":
        # 兼容两种格式: 直接数组 或 {"accounts": [...], "upstreams": [...]}
        raw_list = data.get("upstreams", data.get("accounts", data))
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("upstreams", raw_list.get("accounts", []))
        if not isinstance(raw_list, list):
            raw_list = []

        return GatewayConfig(
            version=int(data.get("version", 2) or 2),
            public_token=str(data.get("public_token", "") or "").strip(),
            admin_token=str(data.get("admin_token", "") or "").strip(),
            upstreams=[GrokUpstream.from_dict(item) for item in raw_list if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "public_token": self.public_token,
            "admin_token": self.admin_token,
            "upstreams": [u.to_dict() for u in self.upstreams],
        }


# ============================================================================
# 核心状态管理
# ============================================================================

class GatewayState:
    """
    全局单例: 持有配置、运行时状态、轮询指针。

    线程安全通过 asyncio.Lock 保证 (单进程 asyncio 下足够)。
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.config = GatewayConfig()
        self.runtime: dict[str, RuntimeState] = {}
        self._rr_index = 0
        self._lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()

    # ---- 持久化 ----

    def load_sync(self) -> None:
        """同步加载配置 (启动时调用)。"""
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self.config = GatewayConfig()
            self.save_sync()
            return
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
        self.config = GatewayConfig.from_dict(data)
        for u in self.config.upstreams:
            self._ensure_runtime(u.name)

    def save_sync(self) -> None:
        """原子写入配置文件。"""
        payload = json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2)
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            Path(tmp_name).replace(path)
        finally:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink()

    async def load(self) -> None:
        async with self._file_lock:
            self.load_sync()

    async def save(self) -> None:
        async with self._file_lock:
            self.save_sync()

    def _ensure_runtime(self, name: str) -> RuntimeState:
        if name not in self.runtime:
            self.runtime[name] = RuntimeState()
        return self.runtime[name]

    # ---- 账号 CRUD ----

    async def add_or_replace(self, upstream: GrokUpstream, replace: bool = False) -> None:
        async with self._lock:
            names = [u.name for u in self.config.upstreams]
            if upstream.name in names:
                if not replace:
                    raise HTTPException(status_code=409, detail=f"账号 '{upstream.name}' 已存在, 使用 replace=true 覆盖")
                idx = names.index(upstream.name)
                self.config.upstreams[idx] = upstream
            else:
                self.config.upstreams.append(upstream)
            self._ensure_runtime(upstream.name)
            await self.save()

    async def delete(self, name: str) -> None:
        async with self._lock:
            before = len(self.config.upstreams)
            self.config.upstreams = [u for u in self.config.upstreams if u.name != name]
            if len(self.config.upstreams) == before:
                raise HTTPException(status_code=404, detail=f"账号 '{name}' 不存在")
            self.runtime.pop(name, None)
            await self.save()

    async def reload(self) -> None:
        async with self._lock:
            await self.load()

    # ---- 模型匹配 ----

    def model_allowed(self, upstream: GrokUpstream, model: Optional[str]) -> bool:
        """检查某个 upstream 是否允许指定模型。"""
        if not model or not upstream.models:
            return True
        for m in upstream.models:
            if m == "*" or m == model:
                return True
            # 精确前缀匹配: "grok-4.*" 模式允许 grok-4 及其子变体
            # 仅当配置以 ".*" 结尾时才做模糊匹配, 避免 grok-4 误匹配 grok-4.1
            if m.endswith(".*"):
                prefix = m[:-2]
                if model.startswith(prefix):
                    return True
        return False

    # ---- 账号选择 (加权轮询) ----

    def _eligible(self, model: Optional[str]) -> list[GrokUpstream]:
        """获取当前可用的上游列表 (未冷却且已启用的)。"""
        now = time.time()
        eligible: list[GrokUpstream] = []
        for u in self.config.upstreams:
            if not u.enabled:
                continue
            if not self.model_allowed(u, model):
                continue
            rt = self._ensure_runtime(u.name)
            if rt.cooldown_until > now:
                continue
            eligible.append(u)
        return eligible

    def choose_upstreams(self, model: Optional[str]) -> list[GrokUpstream]:
        """
        按权重排列候选上游列表。
        返回顺序已去重, 排在越前面越优先尝试。
        """
        eligible = self._eligible(model)
        if not eligible:
            return []

        # 按权重展开
        expanded: list[GrokUpstream] = []
        for u in eligible:
            expanded.extend([u] * max(1, u.weight))

        if not expanded:
            return []

        # 轮询: 从上次位置开始, 保证均匀分布
        start = self._rr_index % len(expanded)
        self._rr_index += 1

        seen: set[str] = set()
        ordered: list[GrokUpstream] = []
        for u in expanded[start:] + expanded[:start]:
            if u.name in seen:
                continue
            seen.add(u.name)
            ordered.append(u)
        return ordered

    # ---- 状态记录 ----

    def mark_success(self, upstream: GrokUpstream) -> None:
        rt = self._ensure_runtime(upstream.name)
        rt.fail_count = 0
        rt.cooldown_until = 0.0
        rt.last_error = ""
        rt.last_success_at = time.time()
        rt.last_used_at = rt.last_success_at
        rt.success_count += 1

    def mark_failure(self, upstream: GrokUpstream, message: str) -> None:
        rt = self._ensure_runtime(upstream.name)
        rt.fail_count += 1
        rt.last_error = message[:500]
        rt.last_used_at = time.time()
        # 指数退避: 连续失败越多, 冷却越长
        cooldown = min(upstream.cooldown_seconds * (2 ** (rt.fail_count - 1)), 600)
        rt.cooldown_until = rt.last_used_at + cooldown

    # ---- 查询 ----

    def runtime_snapshot(self) -> list[dict[str, Any]]:
        """返回所有上游的运行时快照, 用于管理接口。"""
        now = time.time()
        items: list[dict[str, Any]] = []
        for u in self.config.upstreams:
            rt = self._ensure_runtime(u.name)
            items.append({
                "name": u.name,
                "base_url": u.base_url,
                "enabled": u.enabled,
                "models": u.models,
                "weight": u.weight,
                "cooldown_seconds": u.cooldown_seconds,
                "timeout_seconds": u.timeout_seconds,
                "cooldown_remaining": max(0.0, rt.cooldown_until - now),
                "fail_count": rt.fail_count,
                "success_count": rt.success_count,
                "last_error": rt.last_error,
                "last_used_at": rt.last_used_at,
                "last_success_at": rt.last_success_at,
            })
        return items


# ============================================================================
# 全局状态实例
# ============================================================================

state = GatewayState(DEFAULT_CONFIG_PATH)


# ============================================================================
# FastAPI 应用
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载配置, 关闭时无需清理。"""
    await state.load()
    print(f"[OK] Loaded {len(state.config.upstreams)} upstream accounts")
    yield


app = FastAPI(
    title=APP_NAME,
    version=VERSION,
    lifespan=lifespan,
    description="Grok 多账号中转站 — OpenAI 兼容接口",
)


# ============================================================================
# 工具函数
# ============================================================================

def normalize_model(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract_model_from_body(body: bytes) -> Optional[str]:
    """从请求体中提取 model 字段。"""
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_model(payload.get("model"))


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """过滤转发给上游的请求头。"""
    excluded = {
        "host", "authorization", "content-length", "connection",
        "proxy-authorization", "accept-encoding",
    }
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in excluded:
            continue
        result[key] = value
    return result


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """过滤转发回客户端的响应头。"""
    excluded = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in excluded:
            continue
        result[key] = value
    return result


def build_upstream_url(base_url: str, path: str) -> str:
    """拼接上游请求 URL。"""
    return f"{base_url.rstrip('/')}/v1/{path.lstrip('/')}"


# ============================================================================
# 订阅导入解析器
# ============================================================================

def decode_base64_text(payload: str) -> str:
    """Base64 解码, 自动处理 padding。"""
    compact = re.sub(r"\s+", "", payload)
    padding = "=" * ((4 - len(compact) % 4) % 4)
    raw = base64.b64decode(compact + padding)
    return raw.decode("utf-8")


def parse_models_field(raw: str) -> list[str]:
    """解析逗号分隔的模型列表。"""
    return [m.strip() for m in raw.split(",") if m.strip()]


def parse_grok_url(url: str) -> dict[str, str]:
    """
    解析 grok:// 协议的 URL:
      grok://<token>@api.x.ai?name=xxx&models=grok-4,grok-4-reasoning
    """
    parsed = urlparse(url)
    token = parsed.username or parsed.password or ""
    host = parsed.hostname or GROK_API_BASE.replace("https://", "")
    base_url = f"https://{host}"
    if parsed.port:
        base_url += f":{parsed.port}"
    params = parse_qs(parsed.query)
    return {
        "token": token,
        "name": params.get("name", [""])[0],
        "models": params.get("models", ["grok-4,grok-4-reasoning"])[0],
        "base_url": params.get("base_url", [base_url])[0],
    }


def parse_text_lines(text: str) -> list[GrokUpstream]:
    """
    解析纯文本格式的上游列表。

    格式 (用 | 分隔):
      name|base_url|bearer_token|model1,model2|weight|cooldown_seconds|timeout_seconds

    也支持简化的 "name token" 格式 (空格分隔)。
    """
    upstreams: list[GrokUpstream] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "|" in line and line.count("|") >= 2:
            parts = [p.strip() for p in line.split("|")]
            while len(parts) < 7:
                parts.append("")
            name, base_url, token, models_raw, weight_raw, cooldown_raw, timeout_raw = parts[:7]
            upstreams.append(GrokUpstream(
                name=name,
                base_url=base_url or GROK_API_BASE,
                bearer_token=token,
                models=parse_models_field(models_raw) if models_raw else [],
                weight=max(1, int(weight_raw or 1)),
                cooldown_seconds=max(1, int(cooldown_raw or 30)),
                timeout_seconds=max(1, int(timeout_raw or 120)),
            ))
        else:
            # 简化格式: "name token"
            parts = line.split(None, 1)
            if len(parts) == 2:
                upstreams.append(GrokUpstream(
                    name=parts[0],
                    bearer_token=parts[1],
                ))
            elif len(parts) == 1 and len(parts[0]) > 20:
                # 仅 token
                upstreams.append(GrokUpstream(
                    name=f"import-{len(upstreams)+1}",
                    bearer_token=parts[0],
                ))

    return upstreams


def parse_import_payload(payload: str, fmt: str) -> list[GrokUpstream]:
    """
    统一导入入口: 支持多种格式。
      - json:      JSON 数组或 {upstreams:[...]} 结构
      - base64:    Base64 编码的 JSON 或文本
      - text:      纯文本 (| 分隔或空格分隔)
      - grok_url:  grok:// URLs
      - subscription: 订阅链接格式 (Base64 编码的 grok:// 列表)
    """
    fmt = (fmt or "").strip().lower()
    text = payload

    # Base64 解码
    if fmt in {"base64", "b64", "subscription", "sub"}:
        text = decode_base64_text(payload)
        fmt = "text"

    # grok:// URL 格式
    if fmt in {"grok_url", "grok-url", "grokurl"}:
        urls = re.findall(r'grok[s]?://[^\s"\']+', text) or [text]
        upstreams: list[GrokUpstream] = []
        for url in urls:
            info = parse_grok_url(url)
            if info["token"]:
                upstreams.append(GrokUpstream(
                    name=info["name"] or f"import-{len(upstreams)+1}",
                    base_url=info["base_url"] or GROK_API_BASE,
                    bearer_token=info["token"],
                    models=parse_models_field(info["models"]),
                ))
        return upstreams

    # JSON 格式
    if fmt in {"json", "application/json"} or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        # 支持多种 JSON 结构
        if isinstance(data, dict):
            items = data.get("upstreams", data.get("accounts", data.get("data", [])))
        elif isinstance(data, list):
            items = data
        else:
            raise HTTPException(status_code=400, detail="无法解析的 JSON 结构")
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="JSON 内容必须是数组")
        return [GrokUpstream.from_dict(item) for item in items if isinstance(item, dict)]

    # 检测是否包含 grok:// URLs
    if "grok://" in text or "groks://" in text:
        urls = re.findall(r'grok[s]?://[^\s"\']+', text)
        upstreams = []
        for url in urls:
            info = parse_grok_url(url)
            if info["token"]:
                upstreams.append(GrokUpstream(
                    name=info["name"] or f"import-{len(upstreams)+1}",
                    base_url=info["base_url"] or GROK_API_BASE,
                    bearer_token=info["token"],
                    models=parse_models_field(info["models"]),
                ))
        if upstreams:
            return upstreams

    # 默认: 纯文本格式
    return parse_text_lines(text)


# ============================================================================
# 代理转发核心
# ============================================================================

async def dispatch_to_upstream(
    upstream: GrokUpstream,
    request: Request,
    path: str,
    body: bytes,
    stream: bool,
) -> Response:
    """
    将请求转发到指定的上游 Grok API。

    使用 Bearer 认证转发到上游 API:
      Authorization: Bearer <bearer_token>
    """
    url = build_upstream_url(upstream.base_url, path)

    # 准备请求头
    headers = filter_request_headers(dict(request.headers))
    headers.update(upstream.extra_headers)

    # 认证头
    if upstream.bearer_token:
        headers["authorization"] = f"Bearer {upstream.bearer_token}"

    timeout = httpx.Timeout(upstream.timeout_seconds)
    params = dict(request.query_params)
    method = request.method.upper()

    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    req = client.build_request(
        method, url, params=params, content=body or None, headers=headers
    )

    # --- 流式转发 ---
    if stream:
        resp = await client.send(req, stream=True)

        # 流式场景下, 可重试的状态码需要特殊处理
        if resp.status_code in RETRYABLE_STATUS:
            raw = await resp.aread()
            await resp.aclose()
            await client.aclose()
            raise RuntimeError(json.dumps({
                "retryable": True,
                "status_code": resp.status_code,
                "body": raw.decode("utf-8", errors="replace")[:1000],
            }, ensure_ascii=False))

        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            await client.aclose()
            return Response(
                content=raw,
                status_code=resp.status_code,
                headers=filter_response_headers(resp.headers),
            )

        async def stream_iterator():
            try:
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_iterator(),
            status_code=resp.status_code,
            headers=filter_response_headers(resp.headers),
        )

    # --- 非流式转发 ---
    try:
        resp = await client.send(req, stream=True)
        raw = await resp.aread()

        if resp.status_code in RETRYABLE_STATUS:
            raise RuntimeError(json.dumps({
                "retryable": True,
                "status_code": resp.status_code,
                "body": raw.decode("utf-8", errors="replace")[:1000],
            }, ensure_ascii=False))

        return Response(
            content=raw,
            status_code=resp.status_code,
            headers=filter_response_headers(resp.headers),
        )
    finally:
        await client.aclose()


async def proxy_with_retry(request: Request, path: str, stream: bool) -> Response:
    """
    带重试的代理转发: 按轮询顺序尝试候选账号。
    如果账号返回可重试错误 (429/5xx), 标记失败并尝试下一个。
    """
    body = await request.body()
    model = extract_model_from_body(body)

    candidates = state.choose_upstreams(model)
    if not candidates:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "没有可用的上游账号",
                "hint": "所有账号可能都在冷却中、被禁用, 或者没有匹配的模型。"
                       "检查: GET /admin/accounts",
            },
        )

    last_error: str = ""
    last_status = 502
    attempted: list[str] = []

    for upstream in candidates:
        attempted.append(upstream.name)
        try:
            result = await dispatch_to_upstream(upstream, request, path, body, stream=stream)
        except RuntimeError as exc:
            state.mark_failure(upstream, str(exc)[:500])
            last_error = str(exc)
            last_status = 502
            continue
        except httpx.TimeoutException as exc:
            state.mark_failure(upstream, f"timeout: {exc}")
            last_error = f"upstream {upstream.name} 超时 ({upstream.timeout_seconds}s)"
            last_status = 504
            continue
        except httpx.RequestError as exc:
            state.mark_failure(upstream, f"network_error: {exc}")
            last_error = f"网络错误: {exc}"
            last_status = 502
            continue

        if getattr(result, "status_code", 200) >= 400:
            # 非可重试错误 (如 400 Bad Request) — 直接返回, 不重试
            state.mark_failure(upstream, f"upstream_http_{result.status_code}")
            return result

        state.mark_success(upstream)
        return result

    raise HTTPException(
        status_code=last_status,
        detail={
            "message": "所有候选上游都失败了",
            "attempted": attempted,
            "last_error": last_error,
        },
    )


# ============================================================================
# 鉴权中间件
# ============================================================================

async def check_public_auth(request: Request) -> None:
    """检查公共 API 的访问权限。"""
    token = state.config.public_token.strip()
    if not token:
        return  # 未配置 token 时不验证
    auth = request.headers.get("authorization", "").strip()
    if auth != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="public token 不正确, 请设置 Authorization: Bearer <token>")


async def check_admin_auth(request: Request) -> None:
    """检查管理 API 的访问权限。"""
    token = state.config.admin_token.strip()
    if not token:
        return
    provided = request.headers.get("x-admin-token", "").strip()
    if provided != token:
        raise HTTPException(status_code=401, detail="admin token 不正确, 请设置 X-Admin-Token header")


# ============================================================================
# API 路由
# ============================================================================

@app.get("/")
async def root(request: Request) -> dict[str, Any]:
    await check_public_auth(request)
    return {
        "name": APP_NAME,
        "version": VERSION,
        "config_path": str(state.config_path),
        "upstreams": len(state.config.upstreams),
        "enabled": sum(1 for u in state.config.upstreams if u.enabled),
    }


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    await check_public_auth(request)
    now = time.time()
    available = 0
    for u in state.config.upstreams:
        if u.enabled:
            rt = state._ensure_runtime(u.name)
            if rt.cooldown_until <= now:
                available += 1
    return {
        "ok": True,
        "total_accounts": len(state.config.upstreams),
        "enabled": sum(1 for u in state.config.upstreams if u.enabled),
        "available": available,
        "in_cooldown": sum(1 for u in state.config.upstreams if u.enabled and state._ensure_runtime(u.name).cooldown_until > now),
    }


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    """
    列出所有可用模型。

    返回 OpenAI 兼容格式, 同时包含上游账号信息。
    """
    await check_public_auth(request)
    seen: set[str] = set()
    data: list[dict[str, Any]] = []

    for u in state.config.upstreams:
        if not u.enabled:
            continue
        if not u.models:
            # 未指定模型列表, 添加所有已知 Grok 模型
            for model in KNOWN_GROK_MODELS:
                if model not in seen:
                    seen.add(model)
                    data.append({
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": u.name,
                    })
        else:
            for model in u.models:
                model = model.strip()
                if not model or model == "*" or model in seen:
                    continue
                seen.add(model)
                data.append({
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": u.name,
                })

    return {"object": "list", "data": data}


# ---- 管理接口 ----

@app.get("/admin/accounts")
async def admin_list_accounts(request: Request) -> dict[str, Any]:
    await check_admin_auth(request)
    return {
        "config_path": str(state.config_path),
        "total": len(state.config.upstreams),
        "accounts": state.runtime_snapshot(),
    }


@app.post("/admin/accounts")
async def admin_add_account(request: Request) -> dict[str, Any]:
    await check_admin_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc}")
    upstream = GrokUpstream.from_dict(payload)
    replace = bool(payload.get("replace", False))
    await state.add_or_replace(upstream, replace=replace)
    return {"ok": True, "account": upstream.to_dict()}


@app.delete("/admin/accounts/{name}")
async def admin_delete_account(name: str, request: Request) -> dict[str, Any]:
    await check_admin_auth(request)
    await state.delete(name)
    return {"ok": True, "removed": name}


@app.post("/admin/import")
async def admin_import_accounts(request: Request) -> dict[str, Any]:
    """
    批量导入账号。

    请求体:
    {
      "payload": "grok://token@api.x.ai?name=alice&models=grok-4,grok-4-reasoning",
      "format": "grok_url",     // 可选: json | base64 | text | grok_url | subscription
      "replace": false           // 是否覆盖同名账号
    }
    """
    await check_admin_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc}")

    text = str(payload.get("payload", "") or "")
    fmt = str(payload.get("format", "text") or "text")
    replace = bool(payload.get("replace", False))

    if not text:
        raise HTTPException(status_code=400, detail="payload 不能为空")

    imported = parse_import_payload(text, fmt)
    if not imported:
        raise HTTPException(status_code=400, detail="未从 payload 中解析到任何账号")

    added = 0
    for upstream in imported:
        await state.add_or_replace(upstream, replace=replace)
        added += 1

    return {"ok": True, "imported": added, "accounts": [u.name for u in imported]}


@app.post("/admin/reload")
async def admin_reload(request: Request) -> dict[str, Any]:
    """从磁盘重新加载配置文件。"""
    await check_admin_auth(request)
    await state.reload()
    return {"ok": True, "accounts": len(state.config.upstreams)}


# ---- 核心: OpenAI 兼容聊天接口 ----

@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """
    OpenAI 兼容的聊天补全接口。

    请求格式:
    {
      "model": "grok-4",
      "messages": [{"role": "user", "content": "Hello"}],
      "stream": false,
      "temperature": 0.7,
      ...
    }

    支持 stream 和非 stream 两种模式。
    多账号自动轮询, 失败自动切换。
    """
    await check_public_auth(request)
    body = await request.body()
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        payload = {}
    stream = bool(payload.get("stream", False)) if isinstance(payload, dict) else False
    return await proxy_with_retry(request, path="chat/completions", stream=stream)


# ---- 泛用 v1 代理 (透传其他 v1/* 路径) ----

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def v1_proxy(path: str, request: Request) -> Response:
    """透传其他 /v1/* 路径 (如 /v1/images/generations, /v1/embeddings 等)。"""
    await check_public_auth(request)
    if path == "chat/completions":
        body = await request.body()
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}
        stream = bool(payload.get("stream", False)) if isinstance(payload, dict) else False
        return await proxy_with_retry(request, path=path, stream=stream)
    return await proxy_with_retry(request, path=path, stream=False)


# ============================================================================
# 启动入口
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{APP_NAME} v{VERSION} — Grok 多账号 OpenAI 兼容网关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python gateway.py --config accounts.json --port 8000
  python gateway.py --config accounts.json --host 127.0.0.1 --port 8080

测试:
  curl http://127.0.0.1:8000/health
  curl http://127.0.0.1:8000/v1/models
  curl -X POST http://127.0.0.1:8000/v1/chat/completions \\
    -H "Content-Type: application/json" \\
    -d '{"model":"grok-4","messages":[{"role":"user","content":"hi"}]}'

管理接口:
  curl http://127.0.0.1:8000/admin/accounts -H "X-Admin-Token: your-secret"
  curl -X POST http://127.0.0.1:8000/admin/import \\
    -H "Content-Type: application/json" \\
    -H "X-Admin-Token: your-secret" \\
    -d '{"payload":"grok://token@api.x.ai?name=alice","format":"grok_url"}'
        """,
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="账号配置文件路径 (默认 accounts.json)")
    p.add_argument("--host", default=DEFAULT_HOST, help="监听地址 (默认 0.0.0.0)")
    p.add_argument("--port", default=DEFAULT_PORT, type=int, help="监听端口 (默认 8000)")
    p.add_argument("--log-level", default=os.getenv("GATEWAY_LOG_LEVEL", "info"),
                   choices=["critical", "error", "warning", "info", "debug"])
    return p


def main() -> None:
    args = build_parser().parse_args()
    state.config_path = Path(args.config)

    print(f"  {APP_NAME} v{VERSION}")
    print(f"  Config:  {state.config_path}")
    print(f"  Listen:  {args.host}:{args.port}")
    print()

    # 启动前预加载配置 (lifespan 也会加载, 这里提前打印信息)
    state.load_sync()
    enabled_count = sum(1 for u in state.config.upstreams if u.enabled)
    print(f"  Total accounts: {len(state.config.upstreams)}")
    print(f"  Enabled:        {enabled_count}")
    if state.config.public_token:
        print(f"  Public auth:    enabled (Authorization: Bearer <public_token>)")
    if state.config.admin_token:
        print(f"  Admin auth:     enabled (X-Admin-Token)")
    print()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=str(args.log_level),
    )


if __name__ == "__main__":
    main()
