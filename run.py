"""Start the local-only app, or reopen this app if it is already running."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
PORT = int(os.environ.get("INVOICE_AGENT_PORT", "8847"))
URL = f"http://127.0.0.1:{PORT}"


def is_ours():
    try:
        with urlopen(URL + "/api/health", timeout=1) as response:
            return json.load(response).get("app") == "personal-invoice-agent"
    except (URLError, TimeoutError, ValueError):
        return False


def open_when_ready():
    for _ in range(60):
        if is_ours():
            webbrowser.open(URL)
            return
        time.sleep(0.25)


if __name__ == "__main__":
    with socket.socket() as sock:
        occupied = sock.connect_ex(("127.0.0.1", PORT)) == 0
    if occupied:
        if is_ours():
            webbrowser.open(URL)
            print("票夹已在运行，已重新打开。")
            sys.exit(0)
        sys.exit(f"端口 {PORT} 已被其他程序占用。可设置 INVOICE_AGENT_PORT 后重新启动。")
    if not (ROOT / "web/dist/index.html").exists():
        sys.exit("界面尚未构建，请运行 start.command。")
    threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"票夹已启动：{URL}\n关闭此服务请在终端按 Control+C。数据保留在本机。")
    import uvicorn
    uvicorn.run("invoice_agent.app:create_app", factory=True, host="127.0.0.1", port=PORT, access_log=False)
