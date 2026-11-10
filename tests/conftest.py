"""所有测试默认不联网：外部招聘渠道替换为空（需要的测试自己设置）。"""
import pytest

from scanner import channels


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    monkeypatch.setattr(channels, "CHANNELS", {})


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setenv("SCANNER_BROWSER", "0")
