# 自动化路线暂停说明

本项目已停止维护。以下路线不再维护：

- 自动化注册第三方账号
- 处理或绕过 CAPTCHA / Turnstile / Arkose 等反自动化机制
- 批量网页登录并提取 SSO token
- 使用临时邮箱、代理池或浏览器指纹规避来扩大账号获取规模

历史支持路径：

- `register.py provision`：使用官方 xAI Management API 创建 API key。
- `register.py manual`：导入用户已经拥有并授权使用的 API key/token。
- `register.py import`：批量导入用户已经拥有并授权使用的凭据。
- `gateway.py`：运行 OpenAI 兼容中转网关。

仓库内容仅供历史参考。项目不再提供功能更新、问题修复或依赖更新。
