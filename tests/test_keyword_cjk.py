"""中文（繁/简）岗位关键词与中文方向词直通的回归测试：不依赖网络、不读真实 config.yaml。

修的是什么问题（开源用户反馈）：
    教程把关键词写成「英文小写」，用户只填了英文词，搜出来全是英文岗位；
    而香港站上大量岗位是**中文标题**发布的。两层都挡：
      1) 抓取层：用英文词查，搜索站返回的就是英文标题的岗位；
      2) 初筛层：`_match()` 拿 title+摘要做子串匹配，纯中文标题一条都命中不了。

本文件钉住四条底线：
    A) 中文标题能被中文关键词命中（繁体、简体各自命中，互不代替）；
    B) 中文方向词直通只认「岗位名后缀」，不能靠一个「分析」把
       財務分析 / 市場分析 全部放行（否则等于取消初筛）；
    C) 加了中文词之后，**英文岗位的匹配结果必须一字不变**；
    D) 大小写不敏感（写 DATA ANALYST 与 data analyst 效果相同）。

兼容两种运行方式：
- pytest tests/test_keyword_cjk.py
- python tests/test_keyword_cjk.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig, load_config  # noqa: E402
from hk_job_radar.models import JobPosting  # noqa: E402
from hk_job_radar.scoring.keyword_filter import KeywordFilter  # noqa: E402

# 与 config.yaml 同口径的中文关键词（繁体在港站更常见，简体来自中资公司）
CJK_KEYWORDS = ["數據分析", "数据分析", "人工智慧", "人工智能", "機器學習", "机器学习",
                "數據科學家", "数据科学家", "研究助理", "精算", "量化研究"]


def _job(title: str, snippet: str = "", site_type: str = "") -> JobPosting:
    return JobPosting(
        source="test", external_id=title[:24], title=title, company="Test Co",
        url=f"https://example.com/{title}", url_normalized=f"https://example.com/{title}",
        description_snippet=snippet, job_type=site_type,
    )


def _cfg(**kw) -> AppConfig:
    base = dict(keywords=list(CJK_KEYWORDS), keyword_synonyms={},
                title_direction_hints=[])
    base.update(kw)
    return AppConfig(**base)


def _kept_titles(kf: KeywordFilter, titles: list[str]) -> list[str]:
    return [j.title for j in kf.filter([_job(t) for t in titles])]


# ---------------------------------------------------------------------------
# A. 中文标题能被中文关键词命中
# ---------------------------------------------------------------------------
def test_traditional_title_matched_by_traditional_keyword():
    """「數據分析員」这类纯中文标题，英文关键词一条都命中不了，必须靠中文词接住。"""
    kf = KeywordFilter(_cfg())
    kept = kf.filter([_job("數據分析員（兼職）")])
    assert len(kept) == 1, "中文标题被丢弃了——中文关键词没生效"
    assert kept[0].keyword_matched == "數據分析"


def test_simplified_title_matched_by_simplified_keyword():
    kf = KeywordFilter(_cfg())
    kept = kf.filter([_job("数据分析实习生")])
    assert len(kept) == 1
    assert kept[0].keyword_matched == "数据分析"


def test_traditional_and_simplified_do_not_cover_each_other():
    """繁简不会互相匹配（.lower() 不动中文），所以两份都要写。

    这条不是为了鼓励写两遍，而是钉住「写了繁体不等于覆盖简体」这个事实——
    只填「數據分析」时，简体岗位仍然漏；用户加词时必须繁简各写一份。
    """
    trad_only = KeywordFilter(_cfg(keywords=["數據分析"]))
    assert _kept_titles(trad_only, ["數據分析員"]) == ["數據分析員"]
    assert _kept_titles(trad_only, ["数据分析员"]) == [], "繁体词不该命中简体标题"

    simp_only = KeywordFilter(_cfg(keywords=["数据分析"]))
    assert _kept_titles(simp_only, ["数据分析员"]) == ["数据分析员"]
    assert _kept_titles(simp_only, ["數據分析員"]) == [], "简体词不该命中繁体标题"


def test_cjk_keyword_matches_snippet_too():
    """匹配范围仍是「标题 + 摘要」，中文同理（摘要里有中文岗位描述时也算）。"""
    kf = KeywordFilter(_cfg())
    assert _kept_titles(kf, ["兼職人員"]) == []          # 标题摘要都不含 → 丢
    job = _job("兼職人員", snippet="負責數據分析與報表")
    assert kf.filter([job]), "摘要里的中文关键词应当命中"


def test_cjk_keyword_does_not_match_english_only_title():
    """反向守护：加了中文词，不能让纯英文的无关岗位混进来。"""
    kf = KeywordFilter(_cfg())
    assert _kept_titles(kf, ["Warehouse Supervisor", "Retail Sales Associate"]) == []


# ---------------------------------------------------------------------------
# B. 中文方向词直通：只认岗位名后缀，不能一个「分析」放行全部
# ---------------------------------------------------------------------------
CJK_HINTS = ["分析員", "分析员", "分析師", "分析师", "研究員", "研究员",
             "科學家", "科学家", "演算法", "算法", "機器學習", "机器学习"]


def test_cjk_hint_rescues_real_titles():
    """方向词要能捞回「词表没写全」的真实标题写法。"""
    kf = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=CJK_HINTS))
    cases = [
        ("量化研究員（可兼職）", "研究員"),
        ("資料科學家 - 兼職", "科學家"),
        ("風險分析師 (Part-time)", "分析師"),
        ("演算法工程師實習生", "演算法"),
    ]
    for title, hint in cases:
        kept = kf.filter([_job(title)])
        assert len(kept) == 1, f"应放行却被丢弃：{title}"
        assert kept[0].keyword_matched == f"方向词直通: {hint}", title


def test_bare_analysis_word_would_leak_but_suffix_does_not():
    """核心反向守护：方向词写「分析」会放行財務分析/市場分析/信用分析；
    写「分析員/分析師」则不会——这正是选后缀而不是选短词的原因。
    """
    generic = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=["分析"]))
    assert _kept_titles(generic, ["財務分析"]) == ["財務分析"], \
        "对照组：裸「分析」确实会误伤（这是没选它的原因）"

    suffix = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=CJK_HINTS))
    for title in ["財務分析", "市場分析", "信用分析", "風險分析（不含師／員）"]:
        assert _kept_titles(suffix, [title]) == [], f"不该被中文方向词放行：{title}"


def test_cjk_hint_still_drops_unrelated_titles():
    kf = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=CJK_HINTS))
    for title in ["舞台音響工程學徒", "美容顧問", "倉務員", "客戶服務主任"]:
        assert _kept_titles(kf, [title]) == [], f"不该放行：{title}"


def test_cjk_hint_cannot_bypass_fulltime_safety_net():
    """红线不变：方向词只是「放行进 LLM」，不能绕开岗位类型安全网。"""
    kf = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=CJK_HINTS))
    for title in ["高級數據分析師", "財務經理", "研究總監"]:
        assert _kept_titles(kf, [title]) == [], f"全职岗漏进来了：{title}"


# ---------------------------------------------------------------------------
# B2. 英文方向词紧挨汉字时也必须命中（2026-11-02 修）
#
# 老实现用 \b 做边界，而 Python 的 \b 把汉字算「单词字符」，于是
# 「AI原生產品經理」里 "AI" 后面不算词尾 → \bai\b 不命中 → 这条中文标题被丢，
# 尽管它就是我们最想要的 AI 岗。中文标题里 "AI數據"「BI分析員」这类写法极常见，
# 所以边界改成「前后不能是 [0-9a-z_]」。
# ---------------------------------------------------------------------------
def test_ascii_hint_matches_when_adjacent_to_cjk():
    kf = KeywordFilter(_cfg(keywords=["data analyst"], title_direction_hints=["ai", "bi"]))
    for title in ["AI原生產品實習生", "AI工程師（實習）", "BI分析員"]:
        kept = kf.filter([_job(title)])
        assert len(kept) == 1, f"紧挨汉字时没命中：{title}"
    # 原有的防误命中不能被放宽
    for title in ["Email Marketing Assistant", "Retail Sales Assistant", "Air Conditioning Engineer"]:
        assert _kept_titles(kf, [title]) == [], f"不该命中：{title}"


def test_hint_boundary_unchanged_on_pure_english_titles():
    """纯英文标题上，新边界（ASCII 查视）与老边界（\\b）判定必须逐条相同。

    这是「改中文适配不能顺带改英文口径」的证据：这里用老实现现算一遍做对照。
    """
    import re as _re

    hints = ["ai", "data", "quant", "llm", "risk", "analytic", "research"]
    legacy = [(h, _re.compile(r"\b" + _re.escape(h) + r"\b", _re.I) if len(h) <= 2
               else _re.compile(r"\b" + _re.escape(h), _re.I)) for h in hints]
    kf = KeywordFilter(_cfg(keywords=["nonexistent keyword"], title_direction_hints=hints))

    for title in EN_TITLES + [
        "AI R&D Engineering Intern", "Data-driven Marketing Intern",
        "Aircraft Maintenance Trainee", "Retail Operations Assistant",
        "Quantitative Researcher Intern", "LLM Evaluation Intern",
        "Air Conditioning Technician", "Metadata Operations Assistant",
        "Email Campaign Assistant", "Risk and Compliance Intern",
        "Research Assistant, School of Management", "Fulfillment Operations Assistant",
    ]:
        old = next((h for h, p in legacy if p.search(title)), "")
        new = kf._match_direction(title)
        assert old == new, f"边界语义变了：{title}（老={old!r} 新={new!r}）"


# ---------------------------------------------------------------------------
# C. 英文匹配行为必须与加中文词之前完全一致
# ---------------------------------------------------------------------------
EN_TITLES = [
    "Data Analyst Intern",
    "Business Intelligence Intern",
    "Machine Learning Intern (Summer 2027)",
    "AI Engineer (Intern)",
    "Quantitative Analyst Intern",
    "Warehouse Supervisor",
    "Retail Sales Associate",
    "Customer Service Officer",
    "Fulfillment Operations Assistant",
    "Metadata Operations Assistant",
]


def test_english_matching_unchanged_after_adding_cjk_keywords():
    """同一批英文标题，加中文词前后：留下的岗位与匹配到的词都必须一样。

    这是「别把英文用户的口径改坏」的硬要求——中文词不能因为子串匹配的
    副作用去认领英文岗位。
    """
    before = KeywordFilter(_cfg(keywords=["data analyst", "business intelligence",
                                         "machine learning", "ai engineer", "quant"]))
    after = KeywordFilter(_cfg(keywords=["data analyst", "business intelligence",
                                        "machine learning", "ai engineer", "quant"]
                               + CJK_KEYWORDS))
    kept_before = [(j.title, j.keyword_matched) for j in before.filter(
        [_job(t) for t in EN_TITLES])]
    kept_after = [(j.title, j.keyword_matched) for j in after.filter(
        [_job(t) for t in EN_TITLES])]
    assert kept_before == kept_after, "加了中文关键词后英文岗位的匹配结果变了"


def test_cjk_keyword_never_reports_on_english_title():
    """更直接的一版：中文词不会在英文标题上产生命中记录。"""
    kf = KeywordFilter(_cfg(keywords=CJK_KEYWORDS))
    for title in EN_TITLES:
        assert kf.filter([_job(title)]) == [], f"中文词误命中英文标题：{title}"


# ---------------------------------------------------------------------------
# D. 大小写不敏感（走真实配置加载路径，load_config 统一 .lower()）
# ---------------------------------------------------------------------------
def _load_from_yaml(text: str) -> AppConfig:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text(text, encoding="utf-8")
        return load_config(p, require_runnable=False)


def test_case_insensitive_keywords():
    """写 DATA ANALYST 与 data analyst 等价——用户在教程里怎么写都不该影响结果。"""
    lower = _load_from_yaml('keywords:\n  - "data analyst"\n')
    upper = _load_from_yaml('keywords:\n  - "DATA ANALYST"\n')
    assert lower.keywords == upper.keywords == ["data analyst"]
    for title in ("Data Analyst Intern", "DATA ANALYST", "data analyst (part-time)"):
        a = KeywordFilter(lower).filter([_job(title)])
        b = KeywordFilter(upper).filter([_job(title)])
        assert bool(a) == bool(b) == True          # noqa: E712
        assert a[0].keyword_matched == b[0].keyword_matched == "data analyst"


def test_cjk_keywords_survive_config_loading():
    """中文词经过 load_config 的 .strip().lower() 后必须原样保留（lower 不动中文）。"""
    cfg = _load_from_yaml('keywords:\n  - " 數據分析 "\n  - "数据分析"\n'
                          'title_direction_hints:\n  - "分析員"\n')
    assert cfg.keywords == ["數據分析", "数据分析"]
    assert cfg.title_direction_hints == ["分析員"]
    assert KeywordFilter(cfg).filter([_job("數據分析員")])


def test_mixed_ascii_cjk_hint_matches_uppercase_title():
    """方向词里 ASCII + 汉字的混合写法必须大小写不敏感（2026-11-02 修的 bug）。

    配置加载时 title_direction_hints 会被统一 .lower()（「AI數據」→「ai數據」），
    而真实标题常写成「AI數據分析實習生」。早先这条分支编译正则时没带 re.I，
    于是混合词永远只命中全小写的标题——用户按文档写「AI工程師」就白填。
    同一份配置里 keywords 走 haystack.lower() 是好的，两条路径口径必须一致。
    """
    kf = KeywordFilter(_cfg(keywords=["nonexistent keyword"],
                            title_direction_hints=["ai數據", "bi分析員"]))
    for title in ["AI數據分析實習生", "ai數據分析實習生", "BI分析員", "bi分析員"]:
        assert len(kf.filter([_job(title)])) == 1, f"混合语言方向词没命中：{title}"
    # 纯中文方向词不能被这个改动带坏（无大小写概念，行为不变）
    kf2 = KeywordFilter(_cfg(keywords=["nonexistent keyword"],
                             title_direction_hints=["分析員"]))
    assert len(kf2.filter([_job("數據分析員")])) == 1


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
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
