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


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
