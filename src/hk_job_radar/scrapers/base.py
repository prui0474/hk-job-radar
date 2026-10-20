"""BaseScraper 抽象基类：抓取节流与退避的统一实现（架构 4.4）。

职责划分（为什么这样分层）：
- 本基类负责"流程纪律"：每日配额内翻页计数、失败指数退避（2s→8s→32s
  封顶 60s）、3 次失败放弃本轮、human_delay 由 BrowserManager 提供；
- 子类只负责"页面翻译"：_do_fetch 把某站的 DOM/JSON 翻成 JobPosting 列表。

这样"防封号策略"只有一份实现、一处修补；而页面选择器异构的部分被
隔离在每个适配器文件顶部的常量字典里，站点改版时一行即可修补。
"""
from __future__ import annotations

import hashlib
import re
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from loguru import logger
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hk_job_radar.config import AppConfig
from hk_job_radar.db import Database
from hk_job_radar.models import JobPosting
from hk_job_radar.scrapers.browser import BrowserManager


class NeedsManualVerifyError(Exception):
    """该站必须先由用户亲手过一次人机验证，程序替不了（合规红线：不破解）。

    单独定成一个异常类型，是为了让 _attempt_fetch 的 tenacity **不重试**它：
    没有验证态时重试三轮，结果一模一样，只会让用户白等三分钟。
    """

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(f"{source} 需要先过一次人机验证")


# 需要剥除的跟踪参数（url_normalized 的去噪规则）。保留岗位定位必需参数。
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "gclid", "fbclid", "source", "tracingToken"}

# 官网直投域名识别（US-3：Workday/Greenhouse/Lever 优先投，风险最低）
_DIRECT_APPLY_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
)


class BaseScraper(ABC):
    """所有站点适配器的公共基类。"""

    # 子类必须声明自己的站点标识（对应 config.sources / daily_limits 的键）
    source_name: str = ""

    # 每个关键词消耗多少页配额（= 该适配器的翻页数）。默认 1；
    # 用于让"关键词轮转"能正确推算每轮抓几个关键词才不吃爆当日配额。
    pages_per_keyword: int = 1

    def __init__(self, config: AppConfig, browser: BrowserManager,
                 db: Database | None = None,
                 keywords: list[str] | None = None) -> None:
        self.config = config
        self.browser = browser
        # db 为可选依赖：统计"今日已抓页数"用；REPL 单测可不传
        self.db = db
        # 本轮要抓的关键词子集，由 Pipeline 按"关键词轮转"算好传进来
        # （见 config.keyword_rotation）。传 None/空 → 退回 all_keywords()，
        # 即"每轮全抓"的旧行为，单测与 REPL 调试也能照常用。
        self._keywords_override = [k for k in keywords if k] if keywords else None

    def active_keywords(self) -> list[str]:
        """本轮实际要抓的关键词列表（有轮转子集就用它，否则用全部关键词）。

        顺序仍沿用 config.all_keywords()：普通关键词在前、毕业生项目关键词在后，
        所以 job_types_for_keyword() 依然能正确区分两类词的岗位类型过滤。
        """
        if self._keywords_override:
            return list(self._keywords_override)
        return self.config.all_keywords()

    # ------------------------------------------------------------------
    # 对外唯一入口：带退避与计数的抓取
    # ------------------------------------------------------------------
    def fetch_new_jobs(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        """抓取上次检查点之后的新岗位（模板方法，Pipeline 调用它而非 _do_fetch）。

        流程纪律：
        1. tenacity 指数退避（2s → 8s → 32s，封顶 60s），共 3 次尝试；
        2. 全部失败 → 返回 [] 并写 run_logs(scrape_failed)，不影响其他站；
        3. 中途达到每日上限 → 立刻收手，写 run_logs(limit_hit)。
        """
        if not self.source_name:
            raise NotImplementedError("子类必须声明 source_name 类属性。")
        logger.info(f"[{self.source_name}] 开始抓取（cutoff={last_seen_cutoff:%Y-%m-%d %H:%M}）")
        try:
            return self._attempt_fetch(last_seen_cutoff)
        except _LimitReachedError:
            logger.warning(f"[{self.source_name}] 已达每日抓取上限，本轮收手。")
            if self.db:
                self.db.log_event(self.source_name, "limit_hit",
                                  {"at": "fetch_new_jobs"})
            return []
        except NeedsManualVerifyError as exc:
            # 这一条要说得非常具体：用户此刻最需要知道的是「双击哪个文件」，
            # 而不是「运行哪条命令」。写成命令等于默认他会开终端。
            entry = f"login_{exc.source}.bat"
            if sys.platform != "win32":
                entry = f"login_{exc.source}.command"
            logger.error(
                f"[{exc.source}] 这个站还没过过人机验证，本轮直接跳过（不重试）。\n"
                f"    下一步：双击项目文件夹里的 {entry}\n"
                f"    在弹出的浏览器里点一下验证；看到正常的搜索结果页面，就可以关掉它。\n"
                f"    这一步只需要做一次，之后程序会记住（验证有效期内不用再来）。")
            if self.db:
                self.db.log_event(exc.source, "needs_manual_verify", {"entry": entry})
            return []
        except Exception as exc:
            # 3 次退避后仍失败：放弃本轮该站，写日志，绝不抛出到调度层
            logger.error(f"[{self.source_name}] 抓取失败（重试 3 次后放弃）：{exc}")
            if self.db:
                self.db.log_event(self.source_name, "scrape_failed", {"error": str(exc)[:500]})
            return []

    @retry(
        # 2s → 8s → 32s：multiplier=2, exp_base=4 → 2*4^0, 2*4^1, 2*4^2；封顶 60s
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, exp_base=4, max=60),
        # 「没过验证」不是偶发故障，重试三次结果一样，只会让用户多等三分钟
        retry=retry_if_not_exception_type(NeedsManualVerifyError),
        reraise=True,
    )
    def _attempt_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        """带退避的单次完整抓取（子类的异常会在这里被 tenacity 接住重试）。"""
        return self._do_fetch(last_seen_cutoff)

    # ------------------------------------------------------------------
    # 子类必须实现的两个"翻译"方法
    # ------------------------------------------------------------------
    @abstractmethod
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        """真正的站点抓取逻辑：导航 → 等待 → 解析 → JobPosting[]。

        约定：子类不需要自己 try/except（基类统一退避），但每翻一页
        应调用 self._count_page() 计数。
        """

    @abstractmethod
    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        """各站相对时间文案（'2 hours ago' / '2小時前'）→ 绝对时间（香港时区）。"""

    # ------------------------------------------------------------------
    # 公共工具：所有适配器共享的规范化逻辑（只写一份，避免三站各自漂移）
    # ------------------------------------------------------------------
    def _count_page(self) -> None:
        """翻页计数 + 每日上限检查。子类每导航到一个新列表页就调用一次。"""
        if self.db is None:
            return
        limit = self.config.daily_limits.get(self.source_name, 0)
        used = self.db.today_counts(self.source_name)
        if limit and used >= limit:
            # 用异常做控制流在这里是刻意的：让重试模板也一并停止，
            # 并被 fetch_new_jobs 转成温和的"本轮收手"
            raise _LimitReachedError(self.source_name, limit)
        self.db.log_event(self.source_name, "page_fetched", {})

    def normalize_url(self, url: str) -> str:
        """去跟踪参数的规范化 URL（去重键之一）。

        为什么不用整串 md5：可读的规范化 URL 在排查"为什么被判重复"时
        一眼能看出原因，哈希做不到。
        """
        parts = urlsplit(url.strip())
        query = [(k, v[0]) for k, v in parse_qs(parts.query).items()
                 if k not in _TRACKING_PARAMS]
        normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                 parts.path.rstrip("/"), urlencode(query), ""))
        return normalized

    def detect_direct_apply(self, url: str) -> bool:
        """识别官网直投通道（Workday/Greenhouse/Lever 域名）。"""
        host = urlsplit(url).netloc.lower()
        return any(d in host for d in _DIRECT_APPLY_DOMAINS)

    def make_external_id(self, url: str, fallback_title: str = "") -> str:
        """从 URL 提取站内岗位 ID；提不到时退化为规范化 URL 的哈希。

        JobsDB 的岗位链接形如 /job/xxxxxxxx，末段数字是天然稳定 ID；
        哈希兜底保证任何站点的 URL 都能产生确定性 ID（同一 URL 必得同一 ID）。
        """
        path = urlsplit(url).path
        m = re.search(r"(\d{5,})", path)   # 至少 5 位数字，避免误抓年份等短数字
        if m:
            return m.group(1)
        basis = self.normalize_url(url) or fallback_title
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def snippet(text: str, limit: int = 500) -> str:
        """摘要截断到 500 字符：LLM prompt 有 token 上限（~1500），
        超长 JD 只会稀释关键信息并增加成本。"""
        return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


class _LimitReachedError(Exception):
    """内部信号异常：达到每日抓取上限，用于让整条抓取链路立刻收手。"""

    def __init__(self, source: str, limit: int) -> None:
        super().__init__(f"{source} 达到每日上限 {limit} 页")


# 公开别名：适配器需要在 _do_fetch 里捕获"当日配额用尽"这个信号，
# 以便【返回已经抓到的岗位】而不是把这一轮的成果整个丢掉。
# （fetch_new_jobs 会把它转成温和的"本轮收手"，但那时已经拿不到半成品了。）
LimitReachedError = _LimitReachedError
