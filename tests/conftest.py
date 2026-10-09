import os

import pytest

# 默认离线：单元测试不依赖网络；需要测联网分支的用例自己 monkeypatch。
os.environ.setdefault("MINING_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _clear_price_cache():
    """Yahoo 响应有进程内缓存，测试之间要清掉，避免用例互相影响。"""
    from servers.lme_price.providers import YahooProvider

    YahooProvider._cache.clear()
    yield
    YahooProvider._cache.clear()
