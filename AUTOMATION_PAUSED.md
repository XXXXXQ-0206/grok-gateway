# 自动化路线暂停说明

本项目公开版本不包含以下路线：

- 自动化注册第三方账号
- 处理或绕过 CAPTCHA / Turnstile / Arkose 等反自动化机制
- 批量网页登录并提取 SSO token
- 使用临时邮箱、代理池或浏览器指纹规避来扩大账号获取规模

保留的可维护路径：

- `register.py provision`：使用官方 xAI Management API 创建 API key。
- `register.py manual`：导入用户已经拥有并授权使用的 API key/token。
- `register.py import`：批量导入用户已经拥有并授权使用的凭据。
- `gateway.py`：运行 OpenAI 兼容中转网关。

公开仓库只保留上述可维护路径。继续维护时优先保持测试通过、配置格式稳定、依赖可审计，并确保凭据不在日志、文档、截图或提交历史中明文出现。
