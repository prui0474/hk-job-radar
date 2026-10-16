"""免费版 / 高级版分层（tier）单元测试：纯逻辑，不联网、不读真实配置。

为什么这些测试重要：tier.py 是「免费版到底给多少」的唯一定义处。它一旦回归，
表现是"用户填了 20 个关键词却只有 7 个生效"这种**无声故障**——比报错更难发现，
所以用测试钉死。

同时兼容两种运行方式：
- pytest tests/test_tier.py
- python tests/test_tier.py（项目 venv 直接运行）
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar import tier
from hk_job_radar.config import AppConfig


@contextmanager
def _as_free_tier():
    """强制以「免费版」身份运行（不受本机 premium.key / 环境变量影响）。

    为什么不用 pytest 的 monkeypatch：本项目的测试要能在「直接 python 跑」
    的模式下也通过，所以统一手工保存 / 还原，两种模式行为一致。
    """
    old_env = os.environ.pop(tier._TIER_ENV, None)
    old_paths = tier._PREMIUM_KEY_PATHS
    tier._PREMIUM_KEY_PATHS = (Path("__no_such_dir__/premium.key"),)
    try:
        yield
    finally:
        if old_env is not None:
            os.environ[tier._TIER_ENV] = old_env
        tier._PREMIUM_KEY_PATHS = old_paths


@contextmanager
def _as_premium():
    old_env = os.environ.get(tier._TIER_ENV)
    os.environ[tier._TIER_ENV] = "premium"
    try:
        yield
    finally:
        if old_env is None:
            os.environ.pop(tier._TIER_ENV, None)
        else:
            os.environ[tier._TIER_ENV] = old_env


def test_free_limits_are_documented_values():
    assert tier.FREE_MAX_KEYWORDS == 7
    assert tier.FREE_MAX_CHANNELS == 1
    assert tier.FREE_REALTIME_PUSH is False


def test_keywords_truncated_in_order():
    """超限取「前 N 个」：普通关键词优先，其次毕业生项目关键词。"""
    with _as_free_tier():
        cfg = AppConfig(keywords=[f"kw{i}" for i in range(9)],
                        graduate_keywords=["management trainee", "graduate trainee"])
        notes = tier.apply_free_limits(cfg)
    assert cfg.keywords == [f"kw{i}" for i in range(7)]
    assert cfg.graduate_keywords == []
    assert len(notes) == 1 and "7" in notes[0]


def test_keywords_spill_into_graduate_list():
    """普通关键词不足 7 个时，额度顺延给毕业生项目关键词，而不是直接丢弃。"""
    with _as_free_tier():
        cfg = AppConfig(keywords=["a", "b", "c"],
                        graduate_keywords=["management trainee", "graduate trainee"])
        tier.apply_free_limits(cfg)
    assert cfg.keywords == ["a", "b", "c"]
    assert cfg.graduate_keywords == ["management trainee", "graduate trainee"]


def test_channels_keep_only_the_first():
    """免费版只留 channels 列表的第 1 个——用户写在第一位的就是他要用的那个。"""
    with _as_free_tier():
        cfg = AppConfig()
        cfg.notify.channels = ["email", "wecom", "telegram"]
        notes = tier.apply_free_limits(cfg)
    assert cfg.notify.channels == ["email"]
    assert len(notes) == 1 and "邮件" in notes[0]


def test_no_notes_when_within_quota():
    """在额度内不应该有任何提示——不能打扰守规矩的用户。"""
    with _as_free_tier():
        cfg = AppConfig(keywords=["data analyst"], )
        cfg.notify.channels = ["telegram"]
        assert tier.apply_free_limits(cfg) == []


def test_premium_is_not_limited():
    with _as_premium():
        cfg = AppConfig(keywords=[f"kw{i}" for i in range(20)])
        cfg.notify.channels = ["email", "wecom"]
        assert tier.apply_free_limits(cfg) == []
        assert len(cfg.keywords) == 20
        assert cfg.notify.channels == ["email", "wecom"]
        assert tier.realtime_push_enabled() is True


def test_realtime_disabled_for_free():
    with _as_free_tier():
        assert tier.realtime_push_enabled() is False


def test_effective_report_times():
    """免费版只认 report_time；高级版才认 report_times 多个时间点。"""
    cfg = AppConfig(report_time="09:00", report_times=["09:00", "18:30"])
    with _as_free_tier():
        assert tier.effective_report_times(cfg) == ["09:00"]
    with _as_premium():
        assert tier.effective_report_times(cfg) == ["09:00", "18:30"]


def test_channel_labels_and_valid_set():
    # 四个通道完全平等：登记表里都有，顺序不代表优先级
    assert set(tier.VALID_CHANNELS) == {"wecom", "wxpusher", "email", "telegram"}
    assert tier.channel_label("wecom") == "企业微信"
    assert tier.channel_label("unknown") == "unknown"      # 未知名字原样返回，便于排障


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
