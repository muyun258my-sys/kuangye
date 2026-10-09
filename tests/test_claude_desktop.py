"""Claude Desktop 接入：安装脚本合并配置不破坏原有内容；按配置原样（cwd 不在项目目录）启动后 7 个工具都能调用。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("claude_desktop", ROOT / "scripts" / "claude_desktop.py")
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


def test_install_merges_and_backs_up(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "venv_python", lambda: Path(sys.executable))
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    existing = {"mcpServers": {"filesystem": {"command": "npx", "args": ["x"]}}, "globalShortcut": "Ctrl+Space"}
    cfg.write_text(json.dumps(existing), encoding="utf-8-sig")  # 记事本风格 BOM
    monkeypatch.setattr(sys, "argv", ["x", "install", "--config", str(cfg)])
    cd.main()
    raw = cfg.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # 写回时不带 BOM
    new = json.loads(raw)
    assert new["globalShortcut"] == "Ctrl+Space" and "filesystem" in new["mcpServers"]
    assert set(new["mcpServers"]) == {"filesystem", "mining-news", "mineral-pdf", "lme-price"}
    s = new["mcpServers"]["lme-price"]
    assert s["command"] == sys.executable and s["args"] == ["-m", "servers.lme_price.server"]
    assert s["env"]["PYTHONPATH"] == str(ROOT)
    assert len(list(cfg.parent.glob("*.bak-*"))) == 1
    # 卸载只移除自己的 3 个
    monkeypatch.setattr(sys, "argv", ["x", "uninstall", "--config", str(cfg)])
    cd.main()
    assert set(json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]) == {"filesystem"}


def _fake_windows(monkeypatch, tmp_path, msix: bool, classic: bool):
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    if msix:
        (local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    (local / "Packages" / "Microsoft.Other_123").mkdir(parents=True)
    if classic:
        (roaming / "Claude").mkdir(parents=True)
    monkeypatch.setattr(cd.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    return local, roaming


def test_windows_store_version_config_location(tmp_path, monkeypatch):
    """微软商店版：配置必须写到 Packages\\Claude_xxx\\LocalCache\\Roaming\\Claude（即使配置文件还不存在）。"""
    local, roaming = _fake_windows(monkeypatch, tmp_path, msix=True, classic=False)
    msix_cfg = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    assert cd.install_targets(None)[0] == msix_cfg
    assert all("Microsoft.Other" not in str(p) for p in cd.install_targets(None))
    assert cd.find_config(None) == msix_cfg


def test_windows_classic_version_config_location(tmp_path, monkeypatch):
    local, roaming = _fake_windows(monkeypatch, tmp_path, msix=False, classic=True)
    assert cd.install_targets(None) == [roaming / "Claude" / "claude_desktop_config.json"]


async def test_check_launches_like_claude_desktop(monkeypatch):
    monkeypatch.setattr(cd, "venv_python", lambda: Path(sys.executable))
    entries = cd.server_entries(use_uv=False)
    assert await cd.check(entries, offline=True, timeout=60) is True


def test_repo_mcp_config_is_valid():
    cfg = json.loads((ROOT / "mcp-config.json").read_text(encoding="utf-8"))
    assert set(cfg["mcpServers"]) == {"mining-news", "mineral-pdf", "lme-price"}
    for name, s in cfg["mcpServers"].items():
        assert s["command"] and s["args"][-1] == cd.SERVERS[name]
