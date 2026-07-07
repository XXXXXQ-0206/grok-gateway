# Grok Gateway

[English README](README.md)

Grok Gateway 是一个轻量的 OpenAI 兼容 API 网关，用于将请求路由到一个或多个你已授权使用的 xAI/Grok API key。项目还包含一个辅助工具，可通过官方 xAI Management API 创建 API key，或导入你已经拥有并被授权使用的凭据。

本公开版本刻意限定在合法的 API key 使用场景内。它不包含浏览器自动化、验证码处理、第三方账号注册、账号批量获取，也不包含网页登录 SSO token 提取。

## 功能

- OpenAI 兼容的 `/v1/chat/completions` 代理，支持流式输出。
- `/v1/models` 和 `/health` 接口。
- 支持带权重的上游轮询、冷却和失败切换。
- 管理接口支持查看、添加、删除、导入和重新加载上游。
- 支持从 `grok://` URL、纯文本、JSON、Base64 JSON 导入凭据。
- 可通过 `register.py provision` 调用官方 xAI Management API 创建 API key。
- CLI 输出会尽量隐藏完整密钥。
- 已配置 GitHub CodeQL、Dependabot 和 CI workflow。

## 环境要求

- Python 3.10 或更高版本。
- 一个 xAI/Grok API key，或用于创建 API key 的 xAI Management API key 与 team id。
- 如需托管 CI、CodeQL 和 Dependabot 检查，需要启用 GitHub Actions。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 快速开始

从示例创建本地配置：

```bash
cp accounts.example.json accounts.json
```

导入你已经拥有的 API key：

```bash
python register.py manual --name primary --token "xai-your-api-key" --append
```

启动网关：

```bash
python gateway.py --config accounts.json --host 127.0.0.1 --port 8000
```

查看模型：

```bash
curl http://127.0.0.1:8000/v1/models
```

发送聊天请求：

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

## 创建 API Key

如果你有 xAI Management API key 和 team id，可以通过官方接口创建 API key：

```bash
export XAI_MANAGEMENT_API_KEY="your-management-key"
export XAI_TEAM_ID="your-team-id"

python register.py provision --name primary --append
```

可选限额：

```bash
python register.py provision --name primary --qpm 60 --tpm 100000 --append
```

## 导入已有凭据

从 `grok://` URL 导入：

```bash
python register.py import \
  --grok-url "grok://xai-your-api-key@api.x.ai?name=primary&models=grok-4,grok-4-reasoning" \
  --append
```

从文本或 JSON 文件导入：

```bash
python register.py import --file upstreams.txt --append
```

查看已保存上游，且不打印完整密钥：

```bash
python register.py list
```

## 配置

网关默认读取 `accounts.json`。请将该文件视为私密文件，不要提交到仓库。

```json
{
  "version": 2,
  "public_token": "",
  "admin_token": "",
  "upstreams": [
    {
      "name": "primary",
      "base_url": "https://api.x.ai",
      "bearer_token": "xai-your-api-key-here",
      "enabled": true,
      "models": ["grok-4", "grok-4-reasoning"],
      "weight": 1,
      "cooldown_seconds": 30,
      "timeout_seconds": 120,
      "max_retries": 1,
      "extra_headers": {}
    }
  ]
}
```

设置 `public_token` 后，公开 API 请求需要携带 `Authorization: Bearer <public_token>`。设置 `admin_token` 后，管理接口需要携带 `X-Admin-Token`。

## 管理接口

```bash
curl http://127.0.0.1:8000/admin/accounts \
  -H "X-Admin-Token: your-admin-token"
```

```bash
curl -X POST http://127.0.0.1:8000/admin/accounts \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: your-admin-token" \
  -d '{
    "name": "secondary",
    "bearer_token": "xai-secondary-api-key",
    "models": ["grok-4"],
    "replace": false
  }'
```

## 安全与隐私

- `.gitignore` 已忽略 `accounts.json`、`gateway.json`、`.env`、tokens、证书、调试截图、缓存、下载的账号导出、浏览历史和收藏信息。
- CLI 输出会尽量隐藏已保存 API key 的完整值。
- 公开仓库排除了本地历史自动化实验和嵌套 Git 仓库。
- 发布变更前请运行测试和密钥扫描。
- 如果任何凭据曾经出现在本地文件、终端日志、截图、浏览器配置或历史 Git 提交中，请立即轮换。

## GitHub 安全功能

本仓库包含：

- `.github/workflows/codeql.yml` 中的 CodeQL 分析。
- `.github/dependabot.yml` 中的 Python 依赖和 GitHub Actions 自动更新配置。
- `.github/workflows/ci.yml` 中的 CI 测试。
- `SECURITY.md` 中的漏洞报告和密钥处理说明。

对于公开 GitHub 仓库，可以在仓库安全设置中启用 Secret Scanning 和 Push Protection。发布后请在仓库设置里开启这些功能。

## 免责声明

使用本项目的风险由你自行承担。你需要自行确保遵守适用法律、平台条款、xAI/Grok API 条款、速率限制以及内部安全策略。

维护者不提供法律、合规、安全或财务建议。若配置或使用不当，本软件可能导致 API 费用、服务中断、凭据泄露、账号或 API 访问受限等后果。

本项目按“原样”提供，不附带任何形式的明示或默示担保。在法律允许的最大范围内，作者和贡献者不对因使用、误用、无法使用、凭据暴露、数据丢失、服务封禁或与本软件相关的第三方索赔而产生的任何直接、间接、偶然、特殊、后果性或惩罚性损害承担责任。

## 开发

```bash
python -m pytest
```

## 许可证

MIT。详见 [LICENSE](LICENSE)。
