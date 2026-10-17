"""配置校验的边界测试：只读命令不该被「必须能跑抓取」的校验挡住。

背景（2026-10-16 回归）：把 keywords 的默认值清空之后，`hk_radar stats`
跟着一起报「keywords 还是空的」——但 stats 只读数据库、顺带打印每日上限，
本不该要求用户先填关键词。这个回归是 CI 的「stats 命令可用」一步逮到的，
所以在这里钉死，避免以后又有人把两类校验混在一起。

同时兼容两种运行方式：
- pytest tests/test_config_validation.py
- python tests/test_config_validation.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig, ConfigError, load_config


def _write_config(text: str) -> Path:
    """把一段 YAML 落到临时目录，返回它的路径（不碰用户真实的 config.yaml）。"""
    directory = Path(tempfile.mkdtemp())
    path = directory / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_empty_keywords_blocks_scraping_commands():
    # run / once / report 这类命令必须拦住——否则会白跑一轮、什么也抓不到
    path = _write_config("career_stage: student\nkeywords: []\n")
    try:
        load_config(path)
    except ConfigError as exc:
        assert "keywords" in str(exc)
    else:
        raise AssertionError("抓取类命令应当因关键词为空而报错")


def test_empty_keywords_allows_readonly_stats():
    # stats 走 require_runnable=False：能打开、能拿到每日上限用于展示
    path = _write_config("career_stage: student\nkeywords: []\n")
    cfg = load_config(path, require_runnable=False)
    assert cfg.keywords == []
    assert cfg.daily_limits


def test_filled_keywords_pass_both_modes():
    path = _write_config('career_stage: student\nkeywords:\n  - "data analyst"\n')
    assert load_config(path).keywords == ["data analyst"]
    assert load_config(path, require_runnable=False).keywords == ["data analyst"]


def test_missing_config_file_returns_defaults_without_raising():
    cfg = load_config(Path("__no_such_config__.yaml"))
    assert isinstance(cfg, AppConfig)
    assert cfg.keywords == []          # 刻意不预置默认词


def test_format_errors_still_raise_in_readonly_mode():
    # 只放宽「能不能开抓」，不放宽「配置写错了」——拼错的取值在只读模式下也要报
    path = _write_config(
        'career_stage: student\nkeywords: []\nkeyword_rotation:\n  per_cycle: "sometimes"\n')
    try:
        load_config(path, require_runnable=False)
    except ConfigError as exc:
        assert "keyword_rotation" in str(exc)
    else:
        raise AssertionError("取值非法的配置在只读模式下也应当报错")


def test_browser_proxy_default_stays_direct():
    """browser.proxy 留空 = 强制直连，与历史行为一致。

    这条钉住「默认不变」：早期实测过，本机存在一个配错的本地代理时，
    Chromium 走它会让 reCAPTCHA 资源加载失败、人机验证永远转圈。
    所以默认直连；需要走代理的人显式填，不能替所有人默认打开。
    """
    from hk_job_radar.scrapers.browser import build_proxy_args

    path = _write_config('career_stage: student\nkeywords:\n  - "data analyst"\n')
    cfg = load_config(path)
    assert cfg.browser_proxy == ""
    assert build_proxy_args(cfg.browser_proxy) == ["--no-proxy-server"]


def test_browser_proxy_system_and_explicit_address():
    from hk_job_radar.scrapers.browser import build_proxy_args

    head = 'career_stage: student\nkeywords:\n  - "data analyst"\n'
    # "system" → 不传代理参数，让 Chromium 自己读系统设置
    cfg = load_config(_write_config(head + 'browser:\n  proxy: "system"\n'))
    assert cfg.browser_proxy == "system"
    assert build_proxy_args(cfg.browser_proxy) == []

    # 显式地址 → 直接传给 Chromium
    cfg = load_config(
        _write_config(head + 'browser:\n  proxy: "http://127.0.0.1:7890"\n'))
    assert cfg.browser_proxy == "http://127.0.0.1:7890"
    assert build_proxy_args(cfg.browser_proxy) == \
        ["--proxy-server=http://127.0.0.1:7890"]


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
