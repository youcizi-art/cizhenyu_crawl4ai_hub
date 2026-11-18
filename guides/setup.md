# Crawl4AI 配置

1. 在工具箱安装 **Crawl4AI**（会创建 `.venv` 并安装 `requirements.txt`）。
2. 安装后启动 Hub 服务（工具箱服务开关，或本目录 `python manage.py start`）。
3. 启用工具后，Agent 通过 `mcp_server.py` 访问本地 Hub（共享浏览器，不重复起 Chromium）。
4. 系统未安装 Chrome/Edge 时，首次可能需下载共享 Chromium（约数百 MB）。
5. 更新工具源码后，在工具箱对 Crawl4AI 执行「更新/重装」。
