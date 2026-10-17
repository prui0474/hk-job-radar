"""一键自检（doctor）单元测试：只测纯逻辑，不联网、不读真实配置。

为什么这些测试重要：doctor 的价值全在「说人话」上 —— 哪一项没过、怎么修。
如果渲染逻辑坏了（比如漏掉失败项、总结算错），用户会以为"全都通过了"，
然后带着坏配置去抓取，白等一整天。这类静默故障比报错难发现得多。

同时兼容两种运行方式：
- pytest tests/test_doctor.py
- python tests/test_doctor.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.doctor import (
    FAIL,
    OK,
    SKIP,
    WARN,
    Check,
    _display_width,
    _pad,
    _writable,
    render,
)


def _sample(statuses: list[str]) -> list[Check]:
    """按给定状态造一组检查项（分组也覆盖到 4 个组）。"""
    groups = ["环境", "配置", "凭据", "数据"]
    names = ["Python 版本", "虚拟环境", "依赖包", "浏览器内核",
             "配置文件", "关键词", "求职阶段", "个人画像",
             "AI 密钥", "AI 连通", "推送通道",
             "数据目录", "数据库", "浏览器档案"]
    return [Check(groups[min(i // 4, 3)], names[i], status, f"明细{i}")
            for i, status in enumerate(statuses)]


def test_render_reports_nothing_when_all_pass():
    text = render(_sample([OK] * 14))
    assert "共 14 项：14 项通过" in text
    assert "全部通过" in text
    assert "请先处理" not in text


def test_render_lists_numbers_of_failed_items():
    # 第 6 项（关键词）和第 11 项（推送通道）失败
    statuses = [OK] * 14
    statuses[5] = FAIL
    statuses[10] = FAIL
    text = render(_sample(statuses))
    assert "2 项需要处理" in text
    assert "请先处理第 6、11 项" in text


def test_render_treats_warnings_as_non_blocking():
    statuses = [OK] * 14
    statuses[7] = WARN
    text = render(_sample(statuses))
    assert "1 项提醒" in text
    assert "请先处理" not in text
    assert "建议看一眼" in text


def test_render_hint_only_first_line_gets_arrow():
    # 多行提示（比如要填的 YAML 片段）不该每行都挂一个箭头
    checks = [Check("环境", "测试项", FAIL, "说明", "第一行\nsecond\nthird")]
    text = render(checks)
    body = [ln for ln in text.splitlines()
            if any(k in ln for k in ("第一行", "second", "third"))]
    assert len(body) == 3
    assert body[0].count("→") == 1
    assert "→" not in body[1] and "→" not in body[2]
    # 续行缩进应与首行文字起始位置对齐
    assert body[1].index("s") == body[0].index("第")


def test_render_prints_each_group_header_once():
    text = render(_sample([OK] * 14))
    for group in ("【环境】", "【配置】", "【凭据】", "【数据】"):
        assert text.count(group) == 1


def test_skip_is_counted_separately():
    statuses = [OK] * 13 + [SKIP]
    text = render(_sample(statuses))
    assert "13 项通过" in text
    assert "1 项跳过" in text


def test_display_width_counts_chinese_as_two_columns():
    assert _display_width("abc") == 3
    assert _display_width("关键词") == 6
    assert _display_width("Python 版本") == 6 + 1 + 4


def test_pad_makes_chinese_and_ascii_line_up():
    assert _display_width(_pad("关键词", 12)) == 12
    assert _display_width(_pad("Python 版本", 12)) == 12


def test_symbols_are_distinct_per_status():
    symbols = {status: Check("环境", "x", status).symbol for status in (OK, WARN, FAIL, SKIP)}
    assert len(set(symbols.values())) == 4


def test_writable_accepts_normal_directory():
    good, _ = _writable(Path(tempfile.mkdtemp()))
    assert good


def test_writable_rejects_path_under_a_file():
    holder = Path(tempfile.mkdtemp()) / "a_file"
    holder.write_text("x", encoding="utf-8")
    good, error = _writable(holder / "sub")
    assert not good and error


def test_cli_registers_doctor_command():
    from hk_job_radar.cli import _build_parser

    parser = _build_parser()
    action = next(a for a in parser._actions if getattr(a, "choices", None)
                  and "doctor" in a.choices)   # noqa: SLF001 - 测试专用
    assert "doctor" in action.choices
    args = parser.parse_args(["doctor", "--no-llm"])
    assert args.no_llm is True
    assert args.func.__name__ == "cmd_doctor"


# ---------------------------------------------------------------------------
# 网络出口：验证组件通不通，决定「一直转圈」是网络问题还是配置问题
# ---------------------------------------------------------------------------
def _with_network(reachable: bool, system_proxy: str, cfg_proxy: str = "") -> Check:
    """在「全部不通 / 全部能通」两种网络下跑一次网络检查。

    直接替换探测函数，不真的联网 —— 否则测试结果取决于跑测试的人在哪。
    """
    import hk_job_radar.doctor as doc
    from hk_job_radar.config import AppConfig

    original_tcp, original_proxy = doc.tcp_probe, doc._system_proxy
    doc.tcp_probe = lambda host, port=443, timeout=2.5: reachable
    doc._system_proxy = lambda: system_proxy
    try:
        return doc._check_network(AppConfig(browser_proxy=cfg_proxy))
    finally:
        doc.tcp_probe, doc._system_proxy = original_tcp, original_proxy


def test_network_ok_when_probes_reachable():
    check = _with_network(True, system_proxy="")
    assert check.status == OK
    assert check.group == "环境"


def test_network_points_to_browser_proxy_when_unused_system_proxy_exists():
    """系统配了代理但程序默认直连 —— 这是内地用户最常见的死结，
    提示必须直接给出「改成 system」这个动作，而不是泛泛说网络不好。"""
    check = _with_network(False, system_proxy="127.0.0.1:7890")
    assert check.status == WARN
    assert "system" in check.hint
    assert "127.0.0.1:7890" in check.hint


def test_network_suggests_tun_when_no_proxy_at_all():
    check = _with_network(False, system_proxy="")
    assert check.status == WARN
    assert "TUN" in check.hint


def test_network_reports_which_probe_failed():
    import hk_job_radar.doctor as doc
    from hk_job_radar.config import AppConfig

    original = doc.tcp_probe
    doc.tcp_probe = lambda host, port=443, timeout=2.5: "google" not in host
    try:
        check = doc._check_network(AppConfig())
    finally:
        doc.tcp_probe = original
    assert check.status == WARN
    assert "人机验证" in check.detail


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
