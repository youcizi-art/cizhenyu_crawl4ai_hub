# Crawl4AI 共享服务 — 操作手册（Windows）

按顺序执行，每步都有验证命令。全程约 10 分钟，其中第 5 步下载 Chromium 占大头。

目录内容：

| 文件 | 作用 |
|---|---|
| `app.py` | 服务本体（单例浏览器 + 缓存 + 限流） |
| `mcp_server.py` | MCP 桥（给 agent 调用，零依赖） |
| `manage.py` | 启动 / 停止 / 状态 / 日志 / 自启 |
| `requirements.txt` | 锁定依赖版本 |
| `.env.example` | 配置模板 |
| `install.ps1` | 一键安装（等价于第 3–6 步） |

---

## 第 1 步：装 Python

```powershell
winget install -e --id Python.Python.3.12
```

没装 winget 就从 python.org 下载 3.12+，安装时**勾选 Add python.exe to PATH**。

验证：

```powershell
python --version
```

> 出现 `3.12.x` 或更高即可。已有 Python 3.10+ 可跳过本步。

## 第 2 步：放文件

把本目录整个复制到 `C:\crawl4ai-hub`，然后：

```powershell
cd C:\crawl4ai-hub
dir
```

确认能看到 `app.py`、`mcp_server.py`、`manage.py`。

## 第 3 步：建虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> 若报"禁止运行脚本"，先执行：
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force`

验证（提示符前应出现 `(.venv)`）：

```powershell
python -c "import sys; print(sys.prefix)"
```

## 第 4 步：装依赖

```powershell
python -m pip install -U pip
pip install -r requirements.txt
```

验证：

```powershell
python -c "import crawl4ai, fastapi, uvicorn; print('deps ok')"
```

## 第 5 步：装浏览器

```powershell
python -m playwright install chromium
```

验证（应打印 Chromium 可执行文件路径）：

```powershell
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
```

> 国内网络慢可先设镜像：`$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"`

## 第 6 步：建配置文件

```powershell
copy .env.example .env
notepad .env
```

默认值即可先跑通。对外提供服务时再改：

```ini
C4AI_HUB_TOKEN=随便一串长密码
C4AI_MAX_CONCURRENCY=2
```

## 第 7 步：启动服务

```powershell
python manage.py start
python manage.py status
```

`status` 输出里 `"ok": true` 即成功，同时给出 `uptime_s` 和计数。

## 第 8 步：验证接口

```powershell
# 1) 搜索
Invoke-RestMethod http://127.0.0.1:11235/v1/search -Method Post -ContentType 'application/json' -Body '{"query":"crawl4ai","engine":"ddg","limit":3}' | ConvertTo-Json -Depth 5

# 2) 抓页面
Invoke-RestMethod http://127.0.0.1:11235/v1/crawl -Method Post -ContentType 'application/json' -Body '{"url":"https://example.com","max_chars":800}' | Select-Object title,status,@{n='chars';e={$_.markdown.Length}}
```

两个都返回数据，说明服务可用。

## 第 9 步：接 agent

每个 agent 只需加一段"命令 + 参数"，指向 `mcp_server.py`。**先确保命令里的 python 是虚拟环境里的那个**：`C:\crawl4ai-hub\.venv\Scripts\python.exe`。

**Claude Code**

```powershell
claude mcp add crawl4ai -- "C:\crawl4ai-hub\.venv\Scripts\python.exe" "C:\crawl4ai-hub\mcp_server.py"
```

**Cursor**（编辑 `%USERPROFILE%\.cursor\mcp.json`）

```json
{
  "mcpServers": {
    "crawl4ai": {
      "command": "C:\\crawl4ai-hub\\.venv\\Scripts\\python.exe",
      "args": ["C:\\crawl4ai-hub\\mcp_server.py"]
    }
  }
}
```

**Codex**（编辑 `%USERPROFILE%\.codex\config.toml`）

```toml
[mcp_servers.crawl4ai]
command = "C:\\crawl4ai-hub\\.venv\\Scripts\\python.exe"
args = ["C:\\crawl4ai-hub\\mcp_server.py"]
```

**Cline / VS Code 系**（settings.json）

```json
{
  "mcpServers": {
    "crawl4ai": {
      "command": "C:\\crawl4ai-hub\\.venv\\Scripts\\python.exe",
      "args": ["C:\\crawl4ai-hub\\mcp_server.py"]
    }
  }
}
```

**DSH**（`~/.dsh/profiles/desktop/cordis.patch.yml` 末尾追加）

```yaml
- id: mcp-crawl4ai
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: crawl4ai
    transport: stdio
    command: 'C:/crawl4ai-hub/.venv/Scripts/python.exe'
    args: ['C:/crawl4ai-hub/mcp_server.py']
    cwd: 'C:/crawl4ai-hub'
    failOnStartupError: false
    toolCallTimeoutMs: 180000
```

接入后用一句"帮我搜一下 XXX"验证，agent 应调到 `search_web`。

## 第 10 步：开机自启（可选）

```powershell
python manage.py install
```

登录 Windows 后自动拉起，不弹黑窗。撤销：

```powershell
python manage.py uninstall
```

---

## 验收清单

```powershell
python manage.py status                                      # ok=true
Invoke-RestMethod http://127.0.0.1:11235/v1/stats            # 计数正常
# 同一 URL 抓两次，第二次 cached=true
```

- [ ] `/health` 返回 `ok: true`
- [ ] 搜索返回 ≥3 条结果，链接是真实域名（不是 duckduckgo.com/l/...）
- [ ] 抓页面返回 Markdown，标题正确
- [ ] 第二次抓同一 URL，`cached: true`
- [ ] agent 里能看到 4 个工具：`crawl_url`、`search_web`、`extract_json`、`screenshot_page`

## 日常运维

```powershell
python manage.py status     # 看状态
python manage.py logs -n 80 # 看日志
python manage.py stop       # 停止
python manage.py start      # 启动
```

日志与临时文件在 `%LOCALAPPDATA%\crawl4ai-hub\`。

## 出问题先看这三条

| 现象 | 处理 |
|---|---|
| `manage.py start` 后 status 仍 down | `manage.py logs`，多半是依赖没装全或 Chromium 没装 |
| 调用报 hub 不可达 | 服务没起；先 `manage.py start` |
| 搜索返回空列表 | 换引擎：请求体里 `"engine":"bing"` |

---

## 附：Linux 部署（同样的 3–8 步）

```bash
# 3-6 步
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env

# 7 步：systemd
sudo tee /etc/systemd/system/crawl4ai-hub.service >/dev/null <<'EOF'
[Unit]
Description=Crawl4AI Hub
After=network-online.target

[Service]
WorkingDirectory=/opt/crawl4ai-hub
EnvironmentFile=/opt/crawl4ai-hub/.env
ExecStart=/opt/crawl4ai-hub/.venv/bin/python /opt/crawl4ai-hub/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now crawl4ai-hub
sudo systemctl status crawl4ai-hub
```

第 8–10 步与 Windows 相同（命令换成 `curl`）。
