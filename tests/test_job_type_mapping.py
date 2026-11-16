"""岗位类型 → 各站 URL 参数映射 与 config.job_types 读取 的单元测试。

纯函数/本地文件测试，不依赖网络。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import load_config
from hk_job_radar.scrapers.job_types import (
    build_indeed_sc,
    build_jobsdb_worktype,
    build_linkedin_f_jt,
    extract_site_job_type,
    normalize_job_types,
)


def test_normalize():
    assert normalize_job_types(
        ["part-time", "PARTTIME", "Intern", "part time"]) == ["part time", "internship"]
    assert normalize_job_types(["full-time"]) == ["full time"]
    assert normalize_job_types([]) == []


def test_linkedin():
    assert build_linkedin_f_jt(["internship", "part time"]) == "I%2CP"
    assert build_linkedin_f_jt(["internship"]) == "I"
    assert build_linkedin_f_jt([]) == ""


def test_indeed():
    assert build_indeed_sc(["internship", "part time"]) == \
        "sc=0kf%3Ajt%28internship%2Cparttime%29%3B"
    assert build_indeed_sc(["part time"]) == "sc=0kf%3Ajt%28parttime%29%3B"
    assert build_indeed_sc([]) == ""


def test_jobsdb():
    assert build_jobsdb_worktype(["internship", "part time"]) == "243%2C245"
    assert build_jobsdb_worktype(["part time"]) == "243"
    assert build_jobsdb_worktype([]) == ""


def test_config_reads_job_types():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text(
            "keywords: [\"product analyst\"]\n"
            "job_types: [\" Internship \", \"Part-Time\", \"\"]\n",
            encoding="utf-8",
        )
        cfg = load_config(p)
    # load_config 只做小写/去空白/去空项（同 keywords 口径），别名归一化交给 job_types 模块
    assert cfg.job_types == ["internship", "part-time"]


def test_site_label_recognizes_more_spellings():
    """站点标签的更多写法（Workday/JobsDB 实测出现或可能出现的）。"""
    cases = {
        "Fixed Term": "contract",
        "Fixed-Term": "contract",
        "Temporary/Contract": "contract",     # 顺序与 contract/temp 相反
        "Contract / Temp": "contract",        # 斜杠两侧带空格
        "This is a Temporary/Contract job": "contract",
        "Traineeship": "internship",
        "兼職": "part time",
        "兼职": "part time",
        "全職": "full time",
        "全职": "full time",
    }
    for text, expected in cases.items():
        assert extract_site_job_type(text) == expected, text


def test_fulltime_and_parttime_both_offered_counts_as_parttime():
    """「Full Time / Part Time」= 站点明说可以兼职 → 归兼职侧（在读学生可投）。"""
    for text in ["Full Time / Part Time", "Full Time/Part Time",
                 "Full-Time/Part-Time", "This is a Full Time / Part Time job"]:
        assert extract_site_job_type(text) == "part time", text


def test_seasonal_label_normalizes_to_casual():
    assert extract_site_job_type("Seasonal") == "casual"


def test_label_matching_stays_line_based():
    """★ 整行匹配的现有设计不能破坏：标题本身写着 Part-time 不是站点标签。"""
    assert extract_site_job_type("Part-time Data Analyst") == ""
    assert extract_site_job_type("Data Analyst\nFixed Term") == "contract"
    # 一行里混了标题与标签就不算标签（宁可提取不到，也不能误读）
    assert extract_site_job_type("Data Analyst Full time") == ""


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
