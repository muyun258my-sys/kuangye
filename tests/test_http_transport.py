"""streamable-http 传输（docker-compose 路径）：3 个真实 server 子进程 + agent 通过 HTTP 跑完整简报。

- server 监听 0.0.0.0，agent 用非 localhost 地址访问（等价于容器间用服务名访问，检验 DNS rebinding 保护不会拒绝）
- 环境里故意设置一个不存在的代理，检验 agent → MCP server 不走代理（Windows 系统代理 / Clash 场景）
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.graph import run_briefing
from agent.mcp_clients import SERVERS, connect

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 不会真的发包，只是让系统选出本机网卡地址
            ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"


@pytest.fixture
def http_servers(monkeypatch):
    ip, procs, env_urls = _lan_ip(), [], {}
    for name, (module, _, env_var) in SERVERS.items():
        port = _free_port()
        env = {**os.environ, "MCP_TRANSPORT": "streamable-http", "MCP_HOST": "0.0.0.0", "MCP_PORT": str(port), "MINING_OFFLINE": "1", "PYTHONPATH": str(ROOT)}
        procs.append(subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        env_urls[env_var] = f"http://{ip}:{port}/mcp"
    for k, v in env_urls.items():
        monkeypatch.setenv(k, v)
    # 一个根本不存在的代理：如果 agent 走了代理，连接必然失败
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    yield env_urls
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


async def test_full_briefing_over_http(http_servers):
    async with connect("http", http_retries=10) as hub:
        assert hub.transport == "http"
        tools = await hub.list_tools()
        assert sum(len(v) for v in tools.values()) == 7
        st = await run_briefing("给我生成一份关于 Pilbara 锂矿的今日简报", hub)
    md = st["report_md"]
    assert "## 储量数据" in md and "| Pilgangoora | JORC |" in md and "## 引用源" in md


async def test_unreachable_server_fails_fast_with_clear_message(monkeypatch):
    for _, (_, _, env_var) in SERVERS.items():
        monkeypatch.setenv(env_var, f"http://127.0.0.1:{_free_port()}/mcp")
    t0 = time.perf_counter()
    with pytest.raises(ConnectionError, match="server 启动了吗"):
        async with connect("http", http_retries=2):
            pass
    assert time.perf_counter() - t0 < 10
