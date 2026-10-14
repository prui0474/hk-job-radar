"""岗位类型硬过滤（安全网）单元测试：纯字符串匹配，不依赖网络。

测试数据刻意使用【通用的产品/市场岗位名】，不含任何特定人的求职方向。

同时兼容两种运行方式：
- pytest tests/test_job_type_filter.py
- python tests/test_job_type_filter.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig
from hk_job_radar.models import JobPosting
from hk_job_radar.scoring.keyword_filter import KeywordFilter


def _job(title: str, snippet: str = "") -> JobPosting:
    """构造最小 JobPosting：所有测试岗位标题都含 'product'，保证通过关键词初筛。"""
    return JobPosting(
        source="test",
        external_id=title,
        title=title,
        company="Test Co",
        url=f"https://example.com/{title}",
        url_normalized=f"https://example.com/{title}",
        description_snippet=snippet,
    )


def test_classify_internship():
    classify = KeywordFilter.classify_job_type
    for text in [
        "Product Design Intern",
        "Marketing Analyst Internship (Summer 2026)",
        "产品设计实习生",
        "Product 实习",
        "Co-op Product Engineer",
        "Product coop role",
    ]:
        assert classify(text) == "internship", text


def test_classify_parttime():
    classify = KeywordFilter.classify_job_type
    for text in [
        "Part-time Product Analyst",
        "Part time product designer",
        "Parttime product assistant",
        "兼职产品分析员",
        "兼職產品分析師",
    ]:
        assert classify(text) == "part time", text


def test_classify_fulltime():
    classify = KeywordFilter.classify_job_type
    for text in [
        "Full-time Product Designer",
        "Full time product analyst",
        "Fulltime product engineer",
        "全职产品设计师",
        "全職產品設計師",
        "Permanent Product Engineer",
    ]:
        assert classify(text) == "full time", text


def test_classify_unknown():
    classify = KeywordFilter.classify_job_type
    for text in ["Product Analyst", "Product Researcher", "", "Product Analyst Trainee"]:
        assert classify(text) == "", repr(text)


def test_graduate_programmes_are_allowed():
    # 毕业生培训计划（银行管培生等）是「毕业后入职」的全职岗，应放行，而不是当全职剔除
    classify = KeywordFilter.classify_job_type
    for text in [
        "Full-time Management Trainee",
        "Management Trainee (Product)",
        "Graduate Trainee (Finance)",
        "Graduate Programme Product Analyst",
        "Graduate Program Product Designer",
        "Management Associate",
        "银行管培生",
    ]:
        assert classify(text) == "graduate programme", text


def test_fulltime_still_dropped():
    # 普通全职岗（非管培生/非实习/非兼职）仍应判定为全职
    classify = KeywordFilter.classify_job_type
    for text in [
        "Full-time Product Designer",
        "Full time product analyst",
        "Fulltime product engineer",
        "全职产品设计师",
        "全職產品設計師",
        "Permanent Product Engineer",
    ]:
        assert classify(text) == "full time", text


def test_intern_not_false_positive_on_internal():
    # 词边界防误伤：internal/international 不应被判定成实习
    classify = KeywordFilter.classify_job_type
    assert classify("Internal Auditor") == ""
    assert classify("International Product Analyst") == ""


def test_intern_and_parttime_win_over_fulltime():
    # "Full-time Intern" 仍是实习（合法）、"Permanent Part-time" 仍是兼职，都不能被误杀
    assert KeywordFilter.classify_job_type("Full-time Product Intern") == "internship"
    assert KeywordFilter.classify_job_type("Permanent Part-time Product Assistant") == "part time"


def test_filter_drops_fulltime_only():
    cfg = AppConfig(keywords=["product"], keyword_synonyms={})
    f = KeywordFilter(cfg)
    jobs = [
        _job("Full-time Product Designer"),
        _job("Product Design Intern"),
        _job("Part-time Product Analyst"),
        _job("Product Analyst"),          # 无类型标记 → 保留，交 LLM 判定
    ]
    kept = f.filter(jobs)
    titles = {j.title for j in kept}
    assert "Full-time Product Designer" not in titles
    assert titles == {"Product Design Intern", "Part-time Product Analyst", "Product Analyst"}


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
