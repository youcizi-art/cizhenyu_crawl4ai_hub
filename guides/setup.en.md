# Crawl4AI setup

1. Install **Crawl4AI** from the toolbox (creates `.venv` and installs `requirements.txt`).
2. Start the Hub service (toolbox service toggle, or `python manage.py start` in the tool dir).
3. After enabling, Agents talk to the local Hub via `mcp_server.py` (shared browser; no extra Chromium per tool).
4. Without system Chrome/Edge, the first run may download a shared Chromium (hundreds of MB).
5. After updating the tool source, use Toolbox → Update / reinstall.
