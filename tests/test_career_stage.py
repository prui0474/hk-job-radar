"""求职阶段（career_stage）测试：岗位类型过滤是否随身份切换。

背景（为什么要有这个功能）：
原版把「只抓实习/兼职」写死在代码里——因为作者本人是在读学生、持学生签证，
全职违法。但本产品面向**所有**香港求职者，所以改成三档可选：

    student  → internship + part time      （学生签证的合规底线）
    graduate → full time（含管培生）        （已毕业 / 持 IANG）
    both     → 全都要                       （毕业前后过渡期）

这里钉住三件事：① 三档的映射正确；② 显式写 job_types 时不被覆盖（高级用户逃生口）；
③ 关键词初筛的「岗位类型安全网」真的按这三档放行/剔除。

同时兼容两种运行方式：
- pytest tests/test_career_stage.py
- python tests/test_career_stage.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import (
    CAREER_STAGE_JOB_TYPES,
    AppConfig,
    ConfigError,
    _normalize_career_stage,
    load_config,
)
from hk_job_radar.models import JobPosting
from hk_job_radar.scoring.keyword_filter import KeywordFilter


def _job(title: str) -> JobPosting:
    return JobPosting(source="test", external_id=title, title=title, company="Test Co",
                      url=f"https://example.com/{title}",
                      url_normalized=f"https://example.com/{title}",
                      description_snippet="")


def _load_from_yaml(text: str) -> AppConfig:
    """把一段 YAML 写成临时 config.yaml 再加载（验证的是真实的解析路径）。"""
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_normalize_accepts_english_and_chinese():
    assert _normalize_career_stage("student") == "student"
    assert _normalize_career_stage("graduate") == "graduate"
    assert _normalize_career_stage("both") == "both"
    assert _normalize_career_stage("在读学生") == "student"
    assert _normalize_career_stage("已毕业") == "graduate"
    # 写错 / 没写 → 落到最保守的 student（宁可少抓，不可让在读生收到违法全职岗）
    assert _normalize_career_stage("") == "student"
    assert _normalize_career_stage("whatever") == "student"


def test_stage_mapping_table():
    assert CAREER_STAGE_JOB_TYPES["student"] == ["internship", "part time"]
    assert CAREER_STAGE_JOB_TYPES["graduate"] == ["full time"]
    assert CAREER_STAGE_JOB_TYPES["both"] == ["internship", "part time", "full time"]


def test_job_types_derived_from_career_stage():
    cfg = _load_from_yaml("keywords:\n  - \"data analyst\"\ncareer_stage: graduate\n")
    assert cfg.career_stage == "graduate"
    assert cfg.job_types == ["full time"]


def test_explicit_job_types_wins_over_career_stage():
    """高级用户的逃生口：手写 job_types 时以他写的为准，不被 career_stage 覆盖。"""
    cfg = _load_from_yaml(
        "keywords:\n  - \"data analyst\"\n"
        "career_stage: student\n"
        "job_types:\n  - \"internship\"\n  - \"full time\"\n")
    assert cfg.career_stage == "student"
    assert cfg.job_types == ["internship", "full time"]


def test_default_config_behaves_like_before():
    """老配置（没有任何新字段）必须保持原行为：student + 实习/兼职。"""
    cfg = _load_from_yaml("keywords:\n  - \"product\"\n")
    assert cfg.career_stage == "student"
    assert cfg.job_types == ["internship", "part time"]


def test_allowed_classes_always_keep_unmarked_and_graduate_programme():
    """未标注类型的岗位交给 LLM 判定；毕业生项目（管培生）任何时候都放行。"""
    for stage in ("student", "graduate", "both"):
        cfg = AppConfig(career_stage=stage, job_types=CAREER_STAGE_JOB_TYPES[stage])
        allowed = cfg.allowed_job_type_classes()
        assert "" in allowed
        assert "graduate programme" in allowed


def test_filter_respects_each_stage():
    titles = ["Full-time Product Designer", "Product Design Intern",
              "Management Trainee (Product)", "Product Analyst"]

    def kept(stage: str) -> set[str]:
        cfg = AppConfig(keywords=["product"], keyword_synonyms={},
                        career_stage=stage, job_types=CAREER_STAGE_JOB_TYPES[stage])
        return {j.title for j in KeywordFilter(cfg).filter([_job(t) for t in titles])}

    # student：剔除普通全职，保留实习与「无类型标记」的岗位
    assert kept("student") == {"Product Design Intern", "Management Trainee (Product)",
                               "Product Analyst"}
    # graduate：反向——剔除实习，让全职进来
    assert kept("graduate") == {"Full-time Product Designer", "Management Trainee (Product)",
                                "Product Analyst"}
    # both：全放行
    assert kept("both") == set(titles)


def test_graduate_programme_keyword_uses_fulltime_filter():
    """管培生词即使写在 keywords（而不是 graduate_keywords）里，也要用全职过滤。"""
    cfg = AppConfig(keywords=["management trainee", "data analyst"],
                    job_types=["internship", "part time"])
    assert cfg.job_types_for_keyword("management trainee") == ["full time"]
    assert cfg.job_types_for_keyword("data analyst") == ["internship", "part time"]


def test_unknown_channel_name_is_rejected():
    """通道名写错 → 启动即报错，而不是"配了却收不到"的无声故障。"""
    try:
        _load_from_yaml("keywords:\n  - \"data analyst\"\n"
                        "notify:\n  channels:\n    - \"wechat\"\n")
    except ConfigError as exc:
        assert "wechat" in str(exc)
    else:
        raise AssertionError("未知通道名应当抛出 ConfigError")


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
