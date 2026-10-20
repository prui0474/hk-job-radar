"""安装链路（install → 向导 → 体检 → 验证）的回归测试。

为什么单独开一个文件
--------------------
这一层最容易「看着没问题、用户那里却断掉」，而且断了没有任何报错：

1. **向导的退出码**。安装脚本全靠「向导返回 3」来判断用户到底有没有点
   保存。如果这个码被改成别的值，脚本会把「没保存」当成「保存成功」，
   然后拿一份空配置去跑体检，用户看到一堆 ❌ 却不知道是自己没点保存。

2. **脚本里写的提示语 key**。install.bat / install.command 会调用
   `run.py guide <key>`。key 拼错的话，用户看到的是一行
   「没有这段提示语」而不是该怎么修 —— 比不提示还糟。

3. **批处理的 goto 目标**。goto 一个不存在的标签，cmd 会直接终止，
   用户双击后窗口一闪而过。这种错误只能靠静态检查抓。

4. **CI 守卫**。CI 里 `./install.command` 是脚本直接执行的，没有终端。
   没了守卫，配置向导会一直等人点，构建挂到超时。

兼容两种运行方式：
- pytest tests/test_install_chain.py
- python tests/test_install_chain.py
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_ROOT = Path(__file__).resolve().parents[1]

from hk_job_radar import install_guide as guide     # noqa: E402
from hk_job_radar import setup_wizard as w          # noqa: E402


# ---------------------------------------------------------------------------
# 假服务：只实现 run_wizard 真正用到的那几个成员
#   serve_forever 立刻返回 —— 等价于「用户把向导关掉了」。
#   这样能在不起端口、不开浏览器、不动真实 config.yaml 的前提下，
#   把 run_wizard 的收尾判断跑一遍。
# ---------------------------------------------------------------------------
class _FakeServer:
    def __init__(self, saved_ok: bool) -> None:
        self.server_port = 1
        self.token = "fake-token"
        self.saved_ok = saved_ok
        self.shutdown_called = False

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        return None


def _run_wizard_with(saved_ok: bool) -> int:
    fake = _FakeServer(saved_ok)
    silent = io.StringIO()
    with contextlib.redirect_stdout(silent):
        import unittest.mock as mock
        with mock.patch.object(w, "_bind_server", lambda: fake):
            code = w.run_wizard(port=None, open_browser=False, chained=True)
    assert fake.shutdown_called, "收尾时必须真的把服务关掉"
    return code


def test_unsaved_wizard_returns_dedicated_code():
    assert _run_wizard_with(saved_ok=False) == w.EXIT_UNSAVED


def test_saved_wizard_returns_zero():
    assert _run_wizard_with(saved_ok=True) == 0


def test_unsaved_code_does_not_collide_with_error_codes():
    # 1 = 页面文件缺失，2 = 端口用不了。未保存必须单独占一个值，
    # 否则安装脚本分不清「该提醒用户回去保存」还是「该报错」。
    assert w.EXIT_UNSAVED not in (0, 1, 2)


# ---------------------------------------------------------------------------
# 提示语本身
# ---------------------------------------------------------------------------
def test_every_message_is_non_empty():
    for key in guide.available():
        text = guide.message(key)
        assert text.strip(), f"{key} 是空文案"


def test_unknown_key_exits_with_usage_error():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = guide.main(["no-such-key"])
    assert code == 2
    assert "no-such-key" in buf.getvalue()


def test_install_scripts_only_reference_existing_keys():
    """脚本里 `run.py guide X` 用到的 X，必须都在文案表里。

    这是本轮最重要的一条防错：key 拼错不会报错，只会让用户在该被引导的
    时候看到一行「没有这段提示语」。
    """
    pattern = re.compile(r"run\.py\"?\s+guide\s+([A-Za-z][A-Za-z0-9_-]*)")
    known = set(guide.available())
    checked = 0
    for name in ("install.bat", "install.command"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        for key in pattern.findall(text):
            checked += 1
            assert key in known, f"{name} 引用了不存在的提示语：{key}"
    assert checked >= 6, f"只找到 {checked} 处 guide 调用，脚本可能没串起来"


# ---------------------------------------------------------------------------
# 批处理静态检查
# ---------------------------------------------------------------------------
def test_install_bat_goto_targets_all_exist():
    """每个 goto / call :label 都必须有对应的标签，否则双击后窗口一闪而过。

    注意：绝大多数 goto 是**条件跳转**（`if errorlevel 3 goto wizard_unsaved`），
    它们并不以 goto 开头。只匹配行首的 goto 会漏掉这些 —— 而那正是最容易
    写错的地方，所以这里用全文正则，并先把 REM 注释行剔掉（注释里可能
    为了讲解而提到 goto）。
    """
    text = (_ROOT / "install.bat").read_text(encoding="ascii")
    labels = set(re.findall(r"(?m)^\s*:([A-Za-z_][A-Za-z0-9_]*)", text))

    body = "\n".join(
        line for line in text.splitlines()
        if not line.strip().lower().startswith(("rem ", "::"))
    )
    targets = set(re.findall(r"(?i)\bgoto\s+:?([A-Za-z_][A-Za-z0-9_]*)", body))
    targets |= set(re.findall(r"(?i)\bcall\s+:([A-Za-z_][A-Za-z0-9_]*)", body))

    missing = {t for t in targets if t.lower() != "eof" and t not in labels}
    assert not missing, f"这些 goto 目标不存在：{sorted(missing)}"
    # 防「测试其实什么都没检查」：条件跳转必须真的被扫到
    assert len(targets) >= 8, f"只找到 {len(targets)} 个跳转目标，提取逻辑可能又漏了"


def test_install_scripts_guard_against_ci():
    """两边都必须有 CI 守卫：CI 里没人点向导，否则构建会挂到超时。"""
    bat = (_ROOT / "install.bat").read_text(encoding="ascii")
    assert "if defined CI goto chain_skipped" in bat
    assert ":chain_skipped" in bat

    sh = (_ROOT / "install.command").read_text(encoding="utf-8")
    assert 'INTERACTIVE=1' in sh
    assert '[ -t 0 ]' in sh and '${CI:-}' in sh
    # 体检也必须被守卫罩住：CI 里配置是空壳，必然报 ❌，
    # 不跳过的话 ./install.command 会以 1 退出，把构建弄红。
    body = sh[sh.index("第 7 步"):]
    assert 'if [ "$INTERACTIVE" = "1" ]' in body


def test_install_bat_stays_pure_ascii_crlf():
    """cmd.exe 用系统代码页解析 .bat，混进中文会被切碎甚至破坏命令解析。"""
    raw = (_ROOT / "install.bat").read_bytes()
    assert all(b < 128 for b in raw), "install.bat 出现了非 ASCII 字节"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "install.bat 有裸 LF"


def test_install_bat_does_not_pause_in_ci():
    raw = (_ROOT / "install.bat").read_text(encoding="ascii")
    for line in raw.splitlines():
        if line.strip() == "pause":
            raise AssertionError("存在无条件的 pause，无人值守时会挂住")
    assert "if not defined CI pause" in raw


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
