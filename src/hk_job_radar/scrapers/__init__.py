"""抓取层（scrapers/）。

工厂模式（架构 4.4）：Pipeline 只认 get_scraper() 工厂，不 import 任何
具体适配器——这样后续 T04 加入 LinkedIn/Indeed 时，只需在这里注册一行，
流水线代码零改动。

扩展示例（T04）：
    from hk_job_radar.scrapers.linkedin import LinkedInScraper
    SCRAPER_REGISTRY["linkedin"] = LinkedInScraper
"""
from __future__ import annotations

from loguru import logger

from hk_job_radar.config import AppConfig
from hk_job_radar.db import Database
from hk_job_radar.scrapers.base import BaseScraper
from hk_job_radar.scrapers.browser import BrowserManager
from hk_job_radar.scrapers.indeed import IndeedScraper
from hk_job_radar.scrapers.jobsdb import JobsDBScraper
from hk_job_radar.scrapers.linkedin import LinkedInScraper
from hk_job_radar.scrapers.workday import WorkdayScraper

# 注册表：source 名 → 适配器类。
# T04 已注册全部三站：在 config.yaml 的 sources 段把某项设为 true 即可启用。
# （未注册却在 config 里开启，get_scraper 会给出明确中文报错而不是静默跳过）
SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "jobsdb": JobsDBScraper,
    "linkedin": LinkedInScraper,
    "indeed": IndeedScraper,
    # 雇主官网（Workday）：纯 HTTP 接口，不需要浏览器，是唯一自带
    # 「是否还在收简历」字段的源，见 workday.py 顶部说明。
    "workday": WorkdayScraper,
}


def get_scraper(source: str, config: AppConfig, browser: BrowserManager,
                db: Database | None = None,
                keywords: list[str] | None = None) -> BaseScraper:
    """按 source 名实例化适配器。

    db 参数说明：架构类图中 BaseScraper 只持有 config/browser，这里做了
    一个受控扩展——每日抓取上限需要"每翻一页就计数"，计数落在 run_logs 表，
    因此适配器需要一个可选的 db 引用。传 None 时（如 REPL 单测）只丢统计能力，
    不影响抓取本身。

    keywords 参数说明：同样是一个受控扩展——关键词轮转（见 config.keyword_rotation）
    由 Pipeline 统一算好"本轮抓哪几个关键词"再传进来，因为轮转偏移要落库、
    跨轮次连续推进，而 db 只有 Pipeline 稳定持有。传 None → 抓全部关键词。
    """
    cls = SCRAPER_REGISTRY.get(source)
    if cls is None:
        known = "、".join(SCRAPER_REGISTRY) or "（暂无）"
        raise ValueError(
            f"未注册的抓取源：{source}。当前可用：{known}。"
            f"请对照 config.example.yaml 的 sources 段检查拼写。")
    logger.debug(f"实例化抓取器：{source} → {cls.__name__}")
    return cls(config, browser, db=db, keywords=keywords)


def pages_per_keyword(source: str) -> int:
    """该站点"每个关键词要翻几页"（= 消耗几页每日配额）。

    为什么由这里提供：页数上限写在各适配器里（MAX_PAGES / MAX_PAGES_PER_KEYWORD），
    而"关键词轮转"推算每轮抓几个关键词时需要知道它。放在注册表旁边，就能以适配器
    为准、不把数字抄进配置文件里（抄一遍就会有对不上的一天）。
    """
    cls = SCRAPER_REGISTRY.get(source)
    return int(getattr(cls, "pages_per_keyword", 1)) if cls else 1
