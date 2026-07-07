# Security Policy

## Supported Scope

This repository supports only the public Grok/xAI API gateway and authorized API key import/provisioning flows.

The public release does not include browser automation, CAPTCHA bypass, third-party account registration, or web-login SSO token extraction.

## Reporting a Vulnerability

Please report suspected vulnerabilities through GitHub private vulnerability reporting when available, or by opening a minimal issue that does not include secrets, tokens, personal data, exploit payloads, or private infrastructure details.

## Secret Handling

Never commit `accounts.json`, `.env` files, API keys, access tokens, browser profiles, downloaded account exports, browsing history, bookmarks, or screenshots containing sensitive information.
