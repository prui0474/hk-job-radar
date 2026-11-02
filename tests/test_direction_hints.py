"""标题方向词直通（title_direction_hints）单元测试：不依赖网络。

这一层守的是 2026-10-25 实测出来的瓶颈：关键词初筛只认「标题含完整岗位名」，
把大批真实对口岗当噪音扔了。实测 JobsDB 上 205 个真实实习/兼职岗位，
只有 8 个（3.9%）能进 LLM 打分，被丢的含：

    AI R&D Engineering Intern（不含 "ai engineer"）
    Investment Risk Management Intern（不含任何关键词）
    intern - FICC/Research、DFIQ Insights Solutions Intern

于是加了一层「标题出现方向词就放行」，交给 LLM 判断相关性。

★ 关键反向守护：默认配置（title_direction_hints 为空）必须与加这层之前
  **完全一致**——开源用户不该因为这次改动多看到任何一条岗位。

★ 第二道反向守护：它绝不能变成绕过岗位类型安全网的后门。全职岗即使标题
  写了 "Data"，也必须在初筛阶段就被剔掉（学生签证红线）。

兼容两种运行方式：
- pytest tests/test_direction_hints.py
- python tests/test_direction_hints.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig  # noqa: E402
from hk_job_radar.models import JobPosting  # noqa: E402
from hk_job_radar.scoring.keyword_filter import KeywordFilter  # noqa: E402


def _job(title: str, snippet: str = "", site_type: str = "") -> JobPosting:
    return JobPosting(
        source="test", external_id=title[:24], title=title, company="Test Co",
        url=f"https://example.com/{title}", url_normalized=f"https://example.com/{title}",
        description_snippet=snippet, job_type=site_type,
    )


def _cfg(**kw) -> AppConfig:
    """默认参数与开源默认一致：方向词直通关闭。"""
    base = dict(keywords=["data analyst", "machine learning"], keyword_synonyms={})
    base.update(kw)
    return AppConfig(**base)


HINTS = ["data", "analytic", "ai", "machine learning", "llm", "quant",
         "risk", "research", "算法"]


def _filter(hints=HINTS, **kw) -> KeywordFilter:
    return KeywordFilter(_cfg(title_direction_hints=list(hints), **kw))


# ---------------------------------------------------------------------------
# 1. 默认关闭 = 行为完全不变（反向守护，最重要）
# ---------------------------------------------------------------------------
def test_default_off_keeps_old_behaviour():
    kf = KeywordFilter(_cfg())          # 没传 title_direction_hints
    jobs = [
        _job("Data Analyst Intern"),                    # 命中正式关键词 → 留
        _job("AI R&D Engineering Intern"),              # 只含方向词 → 旧行为下丢
        _job("Investment Risk Management Intern"),      # 同上
    ]
    kept = kf.filter(jobs)
    assert [j.title for j in kept] == ["Data Analyst Intern"]
    # 正式关键词的匹配证据不能被改写
    assert kept[0].keyword_matched == "data analyst"


def test_empty_hints_list_same_as_unset():
    a = KeywordFilter(_cfg(title_direction_hints=[]))
    b = KeywordFilter(_cfg())
    for job in (_job("AI R&D Engineering Intern"), _job("Part-time Research Assistant")):
        ja = a.filter([_job(job.title)])[0] if a.filter([_job(job.title)]) else None
        jb = b.filter([_job(job.title)])[0] if b.filter([_job(job.title)]) else None
        assert (ja is None) == (jb is None)


# ---------------------------------------------------------------------------
# 2. 开了之后，真实被误杀的标题能进来
# ---------------------------------------------------------------------------
def test_direction_hint_rescues_real_titles():
    kf = _filter()
    cases = [
        ("AI R&D Engineering Intern — AI Infrastructure", "ai"),
        ("Investment Risk Management Intern-investment dept", "risk"),
        ("Part-time Research Assistant, School of Management", "research"),
        ("算法专家/工程师（可兼职）", "算法"),
        ("Applied LLM Engineer", "llm"),
        ("Quantitative Researcher Intern", "quant"),
    ]
    for title, expect_hint in cases:
        kept = kf.filter([_job(title)])
        assert len(kept) == 1, f"应放行却被丢弃：{title}"
        assert kept[0].keyword_matched == f"方向词直通: {expect_hint}", title


def test_direction_hint_still_drops_irrelevant():
    kf = _filter()
    for title in ("Part Time Instructor", "Event Helpers (November 2026)",
                  "舞台音響工程學徒", "Marketing Assistant (Part-time)"):
        assert kf.filter([_job(title)]) == [], f"不该放行：{title}"


# ---------------------------------------------------------------------------
# 3. 只认标题，不认摘要
# ---------------------------------------------------------------------------
def test_hint_does_not_look_at_snippet():
    """JD 正文里 "data-driven" / "AI-powered" 遍地都是，放开摘要等于取消初筛。

    这条用「摘要含方向词、标题不含」的岗位验证：正式关键词不命中、
    方向词也不该在摘要里找，因此必须被丢弃。
    """
    kf = _filter()
    job = _job("Part Time Instructor",
               snippet="Join an AI-driven, data-first company. Algorithm exposure a plus.")
    assert kf.filter([job]) == []


def test_keyword_still_matches_snippet_even_with_hints_on():
    """原有设计不变：正式关键词本来就匹配 title+摘要，开了直通也不影响这一点。"""
    kf = _filter()
    job = _job("Part Time Instructor", snippet="Teach machine learning basics.")
    kept = kf.filter([job])
    assert len(kept) == 1
    assert kept[0].keyword_matched == "machine learning"


def test_hint_matches_title_even_when_snippet_empty():
    kf = _filter()
    assert len(kf.filter([_job("Data Governance Trainee")])) == 1


# ---------------------------------------------------------------------------
# 4. 词边界：短词不能当裸子串
# ---------------------------------------------------------------------------
def test_short_hint_needs_word_boundary():
    kf = _filter(hints=["ai"])
    # 应命中
    for t in ("AI Intern", "Intern, AI product", "AI-powered Analyst"):
        assert len(kf.filter([_job(t)])) == 1, f"应命中：{t}"
    # 不该命中：ai 嵌在别的词里
    for t in ("Email Marketing Assistant", "Retail Sales Assistant",
              "Air Conditioning Engineer", "Available Part-time Helper"):
        assert kf.filter([_job(t)]) == [], f"不该命中：{t}"


def test_three_letter_hint_allows_suffix():
    """三个字母以上刻意不加词尾边界：llm 要能命中 LLMs / LLM-powered，
    quant 要能命中 Quantitative；但 fulfillment 里的 llm 不能命中。"""
    kf = _filter(hints=["llm", "quant"])
    assert len(kf.filter([_job("LLM Engineer")])) == 1
    assert len(kf.filter([_job("LLMs Evaluation Intern")])) == 1
    assert len(kf.filter([_job("LLM-powered Search Intern")])) == 1
    assert len(kf.filter([_job("Quantitative Researcher Intern")])) == 1
    assert kf.filter([_job("Fulfillment Operations Assistant")]) == []


def test_data_hint_does_not_match_metadata_like_embedding():
    """\\bdata 要求词首边界：metadata 里的 data 前面不是边界，不该命中。"""
    kf = _filter(hints=["data"])
    assert kf.filter([_job("Metadata Operations Assistant")]) == []
    assert len(kf.filter([_job("Business Data Analyst")])) == 1


# ---------------------------------------------------------------------------
# 5. 优先级：正式关键词 > 方向词
# ---------------------------------------------------------------------------
def test_real_keyword_wins_over_hint():
    kf = _filter()
    kept = kf.filter([_job("Data Analyst Intern")])
    assert kept[0].keyword_matched == "data analyst"      # 不是 "方向词直通: data"


def test_synonym_wins_over_hint():
    kf = _filter(keyword_synonyms={"machine learning": ["ml engineer"]})
    kept = kf.filter([_job("ML Engineer Intern — Data Platform")])
    assert kept[0].keyword_matched == "machine learning（同义词: ml engineer）"


# ---------------------------------------------------------------------------
# 6. 不能绕过岗位类型安全网（学生签证红线）
# ---------------------------------------------------------------------------
def test_hint_cannot_bypass_fulltime_safety_net():
    """全职岗即使标题含方向词，也必须在初筛阶段被剔掉。"""
    kf = _filter()
    for title in ("Senior Data Analyst", "Risk Manager", "Research Director"):
        assert kf.filter([_job(title)]) == [], f"全职岗漏进来了：{title}"


def test_hint_respects_exclude_job_type_classes():
    kf = _filter(exclude_job_type_classes=["graduate programme"])
    assert kf.filter([_job("Data Analytics Graduate Trainee")]) == []


# ---------------------------------------------------------------------------
# 7. 计数与证据可追溯
# ---------------------------------------------------------------------------
def test_hint_marks_reason_for_troubleshooting():
    """keyword_matched 写成「方向词直通: xxx」，出问题时能一眼看出是这层放行的。"""
    kf = _filter()
    kept = kf.filter([_job("AI Marketing Intern")])
    assert kept[0].keyword_matched.startswith("方向词直通: ")


def test_mixed_batch_counts():
    kf = _filter()
    jobs = [
        _job("Data Analyst Intern"),          # 正式关键词
        _job("AI R&D Engineering Intern"),    # 方向词
        _job("Part Time Instructor"),         # 丢弃
    ]
    kept = kf.filter(jobs)
    assert len(kept) == 2
    assert {j.keyword_matched for j in kept} == {"data analyst", "方向词直通: ai"}


# ---------------------------------------------------------------------------
def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
        except Exception as exc:                     # noqa: BLE001
            failed += 1
            print(f"  ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
