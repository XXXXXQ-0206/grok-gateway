# Grok Gateway

[中文 README](README.zh-CN.md)

Grok Gateway is a small OpenAI-compatible API gateway for routing requests to one or more authorized xAI/Grok API keys. It also includes a helper for creating API keys through the official xAI Management API or importing credentials that you already own and are authorized to use.

## Features

- OpenAI-compatible `/v1/chat/completions` proxy with streaming support.
- `/v1/models` and `/health` endpoints.
- Weighted upstream rotation with cooldown and failover behavior.
- Admin endpoints for listing, adding, deleting, importing, and reloading upstreams.
- Credential import from `grok://` URLs, plain text, JSON, and Base64-encoded JSON.
- Official xAI Management API provisioning via `register.py provision`.
- Redacted secret display in CLI output.
- CodeQL, Dependabot, and CI workflow configuration for GitHub.

## Requirements

- Python 3.10 or newer.
- An xAI/Grok API key, or an xAI Management API key and team id for provisioning.
- GitHub Actions enabled if you want hosted CI, CodeQL, and Dependabot checks.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Quick Start

Create a local config from the example:

```bash
cp accounts.example.json accounts.json
```

Import an API key you already own:

```bash
python register.py manual --name primary --token "xai-your-api-key" --append
```

Start the gateway:

```bash
python gateway.py --config accounts.json --host 127.0.0.1 --port 8000
```

List models:

```bash
curl http://127.0.0.1:8000/v1/models
```

Send a chat request:

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

## Provision an API Key

Use the official xAI Management API when you have a management key and team id:

```bash
export XAI_MANAGEMENT_API_KEY="your-management-key"
export XAI_TEAM_ID="your-team-id"

python register.py provision --name primary --append
```

Optional limits:

```bash
python register.py provision --name primary --qpm 60 --tpm 100000 --append
```

## Import Existing Credentials

Import from a `grok://` URL:

```bash
python register.py import \
  --grok-url "grok://xai-your-api-key@api.x.ai?name=primary&models=grok-4,grok-4-reasoning" \
  --append
```

Import from a text or JSON file:

```bash
python register.py import --file upstreams.txt --append
```

List saved upstreams without printing full secrets:

```bash
python register.py list
```

## Configuration

The gateway reads `accounts.json` by default. Keep this file private and never commit it.

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

Set `public_token` to require `Authorization: Bearer <public_token>` for public API requests. Set `admin_token` to require `X-Admin-Token` for admin endpoints.

## Admin API

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

## Security And Privacy

- `accounts.json`, `gateway.json`, `.env` files, tokens, certificates, debug screenshots, caches, downloaded account exports, browser history, and bookmarks are ignored by `.gitignore`.
- CLI output redacts saved API keys where practical.
- The public repository excludes local historical automation experiments and nested Git repositories.
- Run the test suite and secret scans before publishing changes.
- Rotate any credential that may have been stored in a local file, terminal log, screenshot, browser profile, or previous Git history.

## GitHub Security Features

This repository includes:

- CodeQL analysis in `.github/workflows/codeql.yml`.
- Dependabot updates for Python dependencies and GitHub Actions in `.github/dependabot.yml`.
- CI tests in `.github/workflows/ci.yml`.
- A `SECURITY.md` policy for vulnerability reporting and secret handling.

For public GitHub repositories, secret scanning is available through GitHub repository security settings. Enable secret scanning and push protection in the repository settings after publishing.

## Disclaimer

Use this project at your own risk. You are responsible for complying with all applicable laws, platform terms, xAI/Grok API terms, rate limits, and internal security policies.

The maintainers do not provide legal, compliance, security, or financial advice. The software may cause API charges, service interruptions, leaked credentials if misconfigured, or account/API access restrictions if used improperly.

This project is provided "as is" without warranties of any kind. To the maximum extent permitted by law, the authors and contributors are not liable for any direct, indirect, incidental, special, consequential, or punitive damages arising from use, misuse, inability to use, credential exposure, data loss, service bans, or third-party claims related to this software.

## Development

```bash
python -m pytest
```

## License

MIT. See [LICENSE](LICENSE).
