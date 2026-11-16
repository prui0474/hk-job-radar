"""岗位类型策略（strict / daily_only / 排除分类）单元测试：不依赖网络。

这些用例守的是 2026-10-25 加的那层策略，背景是：招聘站的 URL 类型过滤
并不可靠（JobsDB 是「包含」语义、Indeed/LinkedIn 新版列表页连摘要都不给），
所以「判不出类型」不能按「大概没事」处理——持学生签证在读期间做全职是违法的。

★ 关键反向守护：默认配置（lenient）必须与加这层之前**完全一致**。
  开源用户不该因为这次改动而少看到任何一条岗位。

兼容两种运行方式：
- pytest tests/test_job_type_policy.py
- python tests/test_job_type_policy.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig  # noqa: E402
from hk_job_radar.models import JobPosting  # noqa: E402
from hk_job_radar.pipeline import Pipeline  # noqa: E402
from hk_job_radar.scoring.keyword_filter import KeywordFilter  # noqa: E402


def _job(title: str, snippet: str = "", site_type: str = "") -> JobPosting:
    return JobPosting(
        source="test", external_id=title[:24], title=title, company="Test Co",
        url=f"https://example.com/{title}", url_normalized=f"https://example.com/{title}",
        description_snippet=snippet, job_type=site_type,
    )


def _cfg(**kw) -> AppConfig:
    """默认参数与开源默认一致：lenient、不排除任何分类。"""
    base = dict(keywords=["data", "product", "analyst", "manager", "intern", "quant",
                          "business", "assistant", "research"], keyword_synonyms={})
    base.update(kw)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# 默认行为不变（反向守护）
# ---------------------------------------------------------------------------
def test_default_policy_is_lenient_and_keeps_unknown():
    cfg = _cfg()
    assert cfg.job_type_policy == "lenient"
    assert cfg.unknown_job_type_action == "daily_only"
    assert cfg.exclude_job_type_classes == []
    # 判不出类型的产品岗必须照旧放行
    kept = {j.title for j in KeywordFilter(cfg).filter([_job("Product Analyst")])}
    assert kept == {"Product Analyst"}


def test_lenient_never_defers_realtime():
    cfg = _cfg()
    fake = SimpleNamespace(config=cfg)
    assert Pipeline._defer_unknown_type(fake, _job("Product Analyst")) is False


# ---------------------------------------------------------------------------
# 销售陷阱：包装成 Trainee 的保险/理财岗
# ---------------------------------------------------------------------------
def test_wealth_management_trainee_is_sales_trap():
    """这类岗位标题带 Trainee，但实为全职销售，必须判成 full time 而不是实习。"""
    for title in [
        "Finance/Wealth Management Graduate Trainee 財務規劃顧問",
        "Graduate Trainee – Private Wealth Management",
        "Management Trainee, Wealth Management",
        "理財顧問 Management Trainee",
    ]:
        assert KeywordFilter.classify_posting(_job(title)) == "full time", title


def test_plain_insurance_data_role_is_not_sales():
    """保险公司的数据分析岗是正常岗位，不能因为公司行业被误杀。"""
    assert KeywordFilter.classify_posting(_job("Insurance Data Analyst")) == ""
    assert KeywordFilter.classify_posting(_job("Data Analyst, AIA Insurance")) == ""


# ---------------------------------------------------------------------------
# 职级词只作用于标题，且不能压过实习信号
# ---------------------------------------------------------------------------
def test_seniority_only_in_title():
    assert KeywordFilter.classify_posting(_job("Senior Data Scientist")) == "full time"
    assert KeywordFilter.classify_posting(_job("Quantitative Portfolio Manager")) == "full time"
    # 正文里出现 Senior 不算——"report to the Senior Manager" 到处都是
    assert KeywordFilter.classify_posting(
        _job("Data Analyst", "You will report to the Senior Manager of Analytics")) == ""


def test_intern_wins_over_seniority_and_sales_words():
    """"Project Manager Intern" 仍是实习；实习信号优先级最高。"""
    assert KeywordFilter.classify_posting(_job("Project Manager Intern")) == "internship"
    assert KeywordFilter.classify_posting(_job("Wealth Management Intern")) == "internship"


# ---------------------------------------------------------------------------
# 站点标签优先于标题推断（但「实习」是唯一例外，见下一条）
# ---------------------------------------------------------------------------
def test_site_label_wins_over_title():
    # 标题里没有 Part-time 字样，但卡片标了 Part time
    assert KeywordFilter.classify_posting(
        _job("Business Intelligence Analyst", site_type="part time")) == "part time"
    # 标题看着中性，站点说合同制——以站点为准（结束日只有站点知道）
    assert KeywordFilter.classify_posting(
        _job("Data Analyst", site_type="contract")) == "contract"


def test_intern_title_beats_fulltime_site_label():
    """★ 2026-10-25 起：「实习」可以压过站点的 Full time 标签。

    背景：JobsDB 把**全职时长的实习**也标成 Full time（卡片上就印
    "This is a Full time job"）。在修好卡片标签提取之前，job_type 一直是空的，
    这个冲突不显现；修好之后若仍让站点无条件优先，Data Analyst Intern
    这类岗位会被整批误杀成全职。

    注意实习标记用的是词边界 `\\binterns?\\b`，Internal Audit 之类不会被误命中。
    """
    assert KeywordFilter.classify_posting(
        _job("Data Analyst Intern", site_type="full time")) == "internship"
    assert KeywordFilter.classify_posting(
        _job("Wealth Management Internship", site_type="full time")) == "internship"
    # 不含实习词的全职岗仍按站点走
    assert KeywordFilter.classify_posting(
        _job("Data Analyst", site_type="full time")) == "full time"


# ---------------------------------------------------------------------------
# strict 策略：不可确认的岗位不推实时推送，但保留进日报
# ---------------------------------------------------------------------------
def _strict_cfg(**kw) -> AppConfig:
    return _cfg(job_type_policy="strict", unknown_job_type_action="daily_only", **kw)


def test_strict_daily_only_defers_but_keeps():
    cfg = _strict_cfg()
    fake = SimpleNamespace(config=cfg)
    job = _job("Junior Quantitative Analyst, Risk Management")
    # 判不出类型 → 暂缓实时推送
    assert Pipeline._defer_unknown_type(fake, job) is True
    # 但初筛要放行（否则连日报都进不去，就变成 drop 了）
    assert {j.title for j in KeywordFilter(cfg).filter([job])} == {job.title}
    # 明确的实习不受影响
    assert Pipeline._defer_unknown_type(fake, _job("Data Analyst Intern")) is False


def test_strict_drop_removes_unknown_at_filter_stage():
    cfg = _cfg(job_type_policy="strict", unknown_job_type_action="drop")
    fake = SimpleNamespace(config=cfg)
    job = _job("Junior Quantitative Analyst, Risk Management")
    # drop 模式下初筛就丢掉，根本不会出现在 fetched 结果里
    assert KeywordFilter(cfg).filter([job]) == []
    # 推送环节只管 daily_only，drop 不归它管，所以返回 False 是正确的
    assert Pipeline._defer_unknown_type(fake, job) is False


# ---------------------------------------------------------------------------
# 额外排除分类（不要管培生）
# ---------------------------------------------------------------------------
def test_exclude_graduate_programme():
    title = "Management Trainee (Data)"
    # 默认：管培生照旧放行（开源行为不变）
    assert {j.title for j in KeywordFilter(_cfg()).filter([_job(title)])} == {title}
    # 显式排除后：挡住
    cfg = _cfg(exclude_job_type_classes=["graduate programme"])
    assert KeywordFilter(cfg).filter([_job(title)]) == []


def test_sales_trainee_without_wealth_words_still_excluded():
    """标题里没写「财富管理」的销售话术岗判不成销售陷阱，但会落进 graduate
    programme 分类——所以排除管培生时同样会被挡掉。这是刻意的兜底：
    只靠标题认不出「是不是保险销售」，那就把整类培训生一起挡在门外。
    """
    title = "Fresh Graduate Trainee (18K+ 5 days work, full training)"
    assert KeywordFilter.classify_posting(_job(title)) == "graduate programme"
    cfg = _cfg(exclude_job_type_classes=["graduate programme"])
    assert KeywordFilter(cfg).filter([_job(title)]) == []


def test_excluded_class_does_not_affect_internships():
    cfg = _cfg(exclude_job_type_classes=["graduate programme"], job_type_policy="strict")
    kept = {j.title for j in KeywordFilter(cfg).filter(
        [_job("Data Analyst Intern"), _job("Management Trainee (Data)")])}
    assert kept == {"Data Analyst Intern"}


# ---------------------------------------------------------------------------
# 类型判定结果要写回对象（入库与日报都靠它）
# ---------------------------------------------------------------------------
def test_filter_writes_back_job_type():
    cfg = _strict_cfg()
    jobs = [_job("Data Analyst Intern"), _job("Senior Data Analyst")]
    KeywordFilter(cfg).filter(jobs)
    assert jobs[0].job_type == "internship"
    assert jobs[1].job_type == "full time"


# ---------------------------------------------------------------------------
# 合同制 / 临时岗（2026-10-25 加）
#
# 背景：入境处 2026-05-03 签发的 NOL 属于「暂免安排」版，没有每周 20 小时限制，
# 所以在读期间做合同制岗位同样合规。真机实测 JobsDB 的 worktype=244 池里
# 90/90 张卡片都标 "Contract/Temp"，且 Junior / Open to Fresh Graduates /
# 6 months contract 的比例明显高于全职池。
#
# ★ 反向守护（本组最重要的一条）：开 contract 绝不等于开 full time。
#   JobsDB 会把部分岗位（甚至 1 年期合同岗）标成 "Full time"，那类必须继续被挡。
# ---------------------------------------------------------------------------
def test_contract_in_job_types_allows_contract_class():
    cfg = _cfg(job_types=["internship", "part time", "contract"])
    allowed = cfg.allowed_job_type_classes()
    assert "contract" in allowed
    assert "temporary" in allowed          # 站点标签两种写法都要通


def test_contract_postings_pass_filter():
    """站点标 Contract/Temp 的岗位必须能过安全网，否则加 contract 等于白抓一笑。"""
    cfg = _cfg(job_types=["internship", "contract"], job_type_policy="strict")
    jobs = [_job("Junior Business Analyst", site_type="contract"),
            _job("Data Analyst (1-Year Contract)", site_type="contract")]
    assert len(KeywordFilter(cfg).filter(jobs)) == 2


def test_full_time_still_blocked_when_contract_enabled():
    """开了 contract 之后，站点标 Full time 的岗位仍必须被剔除。"""
    cfg = _cfg(job_types=["internship", "part time", "contract"], job_type_policy="strict")
    assert KeywordFilter(cfg).filter([_job("Data Analyst", site_type="full time")]) == []


def test_casual_not_auto_allowed_by_contract():
    """casual 对应 JobsDB 的 245 桶（散工 / 暑期工），语义与「有结束日的合同制」
    不同，不该被 contract 顺带放行。"""
    cfg = _cfg(job_types=["internship", "contract"])
    assert "casual" not in cfg.allowed_job_type_classes()


def test_site_label_contract_temp_normalizes_to_contract():
    from hk_job_radar.scrapers.job_types import extract_site_job_type
    assert extract_site_job_type("Contract/Temp") == "contract"
    assert extract_site_job_type("This is a Contract/Temp job") == "contract"


# ---------------------------------------------------------------------------
# 早期职业 / 兼职 / 毕业生项目 压过站点的 Full time 标签（2026-11-16 加）
#
# 背景：Workday 的 timeType 只表达工时长短，不表达雇佣性质。实测 191 条 Workday
# 岗位的 job_type 100% 是 "full time"，其中包含 "Co-op Internship 2027" 和 13 条
# "(Contract)" / "(1-year contract)"。旧规则只在标题出现 intern 类词时豁免
# full time 标签，于是下面这些真实的实习/兼职岗被整批误杀。
#
# ★ 反向守护（本组最后两条）：豁免必须只认「早期职业证据」，不能把普通全职、
#   暑期校招全职（Summer Full-time Analyst）或理财销售培训生一起放进来。
# ---------------------------------------------------------------------------
_EARLY_CAREER_TITLES = [
    "Data Analyst Intern",
    "Summer Intern - Data Science",
    "數據分析實習生",
    "Summer Analyst",                     # 投行暑期实习的标准命名
    "Summer Associate",
    "Data Science Placement Student",
    "Industrial Placement",
    "Placement Year Student",
    "Working Student - Data",
    "Student Helper",
    "Student Assistant",
    "Apprentice Data Analyst",
    "Off-cycle Analyst",
    "Traineeship",
]

_PARTTIME_TITLES = [
    "Part-time Data Analyst",
    "Data Analyst (Part Time)",
    "兼職數據分析員",
    "数据分析师（兼职）",
]

_GRADUATE_TITLES = [
    "FLAME Management Trainee Program",   # 实测被站点标 Full time 的 Sun Life 毕业生项目
    "Graduate Trainee",
    "Management Trainee",
]


def test_early_career_titles_classified_as_internship():
    """两种来源都要判成实习：卡片没标类型、卡片标了 Full time。"""
    for title in _EARLY_CAREER_TITLES:
        assert KeywordFilter.classify_posting(_job(title)) == "internship", title
        assert KeywordFilter.classify_posting(
            _job(title, site_type="full time")) == "internship", title


def test_parttime_titles_classified_as_parttime():
    for title in _PARTTIME_TITLES:
        assert KeywordFilter.classify_posting(_job(title)) == "part time", title
        assert KeywordFilter.classify_posting(
            _job(title, site_type="full time")) == "part time", title


def test_graduate_programmes_classified_against_fulltime_label():
    """毕业生项目必须走到 graduate programme 分支，而不是被 full time 标签早退掉。

    这是「配置说允许、代码判不出」的那个 bug：allowed_job_type_classes() 无条件
    放行 graduate programme，但如果 classify_posting 早退返回 full time，
    这个放行永远不生效。
    """
    for title in _GRADUATE_TITLES:
        assert KeywordFilter.classify_posting(_job(title)) == "graduate programme", title
        assert KeywordFilter.classify_posting(
            _job(title, site_type="full time")) == "graduate programme", title


def test_graduate_programme_excluded_by_config_not_by_fulltime_early_return():
    """用户配置排除 graduate programme 时结果是被排除——但要能证明走的是
    graduate programme 分支（job_type 写回的值就是证据）。"""
    cfg = _cfg(exclude_job_type_classes=["graduate programme"])
    for title in _GRADUATE_TITLES:
        job = _job(title, site_type="full time")
        assert KeywordFilter(cfg).filter([job]) == [], title
        assert job.job_type == "graduate programme", title


def test_body_evidence_overrides_fulltime_site_label():
    """标题没写，但 JD 里写明实习/兼职 → 也要救回来（匹配范围扩到正文）。"""
    assert KeywordFilter.classify_posting(_job(
        "Data Analyst", "6-month internship, 20 hours per week",
        site_type="full time")) == "internship"
    assert KeywordFilter.classify_posting(_job(
        "Business Intelligence Analyst", "This is a part-time role, 3 days per week",
        site_type="full time")) == "part time"


def test_body_evidence_does_not_override_seniority_in_title():
    """正文里的 Senior 仍然不算数：职级词只看标题这条没被放宽。"""
    assert KeywordFilter.classify_posting(_job(
        "Data Analyst", "You will report to the Senior Manager of Analytics")) == ""


def test_sales_trap_not_rescued_by_fulltime_exemption():
    """理财/保险销售话术岗带 Trainee，但它是真全职销售，不能被毕业生项目豁免放进来。"""
    for title in ["Management Trainee, Wealth Management",
                  "理財顧問 Management Trainee",
                  "Graduate Trainee – Private Wealth Management"]:
        assert KeywordFilter.classify_posting(
            _job(title, site_type="full time")) == "full time", title


def test_title_contract_marker_beats_fulltime_site_label():
    """标题写着 "(1-year contract)" / "(6 Months)" 的岗位：站点标 Full time，
    但按有结束日的合同制记 contract（是否放行仍由 config 决定）。"""
    for title in ["Data Analyst (1-year contract)", "Data Analyst (6 Months)",
                  "Data Analyst (12 months contract)"]:
        assert KeywordFilter.classify_posting(
            _job(title, site_type="full time")) == "contract", title


def test_title_contract_marker_not_used_without_site_label():
    """无站点标签时不要把 "(1-year contract)" 判成 contract：
    默认 student 配置只允许 internship/part time，判成 contract 会把现在
    「类型不可确认→保留」的岗位变成剔除，属于新增误杀。"""
    for title in ["Data Analyst (1-year contract)", "Data Analyst (6 Months)"]:
        assert KeywordFilter.classify_posting(_job(title)) == "", title


def test_fulltime_gate_not_weakened():
    """★ 反向守护：这次改动不能把全职闸门拆掉。"""
    cfg = _cfg(keywords=["data", "analyst", "manager", "summer"])
    for title in ["Data Analyst", "Senior Data Analyst", "Branch Manager",
                  "Relationship Manager", "Summer Full-time Analyst"]:
        job = _job(title, site_type="full time")
        assert KeywordFilter.classify_posting(job) == "full time", title
        assert KeywordFilter(cfg).filter([job]) == [], title
    # 无站点标签时：普通岗仍不可确认（保留交 LLM），带职级词/全职词仍是全职
    assert KeywordFilter.classify_posting(_job("Data Analyst")) == ""
    assert KeywordFilter.classify_posting(_job("Senior Data Analyst")) == "full time"
    assert KeywordFilter.classify_posting(_job("Summer Full-time Analyst")) == "full time"


def test_student_stage_keeps_early_career_and_parttime():
    """端到端：默认 student 配置下，这些被站点标 Full time 的真实实习/兼职岗
    必须全部留在结果里。"""
    cfg = _cfg(keywords=["data", "analyst", "intern", "part", "summer", "student"])
    titles = ["Data Analyst Intern", "Summer Analyst", "Part-time Data Analyst",
              "Student Helper"]
    jobs = [_job(t, site_type="full time") for t in titles]
    kept = {j.title for j in KeywordFilter(cfg).filter(jobs)}
    assert kept == set(titles)


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
