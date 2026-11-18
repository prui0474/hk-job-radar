"""关键词闸门的两项可选收紧：类型直通 + 标题资历词剔除。

背景（2026-11-18）：用户把 career_stage 放开到 both、job_types 加了 full time 之后，
Workday 一个源 190 条香港岗里 73 条过了关键词初筛，其中 56 条（77%）标题带资历词
（manager / senior / vice president / director / lead / head of）。

两个调整都必须是**配置驱动、默认关闭**，开源用户行为不变：

① keyword_gate_skip_types：已判定为 internship 的岗位跳过关键词匹配。
   为什么不能往 title_direction_hints 里塞 "intern"：_compile_direction_hints
   对超过 2 个字母的 ASCII 词只卡**前边界**，intern 会命中 internal /
   international / internet，等于放进来一批无关岗位。

② exclude_title_seniority：标题命中高管层资历词直接丢弃。
   ★ 关键反向约束：只剔高管层。Manager / Senior / Lead 是 3-5 年经验那一档，
     用户明确说要试（面过一家要 3-5 年但愿意按实习录用），**不能剔**。

兼容两种运行方式：
- pytest tests/test_keyword_gate_tuning.py
- python tests/test_keyword_gate_tuning.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from loguru import logger  # noqa: E402

from hk_job_radar.config import AppConfig, load_config  # noqa: E402
from hk_job_radar.models import JobPosting  # noqa: E402
from hk_job_radar.scoring.keyword_filter import KeywordFilter, _first_hit  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _job(title: str, snippet: str = "", site_type: str = "") -> JobPosting:
    return JobPosting(
        source="test", external_id=title[:24], title=title, company="Test Co",
        url=f"https://example.com/{title}", url_normalized=f"https://example.com/{title}",
        description_snippet=snippet, job_type=site_type,
    )


def _cfg(**kw) -> AppConfig:
    """默认与开源默认一致：两项都关，career_stage=both（这样全职岗不会被类型闸门拦）。"""
    base = dict(
        keywords=["data", "brand", "marketing", "product"],
        keyword_synonyms={},
        job_types=["internship", "part time", "contract", "temporary", "full time"],
    )
    base.update(kw)
    return AppConfig(**base)


SENIORITY = ["director", "head of", "vice president", "vp", "chief", "principal",
             "managing director", "總監", "总监"]


def _filtered(titles: list[str], **kw) -> list[JobPosting]:
    return KeywordFilter(_cfg(**kw)).filter([_job(t) for t in titles])


def _capture_log():
    """抓 loguru 的 message 文本，用来断言「日志里能看出命中的是哪个词」。"""
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    return lines, sink_id


# ---------------------------------------------------------------------------
# 1. 类型直通：实习不再过关键词（默认关闭）
# ---------------------------------------------------------------------------
def test_skip_type_lets_keywordless_internship_through():
    kept = _filtered(["Design Intern"], keyword_gate_skip_types=["internship"])
    assert [j.title for j in kept] == ["Design Intern"]
    # 证据要能看出是「按类型放的」，不是碰巧命中了什么词
    assert kept[0].keyword_matched == "类型直通: internship"


def test_skip_type_default_off_still_drops_keywordless_internship():
    """反向守护：不配这一项时，标题没有方向词的实习照旧被关键词丢掉。"""
    assert _filtered(["Design Intern"]) == []


def test_skip_type_only_affects_configured_kind():
    """配了 internship 不该顺带放行「类型不可确认」的岗位。"""
    kept = _filtered(["Barista"], keyword_gate_skip_types=["internship"])
    assert kept == []


def test_skip_type_can_target_other_kinds():
    kf = KeywordFilter(_cfg(keyword_gate_skip_types=["full time"]))
    kept = kf.filter([_job("Brand Ambassador", site_type="Full time")])
    assert [j.keyword_matched for j in kept] == ["类型直通: full time"]


# ---------------------------------------------------------------------------
# 2. 资历剔除：高管层命中即丢
# ---------------------------------------------------------------------------
def test_director_title_is_dropped():
    kept = _filtered(["Director, AI Product"], exclude_title_seniority=SENIORITY)
    assert kept == []


def test_head_of_and_vp_are_dropped():
    kept = _filtered(["Head of Brand Marketing", "Vice President, Data"],
                     exclude_title_seniority=SENIORITY)
    assert kept == []


def test_chinese_seniority_marker_is_dropped():
    """中文按子串匹配，繁简各写一份（匹配不做繁简转换）。"""
    assert _filtered(["市場總監"], exclude_title_seniority=SENIORITY) == []
    assert _filtered(["数据总监"], exclude_title_seniority=SENIORITY) == []


def test_seniority_default_off_keeps_director():
    """反向守护：不配黑名单时，Director 该由关键词/方向词决定，不因资历被剔。"""
    kept = _filtered(["Director, Data Platform"])
    assert [j.title for j in kept] == ["Director, Data Platform"]


def test_seniority_drop_logs_the_matched_word():
    """日志里要能看出是哪个词命中的，否则调配置只能靠猜。"""
    lines, sink_id = _capture_log()
    try:
        _filtered(["Director, Brand Marketing"], exclude_title_seniority=SENIORITY)
    finally:
        logger.remove(sink_id)
    assert any("资历不符剔除（director）" in line for line in lines), lines


def test_first_hit_returns_first_configured_term():
    from hk_job_radar.scoring.keyword_filter import _compile_direction_hints
    patterns = _compile_direction_hints(["vp", "vice president"])
    assert _first_hit(patterns, "Vice President, Data") in {"vp", "vice president"}
    assert _first_hit(patterns, "Data Analyst") == ""


# ---------------------------------------------------------------------------
# 3. 关键反向约束：3-5 年那一档不能剔
# ---------------------------------------------------------------------------
def test_mid_level_titles_survive_seniority_blacklist():
    """Manager / Senior / Lead 是 3-5 年经验那一档，用户明确要试。"""
    titles = ["Senior Data Engineer", "Manager, Brand Activation", "Lead Data Engineer"]
    kept = _filtered(titles, exclude_title_seniority=SENIORITY)
    assert sorted(j.title for j in kept) == sorted(titles)


def test_senior_survives_but_director_does_not():
    kept = _filtered(["Senior Brand Manager", "Brand Director"],
                     exclude_title_seniority=SENIORITY)
    assert [j.title for j in kept] == ["Senior Brand Manager"]


# ---------------------------------------------------------------------------
# 4. 顺序：类型直通先于资历剔除
# ---------------------------------------------------------------------------
def test_internship_passes_before_seniority_check():
    """`Intern, Director's Office` 是实习，director 只是部门名，不能被剔。"""
    kept = _filtered(["Intern, Director's Office"],
                     keyword_gate_skip_types=["internship"],
                     exclude_title_seniority=SENIORITY)
    assert [j.title for j in kept] == ["Intern, Director's Office"]
    assert kept[0].keyword_matched == "类型直通: internship"


# ---------------------------------------------------------------------------
# 5. 默认关闭 = 与改动前完全一致
# ---------------------------------------------------------------------------
def test_both_options_empty_behave_like_before():
    """空配置下，结果只由「类型闸门 + 关键词 + 方向词」决定。"""
    titles = [
        "Data Analyst Intern",          # 关键词命中
        "Design Intern",                # 无关键词 → 丢
        "Director, Brand Marketing",    # 关键词命中 brand → 留（不剔）
        "Head of Data Science",         # 关键词 data → 留（不剔）
        "Barista",                      # 无关键词 → 丢
    ]
    kept = _filtered(titles)
    assert [j.title for j in kept] == [
        "Data Analyst Intern", "Director, Brand Marketing", "Head of Data Science",
    ]


def test_explicit_empty_lists_same_as_unset():
    a = KeywordFilter(_cfg())
    b = KeywordFilter(_cfg(keyword_gate_skip_types=[], exclude_title_seniority=[]))
    titles = ["Marketing Intern", "Director, Data", "Senior Data Engineer"]
    assert [j.title for j in a.filter([_job(t) for t in titles])] == \
           [j.title for j in b.filter([_job(t) for t in titles])]


# ---------------------------------------------------------------------------
# 6. 计数器自洽（日志里的数字能对上）
# ---------------------------------------------------------------------------
def test_log_counters_are_consistent():
    """kept == 总数 - 类型剔除 - 排除 - 不可确认 - 资历剔除 - 关键词丢弃。"""
    lines, sink_id = _capture_log()
    try:
        jobs = [
            _job("Marketing Intern", site_type="Internship"),      # 类型直通
            _job("Director, Data", site_type="Full time"),          # 资历剔除
            _job("Senior Data Engineer", site_type="Full time"),    # 关键词保留
            _job("Barista", site_type="Full time"),                 # 关键词丢弃
            _job("Data Analyst Intern", site_type="Internship"),    # 关键词命中
        ]
        kept = KeywordFilter(_cfg(keyword_gate_skip_types=["internship"],
                                  exclude_title_seniority=SENIORITY)).filter(jobs)
    finally:
        logger.remove(sink_id)

    line = next(x for x in lines if x.startswith("关键词初筛："))

    def num(pattern: str) -> int:
        m = re.search(pattern, line)
        assert m, f"{pattern} 没出现在日志里：{line}"
        return int(m.group(1))

    total = num(r"关键词初筛：(\d+) → ")
    kept_n = num(r"关键词初筛：\d+ → (\d+)（")
    type_d = num(r"剔除 (\d+) 个不符合当前求职阶段")
    type_p = num(r"类型直通放行 (\d+) 个")
    senior_d = num(r"资历不符剔除 (\d+) 个")
    dir_p = num(r"方向词直通放行 (\d+) 个") if "方向词直通放行" in line else 0
    kw_d = num(r"丢弃 (\d+) 个不相关岗位")
    excluded_d = num(r"按配置排除 (\d+) 个") if "按配置排除" in line else 0
    unknown_d = num(r"类型不可确认丢弃 (\d+) 个") if "类型不可确认丢弃" in line else 0

    assert kept_n == len(kept) == 3
    assert total == len(jobs) == 5
    # 两个实习都走类型直通（含标题命中关键词的那个——直通在关键词之前）
    assert type_p == 2 and senior_d == 1 and kw_d == 1 and dir_p == 0
    assert kept[0].keyword_matched == "类型直通: internship"
    # 核心恒等式：放行数 + 全部丢弃桶 == 总数
    assert kept_n == total - type_d - excluded_d - unknown_d - senior_d - kw_d
    # 日志里两段新表述都要出现
    assert "类型直通放行 2 个" in line
    assert "资历不符剔除 1 个" in line


# ---------------------------------------------------------------------------
# 7. 配置加载：键名拼错会静默失效，这里钉住读取路径
# ---------------------------------------------------------------------------
def test_load_config_reads_both_new_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "keywords: [\"data\"]\n"
        "job_types: [\"full time\"]\n"
        "keyword_gate_skip_types:\n  - \"Internship\"\n"
        "exclude_title_seniority:\n  - \"Head of\"\n  - \"總監\"\n",
        encoding="utf-8",
    )
    cfg = load_config(p, require_runnable=False)
    assert cfg.keyword_gate_skip_types == ["internship"]      # 统一小写
    assert cfg.exclude_title_seniority == ["Head of", "總監"]  # 原样保留（编译时再处理）


# ---------------------------------------------------------------------------
def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            import inspect
            if "tmp_path" in inspect.signature(fn).parameters:
                import tempfile
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
