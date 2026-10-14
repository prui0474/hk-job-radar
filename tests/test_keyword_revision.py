"""关键词初筛 / 关键词轮转 / 增量时间窗 的回归测试（不依赖任何个人配置）。

为什么刻意【不读真实的 config.yaml】：
本文件守护的是「机制」—— 子串匹配的优先级、同义词的归属、轮转的数学、
时间窗随停机时长放宽。机制与"某个人往里面填了哪些关键词"无关，所以全部用
显式构造的配置来测。

这样做的两个好处：
1. 任何人 clone 本仓库、把 config.example.yaml 改成自己的求职方向之后，
   这个测试文件依然全过（不会因为关键词与作者不同而失败）；
2. 仓库里不会因为测试代码而暴露作者本人的求职方向。

针对"我自己那份 config.yaml 是否合理"的检查（关键词总数、有没有误加短词等）
另放在 tests/test_personal_config.py —— 那个文件绑定个人方向，已加入 .gitignore。

同时兼容两种运行方式：
- pytest tests/test_keyword_revision.py
- python tests/test_keyword_revision.py（项目 venv 直接运行）
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar.config import AppConfig
from hk_job_radar.models import JobPosting, now_hk
from hk_job_radar.pipeline import Pipeline
from hk_job_radar.scoring.keyword_filter import KeywordFilter
from hk_job_radar.scrapers import pages_per_keyword


class _FakeDB:
    """最小 kv 状态桩：只实现 pipeline 用到的两个方法。"""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


def _cfg(**overrides) -> AppConfig:
    """构造一个自包含的测试配置（默认值只是测试样例，不代表任何人的求职方向）。"""
    base = dict(
        keywords=["product design", "growth marketing", "supply chain"],
        graduate_keywords=["management trainee"],
        keyword_synonyms={},
        job_types=["internship", "part time"],
        daily_limits={"jobsdb": 150, "linkedin": 40, "indeed": 40},
        poll_interval_minutes=30,      # → 一天 48 轮
        max_job_age_days=2,
    )
    base.update(overrides)
    return AppConfig(**base)


def _pipeline(cfg: AppConfig) -> Pipeline:
    """只装配 _keyword_window / _incremental_window_minutes 需要的两个属性。"""
    pipe = Pipeline.__new__(Pipeline)     # 绕过 __init__（不需要浏览器/数据库）
    pipe.config = cfg
    pipe.db = _FakeDB()
    return pipe


def _job(title: str, snippet: str = "") -> JobPosting:
    return JobPosting(
        source="test",
        external_id=title,
        title=title,
        company="Test Co",
        url=f"https://example.com/{title}",
        url_normalized=f"https://example.com/{title}",
        description_snippet=snippet,
    )


# ---------------------------------------------------------------------------
# 1. 子串匹配机制
# ---------------------------------------------------------------------------
def test_intern_style_titles_are_matched():
    """机制：标题写成「职位名 + Intern」时，子串匹配应当命中主关键词。

    这是最容易漏的一类岗位——如果关键词表里只写了「职位名词的完整形态」
    （如 product designer），"Product Design Intern" 这类标题就抓不到，
    因为两者并不互为子串。
    """
    f = KeywordFilter(_cfg())
    for title in ["Product Design Intern", "Growth Marketing Internship (Summer)",
                  "Supply Chain Intern", "Supply Chain Co-op"]:
        assert f.filter([_job(title)]), f"应当命中却被丢弃：{title}"


def test_unrelated_job_is_dropped():
    """机制：完全无关的岗位必须被初筛挡掉（这一层是免费的噪音过滤器）。"""
    f = KeywordFilter(_cfg())
    for title in ["Warehouse Supervisor", "Retail Sales Associate"]:
        assert not f.filter([_job(title)]), f"不该放行：{title}"


def test_specific_keyword_reported_first():
    """机制：`_match()` 取「列表中第一个命中的词」，所以更具体的词要排在前面。

    这里 "product design" 恰好是 "product designer" 的子串：若把两者顺序写反，
    "Product Designer" 会被记成命中 "product design"（不准确）。
    """
    f = KeywordFilter(_cfg(keywords=["product designer", "product design"]))
    assert f._match("product designer (summer intern)") == "product designer"
    assert f._match("product design intern") == "product design"


def test_snippet_also_matches():
    """机制：匹配范围是「标题 + 摘要」，摘要里命中也算。"""
    f = KeywordFilter(_cfg(keywords=["product design"]))
    assert f.filter([_job("Summer Analyst", "join our product design team in Hong Kong")])
    assert not f.filter([_job("Summer Analyst", "generalist programme")])


def test_synonym_maps_to_main_keyword():
    cfg = _cfg(keywords=["product design"],
               keyword_synonyms={"product design": ["ux design"]})
    f = KeywordFilter(cfg)
    job = _job("UX Design Intern")
    assert f.filter([job])
    assert "product design" in job.keyword_matched      # 归到主关键词名下


def test_orphan_synonym_is_ignored():
    """机制：主关键词不在 keywords 列表里时，它的同义词必须静默失效（不再匹配）。"""
    cfg = _cfg(keywords=["product design"],
               keyword_synonyms={"retired role": ["legacy title"]})
    assert not KeywordFilter(cfg).filter([_job("Legacy Title Intern")])


def test_graduate_keyword_uses_fulltime_filter():
    """毕业生项目关键词走「全职」类型的 URL 过滤，而不是 job_types。"""
    cfg = _cfg(keywords=["product design"], graduate_keywords=["management trainee"])
    assert cfg.job_types_for_keyword("management trainee") == ["full time"]
    assert cfg.job_types_for_keyword("product design") == cfg.job_types
    # 岗位类型安全网要放行管培生，而不是当全职剔除
    assert KeywordFilter.classify_job_type("Management Trainee 2027") == "graduate programme"


# ---------------------------------------------------------------------------
# 2. 关键词轮转（每轮抓几个关键词）
# ---------------------------------------------------------------------------
def test_rotation_auto_math():
    """auto = 每日页数上限 ÷ 每日轮数（轮询 30 分钟 → 一天 48 轮）。"""
    cfg = _cfg(keyword_rotation_per_cycle="auto")
    assert cfg.keywords_per_cycle_for_source("jobsdb") == 3      # 150 ÷ 48 = 3
    assert cfg.keywords_per_cycle_for_source("linkedin") == 1    # 40 ÷ 48 = 0 → 兜底 1
    assert cfg.keywords_per_cycle_for_source("indeed") == 1


def test_rotation_all_means_no_rotation():
    """all → 返回 0，代表"每轮抓全部关键词"（不轮转）。"""
    assert _cfg(keyword_rotation_per_cycle="all").keywords_per_cycle_for_source("jobsdb") == 0


def test_rotation_fixed_number():
    """per_cycle 写成数字 N → 每轮固定抓 N 个（N 需小于关键词总数才轮转得起来）。"""
    cfg = _cfg(keywords=[f"role {i}" for i in range(8)], graduate_keywords=[],
               keyword_rotation_per_cycle="5")
    assert cfg.keywords_per_cycle_for_source("jobsdb") == 5


def test_rotation_per_source_overrides_global():
    cfg = _cfg(keyword_rotation_per_cycle="all",
               keyword_rotation_per_source={"linkedin": 3})
    assert cfg.keywords_per_cycle_for_source("jobsdb") == 0      # 用全局值 all
    assert cfg.keywords_per_cycle_for_source("linkedin") == 3    # 被 per_source 覆盖


def test_rotation_never_exceeds_keyword_total():
    """每轮固定的个数大于关键词总数时，应当退化为"全抓"，而不是空转。"""
    cfg = _cfg(keywords=["a role"], graduate_keywords=[], keyword_rotation_per_cycle="9")
    assert cfg.keywords_per_cycle_for_source("jobsdb") == 0


def test_rotation_covers_every_keyword():
    """轮转必须能覆盖到每一个关键词——否则某些词会被永久饿死，比不轮转还糟。"""
    keywords = [f"role {i}" for i in range(20)]
    cfg = _cfg(keywords=keywords, graduate_keywords=[],
               keyword_rotation_per_cycle="all",
               keyword_rotation_per_source={"linkedin": 3})
    pipe = _pipeline(cfg)
    total = len(cfg.all_keywords())
    seen: set[str] = set()
    rounds = 0
    while len(seen) < total and rounds <= total:
        seen |= set(pipe._keyword_window("linkedin"))     # 窗口最小的那个站
        rounds += 1
    assert seen == set(cfg.all_keywords()), f"轮转没覆盖全，缺：{set(cfg.all_keywords()) - seen}"
    assert rounds <= total, f"轮转过慢：{rounds} 轮才覆盖完 {total} 个关键词"


def test_rotation_offsets_are_per_source_and_persisted():
    """轮转进度按站点各自独立、并落在 kv 状态里（跨轮次、跨重启连续推进）。

    偏移对关键词总数取模（在 [0, total) 内循环），所以 4 个关键词、每轮 3 个时：
    第 1 轮 0→3，第 2 轮 3→(3+3)%4=2。
    """
    cfg = _cfg(keyword_rotation_per_cycle="all",
               keyword_rotation_per_source={"linkedin": 3})
    pipe = _pipeline(cfg)
    total = len(cfg.all_keywords())                          # 4
    pipe._keyword_window("linkedin")
    assert pipe.db.get_state("keyword_offset_linkedin") == "3"
    pipe._keyword_window("linkedin")
    assert pipe.db.get_state("keyword_offset_linkedin") == str((3 + 3) % total)
    assert pipe.db.get_state("keyword_offset_jobsdb", "0") == "0"   # 未动过


def test_pages_per_keyword_matches_adapters():
    """每关键词的页数消耗必须能如实报到（否则轮转会算错配额）。"""
    assert pages_per_keyword("jobsdb") == 1
    assert pages_per_keyword("linkedin") == 2
    assert pages_per_keyword("indeed") == 1


# ---------------------------------------------------------------------------
# 3. 增量时间窗（电脑不常开时会不会漏岗）
# ---------------------------------------------------------------------------
def test_incremental_window_widens_after_long_downtime():
    """窗口必须随"距上次运行多久"放宽，否则关机期间发布的岗位会被全部漏掉。"""
    pipe = _pipeline(_cfg())
    now = now_hk()
    base = pipe.config.poll_interval_minutes * 2          # 常驻时的默认窗（60 分钟）
    assert pipe._incremental_window_minutes(now, 30) == base
    overnight = pipe._incremental_window_minutes(now, 1440)
    assert overnight >= 1440, f"关机 24 小时后窗口仍只有 {overnight} 分钟，会漏掉一整天岗位"
    # 但不能超过岗位新鲜度上限
    assert overnight <= pipe.config.max_job_age_days * 1440


def test_minutes_since_last_cycle_reads_state():
    pipe = _pipeline(_cfg())
    now = now_hk()
    # 没有任何记录 → 按"最宽窗口"处理（宁可多抓）
    assert pipe._minutes_since_last_cycle(now) == pipe.config.max_job_age_days * 1440
    pipe.db.set_state("last_cycle_at",
                      (now - timedelta(minutes=90)).isoformat(timespec="seconds"))
    assert abs(pipe._minutes_since_last_cycle(now) - 90) < 1
    pipe.db.set_state("last_cycle_at", "这不是时间")      # 状态被改坏时不崩、按最宽处理
    assert pipe._minutes_since_last_cycle(now) == pipe.config.max_job_age_days * 1440


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
