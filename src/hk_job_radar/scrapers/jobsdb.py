"""JobsDB 香港站适配器（T02，P0-1）。

站点背景：
- JobsDB 相对 LinkedIn/Indeed 对自动化流量最宽容，因此作为 P0 第一站；
- 站点有 Cloudflare 防护，策略是 persistent context 真实浏览器 + 等待，
  不做任何破解（合规红线）；
- 页面结构会变，所有 CSS 选择器集中在下方 SELECTORS 常量字典——
  站点改版时只需修补这里，一行一个。

抓取策略：
- 对本轮关键词（由 Pipeline 按关键词轮转给出，见 active_keywords()）逐一搜索，
  每个关键词消耗 1 页配额；
- 每页之间 human_delay(2, 8) 随机延迟；
- 列表页解析出岗位卡片后组装 JobPosting，去重交给 Database（url_normalized）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from loguru import logger

from hk_job_radar.models import HK_TZ, JobPosting
from hk_job_radar.scrapers.base import BaseScraper, LimitReachedError
from hk_job_radar.scrapers.job_types import build_jobsdb_worktype

# ---------------------------------------------------------------------------
# 站点常量：改版修补只动这里
# ---------------------------------------------------------------------------
BASE_URL = "https://hk.jobsdb.com"

# 搜索 URL 模板：{kw} 为 URL 编码后的关键词，{loc} 为 URL 编码后的地点，
# {wt} 为岗位类型过滤参数（由 build_jobsdb_worktype 动态生成，如 &worktype=243%2C245）。
# 首页用这个查询 URL（JobsDB 会 302 跳转到 SEO 短链接，形如
#   https://hk.jobsdb.com/data-scientist-jobs/in-Hong-Kong-SAR/part-time ）；
# 翻页则必须基于跳转后的 SEO 短链接追加 ?page=N——
# 若直接往查询 URL 加 &page=N，会被重定向回第 1 页（已实测）。
SEARCH_URL = BASE_URL + "/jobs?keywords={kw}&location={loc}{wt}"

# 每个关键词每次抓的页数。增量模式下 JobsDB 按时间排序，最新岗位都在
# 第 1 页，翻后页只会抓到几天前的旧岗位（会被时间窗过滤）却照样消耗
# 每日页数配额——所以取 1 页最划算。（若重置数据库需要重新 7 天回填，
# 临时改回 3 即可；回填已完成，日常保持 1）
MAX_PAGES_PER_KEYWORD = 1

# 以下选择器基于 2026-10 实测的 JobsDB 真实页面结构（data-automation 属性）。
# 站点改版时只需修补这里，一行一个。
SELECTORS: dict[str, str] = {
    # 岗位卡片容器（每张卡片一个岗位，data-job-id 即站内稳定 ID）
    "job_card": 'article[data-automation="normalJob"]',
    # 岗位标题链接（href 即岗位详情 URL，如 /job/95074242?type=standard...）
    "job_link": 'a[data-automation="jobTitle"]',
    # 公司名称
    "company": '[data-automation="jobCompany"]',
    # 地点
    "location": '[data-automation="jobLocation"]',
    # 薪资（很多岗位不展示，选不到就留空）
    "salary": '[data-automation="jobSalary"]',
    # 发布时间文案（"34m ago" / "2d ago" / "3h ago" 等缩写）
    "posted": '[data-automation="jobListingDate"]',
    # 摘要片段
    "snippet": '[data-automation="jobShortDescription"]',
    # 下一页链接（用于判断还有没有更多结果）
    "next_page": 'a[aria-label="Next"]',
}

# 相对时间文案 → timedelta 的映射表（正则逐条匹配）。
# 顺序很重要：长单位（月 mo / 周 w）必须排在短单位（分钟 m）之前，
# 否则 "3mo ago" 会被 "m" 误判成分钟。实测 JobsDB 现用缩写：34m/3h/2d/1w，
# 且超过 30 天的岗位统一显示为 "30d+ ago"（尾部带 +，代表"至少 30 天前"）。
_RELATIVE_TIME_PATTERNS: list[tuple[re.Pattern[str], callable]] = [
    (re.compile(r"(\d+)\s*(?:mo|months?|個月|个月|月)\+?\s*ago", re.I),
     lambda m: timedelta(days=30 * int(m.group(1)))),
    (re.compile(r"(\d+)\s*(?:w|wks?|weeks?|週|周)\+?\s*ago", re.I),
     lambda m: timedelta(weeks=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(?:d|days?|天|日)\+?\s*ago", re.I),
     lambda m: timedelta(days=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(?:h|hrs?|hours?|小時|小时|時|时)\+?\s*ago", re.I),
     lambda m: timedelta(hours=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(?:m|mins?|minutes?|分鐘|分钟|分)\+?\s*ago", re.I),
     lambda m: timedelta(minutes=int(m.group(1)))),
    (re.compile(r"(\d+)\s*(?:s|secs?|seconds?|秒)\+?\s*ago", re.I),
     lambda m: timedelta(seconds=int(m.group(1)))),
]


class JobsDBScraper(BaseScraper):
    """JobsDB 香港站适配器。"""

    source_name = "jobsdb"
    pages_per_keyword = MAX_PAGES_PER_KEYWORD

    # ------------------------------------------------------------------
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        """逐关键词搜索 → 逐页解析 → 汇总 JobPosting 列表。"""
        page = self.browser.launch(self.source_name)
        all_jobs: list[JobPosting] = []
        seen_urls: set[str] = set()   # 本轮内先去一次重，减少无谓的入库尝试
        limit_hit = False             # 当日配额在途中用尽：保留已抓结果后收手

        for keyword in self.active_keywords():
            # 岗位类型 → JobsDB/SEEK worktype 数字码（按关键词动态生成）：
            # 普通岗用 job_types（实习/兼职），毕业生项目关键词用全职（毕业后入职合法）。
            wt_value = build_jobsdb_worktype(self.config.job_types_for_keyword(keyword))
            wt_fragment = f"&worktype={wt_value}" if wt_value else ""
            seo_base: Optional[str] = None   # 首页跳转后的 SEO 短链接（翻页基于它）
            for page_no in range(1, MAX_PAGES_PER_KEYWORD + 1):
                if page_no == 1:
                    # 首页用查询 URL，JobsDB 会自动 302 到 SEO 短链接
                    url = SEARCH_URL.format(
                        kw=quote_plus(keyword), loc=quote_plus(self.config.location),
                        wt=wt_fragment)
                else:
                    # 翻页必须基于 SEO 短链接追加 ?page=N（实测查询 URL 的 &page 会被丢弃）
                    url = f"{seo_base}?page={page_no}" if seo_base else \
                        SEARCH_URL.format(kw=quote_plus(keyword),
                                          loc=quote_plus(self.config.location),
                                          wt=wt_fragment)

                try:
                    self._count_page()                 # 配额计数 + 上限检查
                except LimitReachedError:
                    # 配额在途中用尽：保留本轮已抓到的岗位（不再继续翻页），
                    # 而不是把整轮成果丢掉。对"每天只开一小会儿机"的用法尤其重要：
                    # 那一次开机就是当天唯一的机会，白抓一轮等于当天全废。
                    logger.warning(f"[jobsdb] 今日配额已用尽：保留本轮已抓到的 "
                                   f"{len(all_jobs)} 个岗位，下次开机接着抓。")
                    limit_hit = True
                    break
                logger.info(f"[jobsdb] 打开搜索页：{keyword} 第 {page_no} 页")

                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # 导航后先过 Cloudflare 检查（等待而非破解）
                if not self.browser.wait_cloudflare(page):
                    raise RuntimeError("Cloudflare 挑战未通过，本轮放弃（不破解，只等待）")
                page.wait_for_load_state("networkidle", timeout=30000)
                # 每次导航之间的随机延迟：防封号核心手段
                self.browser.human_delay()

                # 首页导航后记下 SEO 短链接（去掉 query / fragment）
                if seo_base is None:
                    seo_base = page.url.split("?")[0].split("#")[0]

                jobs = self._parse_listing(page, keyword)
                fresh = [j for j in jobs
                         if j.url_normalized not in seen_urls
                         and (j.posted_at is None or j.posted_at >= last_seen_cutoff)]
                for j in fresh:
                    seen_urls.add(j.url_normalized)
                all_jobs.extend(fresh)

                # 没有"下一页"链接就提前结束该关键词，省配额
                if not page.locator(SELECTORS["next_page"]).count():
                    break
            if limit_hit:
                break

        logger.info(f"[jobsdb] 本轮共解析到 {len(all_jobs)} 个岗位")
        return all_jobs

    # ------------------------------------------------------------------
    def _parse_listing(self, page, keyword: str) -> list[JobPosting]:
        """解析当前搜索结果列表页。优先尝试页面内嵌 JSON（最稳），
        失败则退回 DOM 选择器解析（选择器集中定义，便于修补）。"""
        jobs: list[JobPosting] = []
        try:
            json_jobs = self._try_parse_embedded_json(page, keyword)
            if json_jobs:
                return json_jobs
        except Exception as exc:
            logger.debug(f"[jobsdb] 内嵌 JSON 解析未命中，退回 DOM 解析：{exc}")

        cards = page.locator(SELECTORS["job_card"]).all()
        logger.debug(f"[jobsdb] DOM 解析：{len(cards)} 张岗位卡片")
        for card in cards:
            try:
                job = self._parse_card(card, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                # 单卡解析失败只跳过该卡，不拖垮整页（页面常有广告位等异构节点）
                logger.debug(f"[jobsdb] 单张卡片解析失败（跳过）：{exc}")
        return jobs

    def _parse_card(self, card, keyword: str) -> Optional[JobPosting]:
        """单张岗位卡片 → JobPosting。"""
        link = card.locator(SELECTORS["job_link"]).first
        if not link.count():
            return None
        href = link.get_attribute("href") or ""
        if not href:
            return None
        url = href if href.startswith("http") else BASE_URL + href
        title = (link.inner_text() or "").strip()

        def _text(sel: str) -> str:
            loc = card.locator(sel)
            return (loc.first.inner_text() or "").strip() if loc.count() else ""

        raw_posted = _text(SELECTORS["posted"])
        return JobPosting(
            source=self.source_name,
            external_id=self.make_external_id(url, title),
            title=title,
            company=_text(SELECTORS["company"]) or "（公司未标注）",
            url=url,
            url_normalized=self.normalize_url(url),
            salary_text=_text(SELECTORS["salary"]),
            location=_text(SELECTORS["location"]) or "Hong Kong",
            posted_at=self._extract_posted_at(raw_posted),
            description_snippet=self.snippet(_text(SELECTORS["snippet"])),
            keyword_matched=keyword,      # 记录是哪个搜索词把它带出来的
            direct_apply=self.detect_direct_apply(url),
        )

    # ------------------------------------------------------------------
    def _try_parse_embedded_json(self, page, keyword: str) -> list[JobPosting]:
        """尝试从页面内嵌的 Next.js JSON（script#__NEXT_DATA__）直接取数据。

        为什么保留这条路径：列表类站点通常把首屏数据内嵌成 JSON，其结构
        比视觉 DOM 稳定得多（改样式不改数据模型）。实现成"尽力而为"：
        结构对不上就静默退回 DOM 解析，两条腿走路。
        """
        jobs: list[JobPosting] = []
        for sel in ('script#__NEXT_DATA__', 'script[id="__NEXT_DATA__"]'):
            loc = page.locator(sel)
            if not loc.count():
                continue
            data = json.loads(loc.first.inner_text() or "{}")
            # 在 JSON 树里递归找"长得像岗位列表"的数组：
            # 元素是 dict 且同时含 title 与(链接/公司)键。
            for node_list in self._walk_find_job_arrays(data):
                for item in node_list:
                    title = str(item.get("title") or "").strip()
                    if not title:
                        continue
                    # 链接可能出现在多个键里，常见的有seek的listing结构
                    url_part = (item.get("canonicalJobUrl") or item.get("jobUrl")
                                or item.get("url") or "")
                    if not url_part:
                        continue
                    url = url_part if url_part.startswith("http") else BASE_URL + str(url_part)
                    advertiser = item.get("advertiser") or {}
                    company = str(
                        advertiser.get("description") if isinstance(advertiser, dict)
                        else advertiser or item.get("company") or ""
                    ).strip() or "（公司未标注）"
                    listing_date = item.get("listingDate") or item.get("postedAt") or ""
                    jobs.append(JobPosting(
                        source=self.source_name,
                        external_id=str(item.get("id") or self.make_external_id(url, title)),
                        title=title,
                        company=company,
                        url=url,
                        url_normalized=self.normalize_url(url),
                        salary_text=str(item.get("salary") or ""),
                        location="Hong Kong",
                        posted_at=self._extract_posted_at(str(listing_date)),
                        description_snippet=self.snippet(str(item.get("teaser") or "")),
                        keyword_matched=keyword,
                        direct_apply=self.detect_direct_apply(url),
                    ))
                if jobs:
                    break   # 找到第一组可用列表就够了
            if jobs:
                break
        return jobs

    @classmethod
    def _walk_find_job_arrays(cls, node, depth: int = 0):
        """递归遍历 JSON 树，产出所有"疑似岗位列表"的数组（生成器）。"""
        if depth > 12:      # 防御：限制递归深度，避免病态结构
            return
        if isinstance(node, list) and node and all(isinstance(x, dict) for x in node[:5]):
            sample = node[0]
            if "title" in sample and any(k in sample for k in ("url", "jobUrl", "canonicalJobUrl")):
                yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from cls._walk_find_job_arrays(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                yield from cls._walk_find_job_arrays(value, depth + 1)

    # ------------------------------------------------------------------
    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        """JobsDB 时间文案 → 香港时区绝对时间。

        支持两类输入：
        1. 相对文案："2h ago" / "Posted 3 days ago" / "30分鐘前"；
        2. ISO 8601（内嵌 JSON 路径里的 listingDate）。
        解析失败返回 None：宁可让岗位带着 None 进流水线（日报不显示时间），
        也不能因时间解析问题丢掉岗位（宁多勿漏）。
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        # ISO 8601 分支
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(HK_TZ)
        except ValueError:
            pass
        # 相对时间分支：now - delta
        for pattern, to_delta in _RELATIVE_TIME_PATTERNS:
            m = pattern.search(raw)
            if m:
                from hk_job_radar.models import now_hk
                return now_hk() - to_delta(m)
        logger.debug(f"[jobsdb] 无法解析发布时间文案：{raw!r}")
        return None
