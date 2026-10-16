"""推送配置解析与通道装配测试：纯逻辑，不发任何网络请求。

覆盖三件最容易悄悄坏掉的事：
1. **向后兼容** —— 老用户的 config.yaml 只有顶层 `telegram:` 段，
   升级后必须仍能收到消息（自动当成"只启用 Telegram"）；
2. **四个通道完全平等** —— 没有默认通道、没有隐式优先级；
3. **门禁** —— 一个通道都没配好时要明确报错，而不是默默什么都不发。

同时兼容两种运行方式：
- pytest tests/test_notify_config.py
- python tests/test_notify_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import (
    AppConfig,
    ConfigError,
    _parse_notify,
    require_notify,
)
from hk_job_radar.notify.base import split_text, split_text_bytes
from hk_job_radar.notify.dispatch import Notifier, build_channel
from hk_job_radar.notify.email_client import EmailChannel
from hk_job_radar.notify.telegram_client import TelegramClient
from hk_job_radar.notify.wecom_client import WecomChannel
from hk_job_radar.notify.wxpusher_client import WxPusherChannel


def test_backward_compatible_legacy_telegram():
    """老配置：只有顶层 telegram 段 → 自动只启用 Telegram（升级不静默失联）。"""
    nc = _parse_notify({"telegram": {"bot_token": "123456:abc-def", "chat_id": "999"}})
    assert nc.channels == ["telegram"]
    assert nc.telegram.bot_token == "123456:abc-def"


def test_legacy_telegram_inside_notify_block():
    """也允许把 telegram 段写进 notify 里（新老写法都能认）。"""
    nc = _parse_notify({"notify": {"telegram": {"bot_token": "1:a", "chat_id": "2"}}})
    assert nc.channels == ["telegram"]


def test_new_notify_section_parsing():
    nc = _parse_notify({"notify": {
        "channels": ["wecom", "email"],
        "wecom": {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"},
        "email": {"smtp_host": "smtp.qq.com", "smtp_port": 465, "user": "a@qq.com",
                  "auth_code": "code", "to": "a@qq.com, b@qq.com"},
        "wxpusher": {"app_token": "AT_x", "uids": ["UID_1", "UID_2"]},
    }})
    assert nc.channels == ["wecom", "email"]          # 顺序即用户的选择顺序
    assert nc.wecom.webhook.endswith("key=k")
    assert nc.email.to == ["a@qq.com", "b@qq.com"]    # 逗号字符串被拆成列表
    assert nc.wxpusher.uids == ["UID_1", "UID_2"]
    assert nc.telegram.bot_token == ""                # 未填的通道保持空，不报错


def test_channel_names_are_lowercased():
    nc = _parse_notify({"notify": {"channels": ["WeCom", " TELEGRAM "]}})
    assert nc.channels == ["wecom", "telegram"]


def test_build_channel_returns_right_types():
    cfg = AppConfig()
    cfg.notify.channels = ["wecom"]
    assert isinstance(build_channel("wecom", cfg), WecomChannel)
    assert isinstance(build_channel("wxpusher", cfg), WxPusherChannel)
    assert isinstance(build_channel("email", cfg), EmailChannel)
    assert isinstance(build_channel("telegram", cfg), TelegramClient)
    assert build_channel("nope", cfg) is None         # 未知名字不抛异常，只记日志


def test_notifier_from_config_keeps_order():
    cfg = AppConfig()
    cfg.notify.channels = ["wxpusher", "telegram"]
    n = Notifier.from_config(cfg)
    assert n.names == ["wxpusher", "telegram"]
    assert n.has_feedback_channel is True             # Telegram 支持点击反馈


def test_notifier_without_telegram_has_no_feedback():
    """邮件 / 企微 / WxPusher 不支持点击按钮 → 调度器不该启动反馈轮询任务。"""
    cfg = AppConfig()
    cfg.notify.channels = ["email"]
    n = Notifier.from_config(cfg)
    assert n.has_feedback_channel is False


def test_check_reports_missing_fields():
    assert WecomChannel("").check()[0] is False
    assert WxPusherChannel("AT_x", []).check()[0] is False
    assert EmailChannel("", 465, "", "", []).check()[0] is False
    assert EmailChannel("smtp.qq.com", 465, "a@qq.com", "code", ["a@qq.com"]).check()[0] is True
    # token 格式不对要能识别出来，而不是等到发送时才失败
    assert TelegramClient("not-a-token", "1").check()[0] is False
    assert TelegramClient("123456:abc-def", "1").check()[0] is True


def test_require_notify_rejects_empty_channels():
    try:
        require_notify(AppConfig())
    except ConfigError as exc:
        assert "推送通道" in str(exc)
    else:
        raise AssertionError("没有任何通道时应当抛出 ConfigError")


def test_require_notify_rejects_unconfigured_channel():
    cfg = AppConfig()
    cfg.notify.channels = ["wecom"]        # 启用了但 webhook 是空的
    try:
        require_notify(cfg)
    except ConfigError as exc:
        assert "企业微信" in str(exc)
    else:
        raise AssertionError("通道未配置时应抛出 ConfigError")


def test_require_notify_passes_when_configured():
    cfg = AppConfig()
    cfg.notify.channels = ["wecom"]
    cfg.notify.wecom.webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k"
    require_notify(cfg)                    # 不抛异常即通过


def test_channel_max_chars_sane():
    """各通道的单条安全长度必须为正，否则分页会退化成"整段一次发"。"""
    for cls in (WecomChannel, WxPusherChannel):
        assert cls.max_chars > 0


# ---------------------------------------------------------------------------
# 分页工具
#
# 这里盯的是一个真实踩过的坑：如果只按换行切分、不处理「一整行就超限」的情况，
# 那一页就会超出通道上限被直接拒收 —— 用户什么都收不到，日志里只有一句
# "发送失败"。所以必须验证：任何一页都不超限，且内容一字不丢。
# ---------------------------------------------------------------------------
def test_split_text_by_chars():
    pages = split_text("abcdefghij\n" * 20, 40)
    assert len(pages) > 1
    assert all(len(p) <= 40 for p in pages)


def test_split_text_handles_overlong_single_line():
    pages = split_text("x" * 100, 30)
    assert all(len(p) <= 30 for p in pages)
    assert "".join(pages) == "x" * 100          # 内容一字不丢


def test_split_text_bytes_respects_byte_limit():
    original = "中文测试" * 400                  # 1600 字 ≈ 4800 字节
    pages = split_text_bytes(original, 3800)
    assert len(pages) > 1
    assert all(len(p.encode("utf-8")) <= 3800 for p in pages)
    assert "".join(pages) == original


def test_split_text_bytes_never_breaks_a_character():
    """按字节硬切时绝不能把汉字切成两半，否则收件端看到的是乱码。"""
    pages = split_text_bytes("中" * 100, 10)     # 10 字节装不下 4 个汉字（12 字节）
    for p in pages:
        assert len(p.encode("utf-8")) <= 10
        p.encode("utf-8").decode("utf-8")        # 能解码 = 没切坏


def test_split_short_text_is_unchanged():
    assert split_text("短", 100) == ["短"]
    assert split_text_bytes("短", 100) == ["短"]


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
