"""Indeed 香港站适配器（T04，P1）。

站点特点与应对：
1. Indeed 香港区有时会把 /jobs 重定向到带地域前缀的地址（如 hk.indeed.com），
   因此所有链接统一补全为绝对地址；
2. sort=date + fromage=3：只取最近 3 天、按时间倒序，既契合"发现新岗位"
   的目标，又把抓取量压到较小（抓取量越小，被风控盯上的概率越低）；
   （fromage=1 即 24h 窗口过窄，实测经常无结果，故放宽到 3 天）
3. Indeed 条款明文禁止第三方自动化其申请流程——本适配器严格只做
   "浏览搜索结果"，不碰任何申请动作。

已知风险（架构 U1）：Indeed 在部分地区对自动化流量限流严重。若实测频繁被
拦截，可在 config.yaml 把 sources.indeed 设为 false，退化为 JobsDB +
LinkedIn 两站覆盖，不影响主链路。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from loguru import logger

from hk_job_radar.models import HK_TZ, JobPosting, now_hk
from hk_job_radar.scrapers.base import BaseScraper, LimitReachedError
from hk_job_radar.scrapers.job_types import build_indeed_sc

BASE_URL = "https://hk.indeed.com"

# q=关键词, l=地点, sort=date 按时间倒序, fromage=3 只看最近 3 天, start=分页偏移
# {sc} 由 build_indeed_sc 动态生成（如 &sc=0kf%3Ajt%28internship%2Cparttime%29%3B），
# 从 config.job_types 映射而来——Indeed 的 jt 单值参数无法表达"多类"，必须用 sc 过滤器。
#
# 为什么是 fromage=3 而不是 1：实测 fromage=1（最近 24 小时）在 Indeed 香港区
# 经常返回"找不到職位"（专业岗位 24h 内往往没有新发布，实测多个关键词全部 0 结果）。
# Indeed 的 fromage 只有 1/3/7/14/30 档，没有"2 天"，
# 取 3 天是「尽量贴近用户 ≤2 天诉求」与「保证有结果覆盖」的折中。
SEARCH_URL = BASE_URL + "/jobs?q={kw}&l={loc}&sort=date&fromage=3&start={start}{sc}"

# 每个关键词只抓第 1 页。原因：Indeed 对同一 profile 的"第二次访问"就软性
# 限流返回空，翻第 2 页属于同一 profile 的第 2 次访问、大概率空手而归；
# 且 fromage=3 下第 1 页已有 4-16 个岗位，足够。翻页会白白消耗每日页数配额。
MAX_PAGES = 1
PAGE_SIZE = 10   # Indeed 每页实际约 10-15 条，取保守值

SELECTORS: dict[str, str] = {
    # 关键：只用单一容器。原来的 "job_seen_beacon, td.resultContent,
    # div.cardOutline" 是同一张卡片的嵌套层级，并列选择器会把同一岗位
    # 重复抓 3 遍（实测确实出现 3 份重复）。
    "job_card": "div.job_seen_beacon",
    # 链接只用带 data-jk 的锚点：jk 是 Indeed 的稳定岗位 ID。
    "job_link": "a[data-jk]",
    # 实测当前标题在 h3.jobTitle 里（不是 h2）。
    "title": "h3.jobTitle span, a[data-jk] span",
    "company": "span[data-testid='company-name'], span.companyName, div.companyName",
    "location": "div[data-testid='text-location'], div.companyLocation",
    "salary": "div[data-testid='attribute_snippet_testid'], "
              "span.salary-snippet-container, div.salary-snippet",
    # 实测当前列表页（mosaic 新版）不展示发布时间，仅详情页有；
    # 本适配器只浏览列表页（合规），posted_at 通常为 None——
    # 新鲜度由 URL 的 fromage=3（最近 3 天）保证，不必靠时间戳。
    "posted": "span[data-testid='myJobsStateDate'], span.date, "
              "span[data-testid='job-age']",
    "snippet": "div.job-snippet, div[data-testid='jobsnippet_footer']",
}

_TODAY_MARKERS = ("today", "just posted", "今天", "剛剛", "刚刚", "今日")
_YESTERDAY_MARKERS = ("yesterday", "昨天")

# 相对时间文案 → timedelta。后缀同时支持英文 "ago" 与中文 "前"：
# hk.indeed.com 按浏览器 locale 显示中文相对时间（如"2 天前"）。
_RELATIVE_TIME_PATTERNS: list[tuple[re.Pattern[str], callable]] = [
    (re.compile(r"(\d+)\s*(second|sec|秒)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(seconds=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(minute|min|分鐘|分钟)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(minutes=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(hour|hr|小時|小时|時|时)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(day|天|日)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(week|週|周)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(weeks=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(month|個月|个月|月)s?\s*(?:ago|前)", re.I),
     lambda m: timedelta(days=30 * int(m.group(1)))),
]


class IndeedScraper(BaseScraper):
    """Indeed 香港站适配器（只读监控）。"""

    source_name = "indeed"
    pages_per_keyword = MAX_PAGES

    # ------------------------------------------------------------------
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        # 关键：Indeed 会拦截无头浏览器，必须用有头模式（可见浏览器）。
        # 更棘手的是——Indeed 对"同一浏览器 profile 连续访问搜索页"会软性
        # 限流：第一个搜索正常返回，第二个起返回空结果（实测间隔 30 秒也无法
        # 恢复，只有换全新 profile 的"首次访问"才放行）。
        # 因此这里每个关键词用独立的 user_data_dir（独立"新访客"身份），
        # 抓完即关闭该 profile 释放资源。
        all_jobs: list[JobPosting] = []
        seen: set[str] = set()
        loc = quote_plus(self.config.location or "Hong Kong")
        limit_hit = False             # 当日配额在途中用尽：保留已抓结果后收手

        for idx, keyword in enumerate(self.active_keywords()):
            # 岗位类型 → Indeed sc 过滤器（按关键词动态生成）：
            # 普通岗用 job_types（实习/兼职），毕业生项目关键词用全职（毕业后入职合法）。
            sc_param = build_indeed_sc(self.config.job_types_for_keyword(keyword))
            sc_fragment = f"&{sc_param}" if sc_param else ""
            source_key = f"{self.source_name}_{idx}"   # 每关键词独立 profile
            # fresh=True：每次抓取前清空该 profile，保证"全新访客"身份，
            # 规避 Indeed 对回头客的软性限流（否则第二次起返回空结果）。
            page = self.browser.launch(source_key, force_headless=False, fresh=True)
            try:
                for page_no in range(MAX_PAGES):
                    url = SEARCH_URL.format(kw=quote_plus(keyword), loc=loc,
                                            start=page_no * PAGE_SIZE, sc=sc_fragment)
                    try:
                        self._count_page()
                    except LimitReachedError:
                        # 配额在途中用尽：保留本轮已抓到的岗位，不再继续翻页
                        logger.warning(f"[indeed] 今日配额已用尽：保留本轮已抓到的 "
                                       f"{len(all_jobs)} 个岗位，下次开机接着抓。")
                        limit_hit = True
                        break
                    logger.info(f"[indeed] 打开搜索页：{keyword}（第 {page_no + 1} 页）")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    # 有头模式下若仍被 Cloudflare 拦截，等它自动通过（只等待，不破解）
                    if not self.browser.wait_cloudflare(page):
                        raise RuntimeError("Cloudflare 挑战未通过，本轮放弃（不破解，只等待）")
                    # 与 LinkedIn 同理：不依赖 networkidle，改为等岗位卡片出现
                    try:
                        page.wait_for_selector(SELECTORS["job_card"], state="attached",
                                               timeout=15000)
                    except Exception:
                        pass
                    self.browser.human_delay()

                    jobs = self._parse_listing(page, keyword)
                    fresh = [j for j in jobs
                             if j.url_normalized not in seen
                             and (j.posted_at is None or j.posted_at >= last_seen_cutoff)]
                    for j in fresh:
                        seen.add(j.url_normalized)
                    all_jobs.extend(fresh)

                    if len(jobs) < PAGE_SIZE:
                        break
            finally:
                self.browser.close_source(source_key)
            if limit_hit:
                break

        logger.info(f"[indeed] 本轮共解析到 {len(all_jobs)} 个岗位")
        return all_jobs

    # ------------------------------------------------------------------
    def _parse_listing(self, page, keyword: str) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        cards = page.locator(SELECTORS["job_card"]).all()
        logger.debug(f"[indeed] 解析到 {len(cards)} 张岗位卡片")
        for card in cards:
            try:
                job = self._parse_card(card, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"[indeed] 单卡解析失败（跳过）：{exc}")
        return jobs

    def _parse_card(self, card, keyword: str) -> Optional[JobPosting]:
        link = card.locator(SELECTORS["job_link"]).first
        if not link.count():
            return None
        # 关键：Indeed 的 /rc/clk?... 链接带每次请求都变化的跟踪参数
        # （bb / xkcb / fccid / vjs），直接用会导致同一岗位每次被抓都被判成
        # "新岗位"、无法去重。稳定标识是 data-jk 属性，用规范链接
        # viewjob?jk=xxx 代替——既稳定去重，点击也能直达岗位页。
        jk = (link.get_attribute("data-jk") or "").strip()
        if jk:
            url = f"{BASE_URL}/viewjob?jk={jk}"
        else:
            href = link.get_attribute("href") or ""
            if not href:
                return None
            url = href if href.startswith("http") else BASE_URL + href

        def _text(sel: str) -> str:
            loc = card.locator(sel)
            return (loc.first.inner_text() or "").strip() if loc.count() else ""

        title = _text(SELECTORS["title"]) or (link.inner_text() or "").strip()
        if not title:
            return None

        return JobPosting(
            source=self.source_name,
            # 优先用 jk 作为岗位 ID（如 indeed:ca36a9d5367ac6f9，人可读），
            # 提不到 jk 时退化为规范化 URL 的 SHA1 前 16 位。
            external_id=jk or self.make_external_id(url, title),
            title=title,
            company=_text(SELECTORS["company"]) or "（公司未标注）",
            url=url,
            url_normalized=self.normalize_url(url),
            salary_text=_text(SELECTORS["salary"]),
            location=_text(SELECTORS["location"]) or "Hong Kong",
            posted_at=self._extract_posted_at(_text(SELECTORS["posted"])),
            description_snippet=self.snippet(_text(SELECTORS["snippet"])),
            keyword_matched=keyword,
            direct_apply=self.detect_direct_apply(url),
        )

    # ------------------------------------------------------------------
    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        raw = (raw or "").strip()
        if not raw:
            return None
        low = raw.lower()
        if any(m in low for m in _TODAY_MARKERS):
            return now_hk()
        if any(m in low for m in _YESTERDAY_MARKERS):
            return now_hk() - timedelta(days=1)
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(HK_TZ)
        except ValueError:
            pass
        for pattern, to_delta in _RELATIVE_TIME_PATTERNS:
            m = pattern.search(raw)
            if m:
                return now_hk() - to_delta(m)
        logger.debug(f"[indeed] 无法解析发布时间：{raw!r}")
        return None
