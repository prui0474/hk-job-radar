"""通道登记表与日报时间点的单元测试：纯逻辑，不联网、不读真实配置。

这组测试钉住三件事：

1. 四个通道的登记表与中文名没写错（写错了配置校验会静默失效）；
2. 日报时间点的取法（配了多份就发多份，没配就退回单个）；
3. **关键词与通道不再被裁剪**——这是一条防回归线：万一以后有人把限制加回来，
   这个测试会立刻红。

同时兼容两种运行方式：
- pytest tests/test_channels.py
- python tests/test_channels.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar import channels
from hk_job_radar.config import AppConfig
from hk_job_radar.scheduler import report_times_of


def test_channel_labels_and_valid_set():
    # 四个通道完全平等：登记表里都有，顺序不代表优先级
    assert set(channels.VALID_CHANNELS) == {"wecom", "wxpusher", "email", "telegram"}
    assert channels.channel_label("wecom") == "企业微信"
    assert channels.channel_label("unknown") == "unknown"      # 未知名字原样返回，便于排障


def test_all_four_labels_exist():
    """每个合法通道都必须有中文名，否则报错信息里会露出英文机器名。"""
    for name in channels.VALID_CHANNELS:
        assert channels.CHANNEL_LABELS.get(name), f"{name} 缺中文名"


def test_report_times_falls_back_to_single_time():
    """没配 report_times 时，用 report_time 那一个时间点。"""
    cfg = AppConfig(report_time="09:00")
    assert report_times_of(cfg) == ["09:00"]


def test_report_times_uses_all_given_points():
    """配了几个时间点就发几份日报。"""
    cfg = AppConfig(report_time="09:00", report_times=["09:00", "18:30"])
    assert report_times_of(cfg) == ["09:00", "18:30"]


def test_report_times_ignores_blank_entries():
    """列表里混进空字符串不能变成「半夜 0 点发」这种奇怪任务。"""
    cfg = AppConfig(report_time="09:00", report_times=["", "  ", "18:30"])
    assert report_times_of(cfg) == ["18:30"]


def test_keywords_are_never_truncated():
    """关键词不设上限：填 20 个就 20 个，一个都不能少。"""
    cfg = AppConfig(keywords=[f"kw{i}" for i in range(20)],
                    graduate_keywords=["management trainee"])
    assert len(cfg.keywords) == 20
    assert cfg.graduate_keywords == ["management trainee"]


def test_channels_are_never_truncated():
    """通道不设上限：写四个就四个，全部都会收到推送。"""
    cfg = AppConfig()
    cfg.notify.channels = ["email", "wecom", "telegram", "wxpusher"]
    assert cfg.notify.channels == ["email", "wecom", "telegram", "wxpusher"]


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
