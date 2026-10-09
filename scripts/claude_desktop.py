"""把 3 个 MCP server 注册到 Claude Desktop，并按 Claude Desktop 的方式自检。

  uv run python scripts/claude_desktop.py install     # 写入 Claude Desktop 配置（自动备份原文件）
  uv run python scripts/claude_desktop.py check       # 按配置原样启动 3 个 server，调用全部 7 个工具
  uv run python scripts/claude_desktop.py print       # 只打印要合并的 JSON，不改任何文件
  uv run python scripts/claude_desktop.py uninstall   # 从配置里移除这 3 个 server

可选参数：
  --config PATH   指定配置文件（默认自动查找 Claude Desktop 的 claude_desktop_config.json）
  --uv            用 "uv run --directory" 启动（默认直接用本项目 .venv 里的 python，启动最快、不依赖 PATH）
  --offline       check 时设置 MINING_OFFLINE=1（不联网，结果确定）

为什么默认用 .venv 的 python 绝对路径：Claude Desktop 从开始菜单启动时 PATH 可能不含 uv，
而且 "uv run" 首次启动会同步依赖，可能超过 Claude Desktop 的启动超时。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVERS = {
    "mining-news": "servers.mining_news.server",
    "mineral-pdf": "servers.mineral_pdf.server",
    "lme-price": "servers.lme_price.server",
}
# check 时对每个工具的调用参数（覆盖全部 7 个工具）
SAMPLE_CALLS = {
    "mining-news": [("search", {"query": "Pilbara lithium", "days": 7, "limit": 3}), ("fetch_article", "__FIRST_URL__")],
    "mineral-pdf": [("list_reports", {}), ("extract_resources", {"pdf_url": "fixture://pilgangoora"})],
    "lme-price": [("list_commodities", {}), ("get_price", {"commodity": "lithium", "date": "today"}), ("get_trend", {"commodity": "lithium", "days": 30})],
}


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 配置文件位置
CONFIG_NAME = "claude_desktop_config.json"


def candidate_configs() -> list[Path]:
    """按优先级排列。Windows 上微软商店（MSIX）版 Claude Desktop 的 %APPDATA% 被重定向到
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude，它读的是那里的配置。"""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        msix = [d / CONFIG_NAME for d in sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude")) if d.is_dir()]
        msix += [d / "LocalCache" / "Roaming" / "Claude" / CONFIG_NAME for d in sorted(local.glob("Packages/Claude_*")) if d.is_dir()]
        classic = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude" / CONFIG_NAME
        return list(dict.fromkeys(msix + [classic]))
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Claude" / CONFIG_NAME]
    return [Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "Claude" / CONFIG_NAME]


def install_targets(explicit: str | None) -> list[Path]:
    """install 写入的位置：MSIX 包目录存在就写那里；经典版 Claude 目录存在也写（两种安装方式都覆盖）。"""
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    cands = candidate_configs()
    targets = [p for p in cands if p.exists() or p.parent.exists() or "Packages" in p.parts]
    return targets or [cands[-1]]


def find_config(explicit: str | None) -> Path:
    """check 读取的位置：第一个已存在的配置。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    cands = candidate_configs()
    return next((p for p in cands if p.exists()), install_targets(None)[0])


# ---------------------------------------------------------------- 生成配置
def venv_python() -> Path:
    exe = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    return ROOT / ".venv" / exe


def server_entries(use_uv: bool) -> dict:
    env = {"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    entries = {}
    for name, module in SERVERS.items():
        if use_uv:
            uv = shutil.which("uv")
            if not uv:
                sys.exit("找不到 uv，可去掉 --uv 改用 .venv 里的 python")
            entries[name] = {"command": uv, "args": ["run", "--frozen", "--directory", str(ROOT), "python", "-m", module], "env": env}
        else:
            py = venv_python()
            if not py.exists():
                sys.exit(f"找不到 {py}\n请先在项目目录执行：uv sync")
            entries[name] = {"command": str(py), "args": ["-m", module], "env": env}
    return entries


def load_config(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))  # utf-8-sig：兼容记事本保存的 BOM
    except json.JSONDecodeError as e:
        sys.exit(f"{path} 不是合法 JSON（{e}），请先手动修复，避免覆盖你已有的配置")


def save_config(path: Path, cfg: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # 无 BOM
    os.replace(tmp, path)
    return backup


# ---------------------------------------------------------------- 自检（模拟 Claude Desktop 启动方式）
async def check(cfg_servers: dict, offline: bool, timeout: float) -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    ok_all = True
    neutral_cwd = Path.home()  # Claude Desktop 不会把 cwd 设到项目目录
    for name in SERVERS:
        spec = cfg_servers.get(name)
        if not spec:
            print(f"[FAIL] {name}: 配置里没有这个 server（先运行 install）")
            ok_all = False
            continue
        env = {**os.environ, **spec.get("env", {})}
        if offline:
            env["MINING_OFFLINE"] = "1"
        params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=env, cwd=str(neutral_cwd))
        t0 = time.perf_counter()
        try:
            with open(os.devnull, "w") as errlog:
                async with stdio_client(params, errlog=errlog) as (r, w):
                    async with ClientSession(r, w) as s:
                        await asyncio.wait_for(s.initialize(), timeout)
                        tools = [t.name for t in (await s.list_tools()).tools]
                        print(f"[PASS] {name}: 启动 + 握手 {time.perf_counter() - t0:.1f}s，工具 {tools}")
                        first_url = None
                        for tool, args in SAMPLE_CALLS[name]:
                            if args == "__FIRST_URL__":
                                if not first_url:
                                    print(f"   [SKIP] {tool}: search 没有返回 url")
                                    continue
                                args = {"url": first_url, "max_chars": 300}
                            t1 = time.perf_counter()
                            res = await asyncio.wait_for(s.call_tool(tool, args), timeout)
                            text = res.content[0].text if res.content else ""
                            data = res.structuredContent or (json.loads(text) if text.startswith("{") else {})
                            if tool == "search" and data.get("results"):
                                first_url = data["results"][0]["url"]
                            status = "FAIL" if res.isError else "PASS"
                            ok_all &= not res.isError
                            ds = data.get("data_source") or data.get("source") or ""
                            print(f"   [{status}] {tool}  {time.perf_counter() - t1:.1f}s  {('data_source=' + ds) if ds else ''}  {'' if not res.isError else text[:200]}")
        except Exception as e:  # noqa: BLE001
            ok_all = False
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            print(f"       手动排查：\"{spec['command']}\" {' '.join(spec.get('args', []))}")
    return ok_all


def main() -> None:
    _utf8()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["install", "check", "print", "uninstall"])
    ap.add_argument("--config")
    ap.add_argument("--uv", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()

    if a.action == "print":
        print(json.dumps({"mcpServers": server_entries(a.uv)}, ensure_ascii=False, indent=2))
        return

    if a.action in {"install", "uninstall"}:
        entries = server_entries(a.uv) if a.action == "install" else {}
        for path in install_targets(a.config):
            cfg = load_config(path)
            servers = cfg.setdefault("mcpServers", {})
            if a.action == "install":
                servers.update(entries)
            else:
                for n in SERVERS:
                    servers.pop(n, None)
            backup = save_config(path, cfg)
            print(f"{'已写入' if a.action == 'install' else '已移除 3 个 server'}：{path}" + (f"\n  原配置备份：{backup}" if backup else ""))
        if a.action == "install":
            print("已注册：" + "、".join(SERVERS))
            print("下一步：完全退出 Claude Desktop（托盘图标右键 → 退出，或任务管理器结束 Claude）再重新打开。")
    else:
        path = find_config(a.config)
        cfg = load_config(path)
        print(f"配置文件：{path}\n")
        ok = asyncio.run(check(cfg.get("mcpServers", {}), a.offline, a.timeout))
        print("\nClaude Desktop 自检：" + ("全部通过 ✅" if ok else "存在失败 ❌"))
        print(f"Claude Desktop 的 server 日志：{path.parent / 'logs'}{os.sep}mcp-server-<名字>.log")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
