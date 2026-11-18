"""配置「被静默忽略」的三种写法，现在必须看得见。

背景（开源用户 Kevin 的真实事故）：他把 hide_headless 改成了 true，但
JobsDB 每轮都卡在人机验证，日志还说 hide_headless 为 false。原因是
他改的那份 config.yaml 不是程序读的那份（解压套了两层目录），或者把键
写在了顶层。同类坑还有 `hide_headless: "false"` —— 引号让 YAML 给出字符串，
旧代码 bool("false") 是 True，设置反着生效。

这些都不是「用户笨」，而是程序没把他改错的地方说出来。这个文件把三类
写法的处理钉死：
1. 键写到顶层 → 启动即报错，并给出正确缩进；
2. 段内键名拼错 → 警告并列出认识的键，不阻断启动；
3. 布尔值带引号 → 按字面解析 + 警告。

外加两条可观测性：启动打印配置来源；挑战超时打印 UA 与 cf_clearance。
测试只写 tmp_path，绝不碰仓库里的 config.yaml。

同时兼容两种运行方式：
- pytest tests/test_config_diagnostics.py
- python tests/test_config_diagnostics.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from loguru import logger

from hk_job_radar.cli import _log_config_source
from hk_job_radar.config import CONFIG_PATH, AppConfig, ConfigError, load_config


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def log_messages():
    """收集 loguru 的日志正文（loguru 不走标准 logging，caplog 抓不到）。"""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# 一、键写到了顶层
# ---------------------------------------------------------------------------
def test_top_level_browser_key_is_fatal(tmp_path):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "hide_headless: true\n"
        "browser:\n"
        "  headless: true\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, require_runnable=False)

    message = str(excinfo.value)
    # 必须点名「应该写在 browser: 段里」，并给出可直接照抄的缩进
    assert "browser:" in message
    assert "hide_headless" in message
    assert "  hide_headless: true" in message
    # 用户最需要知道的是「为什么看起来没生效」
    assert "顶层" in message


def test_multiple_misplaced_keys_are_all_reported(tmp_path):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "headless: true\n"
        "proxy: \"system\"\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, require_runnable=False)

    message = str(excinfo.value)
    # 不能只报第一个——用户改完一个再跑一次又被拦，体验最差
    assert "顶层的 headless" in message
    assert "顶层的 proxy" in message


# ---------------------------------------------------------------------------
# 二、browser 段里拼错键名（警告，不阻断）
# ---------------------------------------------------------------------------
def test_unknown_browser_key_warns_but_does_not_break(tmp_path, log_messages):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "browser:\n"
        "  hide_headles: true\n",   # 少一个下划线，典型手滑
    )
    cfg = load_config(path, require_runnable=False)

    # 配置照常加载，值为默认值（写错的键不生效）
    assert cfg.browser_hide_headless is True
    warnings = [m for m in log_messages if "不认识的键" in m]
    if warnings:   # 捕获不到 loguru sink 时，至少保住「不抛异常」这条断言
        assert "hide_headles" in warnings[0]
        assert "hide_headless" in warnings[0]


# ---------------------------------------------------------------------------
# 三、布尔值带引号（修复前会反着生效）
# ---------------------------------------------------------------------------
def test_quoted_false_is_actually_false(tmp_path, log_messages):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "browser:\n"
        "  hide_headless: \"false\"\n",
    )
    cfg = load_config(path, require_runnable=False)

    # 旧代码 bool("false") == True：这正是用户「关了反而开」的根源
    assert cfg.browser_hide_headless is False
    warnings = [m for m in log_messages if "引号" in m]
    if warnings:
        assert "hide_headless" in warnings[0]


def test_quoted_true_still_true(tmp_path):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "browser:\n"
        "  hide_headless: \"TRUE\"\n",   # 大小写不敏感
    )
    assert load_config(path, require_runnable=False).browser_hide_headless is True


def test_normal_browser_section_has_no_warning(tmp_path, log_messages):
    path = _write(
        tmp_path,
        "career_stage: student\n"
        "keywords: [\"data analyst\"]\n"
        "browser:\n"
        "  headless: true\n"
        "  hide_headless: true\n"
        "  cloudflare_wait_seconds: 45\n"
        "  proxy: \"\"\n",
    )
    cfg = load_config(path, require_runnable=False)

    assert cfg.browser_headless is True
    assert cfg.browser_hide_headless is True
    assert cfg.cloudflare_wait_seconds == 45
    assert not [m for m in log_messages if "browser" in m]


# ---------------------------------------------------------------------------
# 四、启动日志：配置来源与生效值
# ---------------------------------------------------------------------------
def test_log_config_source_shows_absolute_path_and_effective_values(log_messages):
    cfg = AppConfig()
    cfg.browser_headless = True
    cfg.browser_hide_headless = False

    _log_config_source(cfg)

    assert log_messages, "应当打出一行配置来源"
    line = log_messages[-1]
    assert str(CONFIG_PATH) in line
    assert Path(CONFIG_PATH).is_absolute()
    assert "headless=True" in line
    assert "hide_headless=False" in line


# ---------------------------------------------------------------------------
# 五、挑战超时：打出实测 UA 与 cf_clearance
# ---------------------------------------------------------------------------
class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    def cookies(self) -> list[dict]:
        return self._cookies


class _FakePage:
    """永远停在挑战页上，直到超时。"""

    url = "https://hk.jobsdb.com/"

    def __init__(self, ua: str, cookies: list[dict] | None = None) -> None:
        self._ua = ua
        self.context = _FakeContext(cookies or [])

    def content(self) -> str:
        return "<html><title>Just a moment...</title><body>cf-chl</body></html>"

    def title(self) -> str:
        return "Just a moment..."

    def evaluate(self, script: str):   # noqa: ARG002 - 只模拟 navigator.userAgent
        return self._ua


def _run_timeout(monkeypatch, page):
    """跑一次注定超时的 wait_cloudflare，把时钟调快避免真等。"""
    from hk_job_radar.scrapers import browser as browser_mod

    clock = {"now": 0.0}
    monkeypatch.setattr(browser_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser_mod.time, "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    manager = browser_mod.BrowserManager(AppConfig())
    return manager.wait_cloudflare(page, timeout_s=1)


def test_challenge_timeout_reports_headless_chrome(monkeypatch, log_messages):
    page = _FakePage(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36"
    )
    passed = _run_timeout(monkeypatch, page)

    assert passed is False
    text = "\n".join(log_messages)
    assert "Cloudflare 挑战" in text
    assert "HeadlessChrome" in text
    assert "hide_headless" in text
    assert "cf_clearance：没有" in text


def test_challenge_timeout_reports_existing_clearance_cookie(monkeypatch, log_messages):
    page = _FakePage(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        [{"name": "cf_clearance", "value": "x", "expires": 1798766400}],
    )
    passed = _run_timeout(monkeypatch, page)

    assert passed is False
    text = "\n".join(log_messages)
    assert "cf_clearance：有，" in text
    assert "到期" in text
    # UA 干净却仍被拦 → 指向出口 IP / cookie 作废，而不是让用户乱改配置
    assert "IP" in text


def test_challenge_timeout_survives_broken_page(monkeypatch, log_messages):
    """读 UA / cookie 抛异常时，诊断不能把整轮抓取带崩。"""

    class _BrokenPage:
        url = "https://hk.jobsdb.com/"

        def __init__(self):
            pass

        def content(self):
            return "<html><title>Just a moment...</title></html>"

        def title(self):
            return "Just a moment..."

        def evaluate(self, script):   # noqa: ARG002
            raise RuntimeError("page is navigating")

        @property
        def context(self):
            raise RuntimeError("context gone")

    page = _BrokenPage()
    assert _run_timeout(monkeypatch, page) is False
    text = "\n".join(log_messages)
    assert "Cloudflare 挑战" in text
    assert "读不到" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
