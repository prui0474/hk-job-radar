"""无头 UA 隐去逻辑测试（browser.hide_headless）。

背景（2026-11-12 实测）：JobsDB 的 Cloudflare 会放行普通 Chrome，但会拦下
UA 里写着 `HeadlessChrome` 的会话。同一台机器、同一份启动参数：

| UA 里的字样 | 挑战页 | 抓到的岗位 |
|---|---|---|
| HeadlessChrome/156 | 45 秒不放行 | 0 |
| Chrome/157 | 没有出现 | 30 |

这里锁三件事：
1. 只替换 `HeadlessChrome` 这一个词，版本号与平台原样保留；
2. 开关关闭时不传 `user_agent`（产品默认值必须与 README 的说法一致）；
3. 有头模式不传，UA 本来就没有那个词，多此一举。

兼容两种运行方式：pytest 与 `python tests/test_browser_ua.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.config import AppConfig                      # noqa: E402
from hk_job_radar.scrapers.browser import BrowserManager      # noqa: E402

HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) HeadlessChrome/156.0.8078.4 Safari/537.36")


# ---------------------------------------------------------------------------
# 字符串层面的替换
# ---------------------------------------------------------------------------
def test_strip_only_replaces_the_headless_word():
    out = BrowserManager.strip_headless_marker(HEADLESS_UA)
    assert "HeadlessChrome" not in out
    assert "Chrome/156.0.8078.4" in out, "版本号必须原样保留，不能写成别的版本"
    assert out == HEADLESS_UA.replace("HeadlessChrome", "Chrome")


def test_strip_leaves_normal_ua_untouched():
    normal = HEADLESS_UA.replace("HeadlessChrome", "Chrome")
    assert BrowserManager.strip_headless_marker(normal) == normal


def test_strip_handles_empty():
    assert BrowserManager.strip_headless_marker("") == ""
    assert BrowserManager.strip_headless_marker(None) == ""   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 启动参数层面：什么时候该传 user_agent
# ---------------------------------------------------------------------------
class _FakePage:
    def __init__(self, ua: str = "") -> None:
        self.ua = ua

    def evaluate(self, script: str):        # noqa: ARG002
        return self.ua


class _FakeProbeBrowser:
    def __init__(self, ua: str) -> None:
        self.ua = ua

    def new_page(self):
        return _FakePage(self.ua)

    def close(self) -> None:
        pass


class _FakeContext:
    def new_page(self):
        return _FakePage()


class _FakeChromium:
    def __init__(self, ua: str) -> None:
        self.ua = ua
        self.persistent_kwargs: dict = {}
        self.probe_launches = 0

    def launch(self, **kwargs):             # noqa: ARG002
        self.probe_launches += 1
        return _FakeProbeBrowser(self.ua)

    def launch_persistent_context(self, **kwargs):
        self.persistent_kwargs = kwargs
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, ua: str = HEADLESS_UA) -> None:
        self.chromium = _FakeChromium(ua)


def _launch_with(tmp_path, *, hide: bool, headless: bool = True, ua: str = HEADLESS_UA):
    cfg = AppConfig(browser_headless=headless, browser_hide_headless=hide)
    manager = BrowserManager(cfg)
    fake = _FakePlaywright(ua)
    manager._playwright = fake
    page = _launch(manager, "testsrc", tmp_path)
    return fake.chromium, manager, page


def _launch(manager: BrowserManager, source_key: str, browser_dir: Path):
    """把 BROWSER_DIR 指到临时目录，避免在真实 data/browser 下建档案目录。"""
    import hk_job_radar.config as cfg_module

    old = cfg_module.BROWSER_DIR
    cfg_module.BROWSER_DIR = browser_dir
    try:
        return manager.launch(source_key)
    finally:
        cfg_module.BROWSER_DIR = old


def test_user_agent_passed_only_when_enabled(tmp_path):
    chromium, _, page = _launch_with(tmp_path, hide=True)
    assert page is not None
    passed = chromium.persistent_kwargs.get("user_agent", "")
    assert passed and "HeadlessChrome" not in passed
    assert "Chrome/156.0.8078.4" in passed


def test_no_user_agent_when_disabled(tmp_path):
    """默认关闭：不改变任何现有用户的行为（这也是 README 口径能站住的前提）。"""
    chromium, _, _ = _launch_with(tmp_path, hide=False)
    assert "user_agent" not in chromium.persistent_kwargs


def test_no_user_agent_when_headed(tmp_path):
    chromium, _, _ = _launch_with(tmp_path, hide=True, headless=False)
    assert "user_agent" not in chromium.persistent_kwargs


def test_ua_is_read_once_and_cached(tmp_path):
    """同一进程内多处 launch（jobsdb/linkedin/indeed）只该多起一次探测浏览器。"""
    chromium, manager, _ = _launch_with(tmp_path, hide=True)
    assert chromium.probe_launches == 1
    _launch(manager, "testsrc2", tmp_path / "b")
    assert chromium.probe_launches == 1, "第二次应命中缓存，不该再起一个浏览器"


def test_probe_failure_falls_back_to_no_override(tmp_path):
    """读不到 UA 就别传：宁可沿用原始 UA，也不能因为一项可选优化崩掉抓取。"""
    class _BrokenChromium(_FakeChromium):
        def launch(self, **kwargs):        # noqa: ARG002
            raise RuntimeError("内核起不来")

    cfg = AppConfig(browser_headless=True, browser_hide_headless=True)
    manager = BrowserManager(cfg)
    fake = _FakePlaywright()
    fake.chromium = _BrokenChromium(HEADLESS_UA)
    manager._playwright = fake
    _launch(manager, "testsrc", tmp_path)
    assert "user_agent" not in fake.chromium.persistent_kwargs


if __name__ == "__main__":
    import tempfile

    _tmp = Path(tempfile.mkdtemp())
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        if fn.__code__.co_argcount:
            fn(_tmp)
        else:
            fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
