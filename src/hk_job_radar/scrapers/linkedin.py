"""LinkedIn 香港岗位适配器（T04，P1）。

为什么单独强调 LinkedIn 的风险：
LinkedIn 是四个平台中风控最严的一个（协议 8.2 明文禁止 bots），因此
本适配器的设计原则是"只读、低频、克制"：
1. 只抓搜索列表页，不点开岗位详情、不做任何交互动作；
2. 每页之间 human_delay（默认 2-8 秒）由基类与 BrowserManager 强制插入；
3. 使用 f_TPR=r86400（只看最近 24 小时）而非翻很多页——翻页越多风险越大，
   而"新岗位"本来就在最近一天里，翻老页没有价值；
4. 强烈建议用求职专用小号登录（主号人脉资产不应暴露在自动化活动中）。

登录态策略：
- 不依赖登录。2026-11 LinkedIn 把搜索页换成新版前端后，登录后台那套 DOM 里
  根本没有可解析的岗位卡片，所以改走公开访客列表接口（见 GUEST_SEARCH_URL），
  未登录也能抓到带链接的岗位；
- 若配了 `data/linkedin_session.json`，仍会顺手注入 cookie，让请求更像真实访客；
- 绝不自动提交任何登录表单（合规红线：不接受把账号密码交给脚本）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from loguru import logger

from hk_job_radar.config import DATA_DIR
from hk_job_radar.models import HK_TZ, JobPosting, now_hk
from hk_job_radar.scrapers.base import BaseScraper, LimitReachedError
from hk_job_radar.scrapers.job_types import build_linkedin_f_jt, extract_site_job_type

BASE_URL = "https://www.linkedin.com"

# 登录凭证的本地缓存文件：持久化 context 的 add_cookies 不会把 Cookie 写进
# 磁盘 profile（关闭即丢），所以改为把 li_at/JSESSIONID 明文存到本地文件，
# 每次抓取启动时重新注入一次，保证登录态持续有效。
SESSION_FILE = DATA_DIR / "linkedin_session.json"


def save_session(li_at: str, jsessionid: str = "") -> None:
    """把 LinkedIn 登录凭证保存到本地文件（供每次抓取时重新注入）。"""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "li_at": li_at,
        "JSESSIONID": jsessionid,
        "saved_at": now_hk().isoformat(),
    }
    SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_session() -> list[dict]:
    """读取保存的 LinkedIn 登录凭证，转成 add_cookies 需要的 cookie 列表。"""
    if not SESSION_FILE.exists():
        return []
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    cookies: list[dict] = []
    if data.get("li_at"):
        cookies.append({"name": "li_at", "value": data["li_at"],
                        "domain": ".linkedin.com", "path": "/"})
    if data.get("JSESSIONID"):
        cookies.append({"name": "JSESSIONID", "value": data["JSESSIONID"],
                        "domain": ".linkedin.com", "path": "/"})
    return cookies

# 搜索入口：LinkedIn 的公开访客列表接口（返回一段 HTML，不是整页 SPA）。
#
# 为什么不用 /jobs/search（2026-11-12 实测）：那个地址已经被改版成
# React Server Components 前端，会 302 到 /jobs/search-results，class 全是
# i87l55 这类原子化哈希名，卡片上没有 a[href*="/jobs/view/"] 链接，
# 旧的 `div.job-card-container` / `li.jobs-search-results__list-item` 等三代
# 选择器全部 0 命中，连 `time` 元素都没有。结果是页面能打开、岗位文本也在，
# 但解析出 0 条，而且不报错，看起来和「今天没有新岗位」一模一样。
#
# 这个访客接口只回列表片段，一页 10 条，结构还是老的 base-card 那套：
#   li > a.base-card__full-link + h3.base-search-card__title
# 实测 start=0/10/20 三页各 10 条、互不重复，岗位链接里带数字 ID。
#
# 它同样是「只读列表页」：不登录、不点击、不打开详情，与原来的克制原则一致，
# 工作量反而更小（不用等 SPA 渲染）。
#
# {jt} 由 build_linkedin_f_jt 动态生成（如 &f_JT=I%2CP，实习+兼职）。
GUEST_SEARCH_URL = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                    "?keywords={kw}&location={loc}&f_TPR=r86400&start={start}{jt}")

#: 访客接口每页固定 10 条（不是登录版的 25 条）
PAGE_SIZE = 10

#: 每个关键词最多翻几页（克制原则：翻页越多风险越大）
MAX_PAGES = 2

# 选择器集中在此：LinkedIn DOM 变化频繁，改版时只需改这几行。
#
# 2026-11-12 更新：这里的每一行都对应访客接口返回的那段 HTML（见 GUEST_SEARCH_URL）。
# 登录后台那套 div.job-card-container / job-card-list__* 已经随新版前端一起消失，
# 留着只会让人误以为还有两条路可走。
SELECTORS: dict[str, str] = {
    # 列表项：li 里含「/jobs/view/ 链接」的才算，避免把嵌套 li 或广告位算进来
    "job_card": "li:has(a[href*='/jobs/view/'])",
    "job_link": "a.base-card__full-link, a[href*='/jobs/view/']",
    "title": "h3.base-search-card__title",
    "company": "h4.base-search-card__subtitle",
    "location": "span.job-search-card__location",
    # 可见文案（「1 小時前」）与 datetime 属性（2026-11-12）都在 <time> 上
    "posted": "time.job-search-card__listdate, time[datetime]",
    "snippet": "p.job-search-card__snippet, div.job-search-card__snippet",
    "salary": "span.job-search-card__salaryinfo",
}

# "Just now" / "Now" 这类无数字文案单独处理
_NOW_MARKERS = ("just now", "now", "剛剛", "刚刚", "剛剛發布", "moments ago")

# 相对时间文案 → timedelta。后缀同时支持英文 "ago" 与中文 "前"：
# LinkedIn 按浏览器 locale（本系统设 zh-HK）显示"1 小時前"，不带 "ago"，
# 原来的英文-only 正则匹配不到，导致发布时间全部解析为 None。
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


class LinkedInScraper(BaseScraper):
    """LinkedIn 香港岗位适配器（只读监控，绝不自动投递）。"""

    source_name = "linkedin"
    pages_per_keyword = MAX_PAGES

    # ------------------------------------------------------------------
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        page = self.browser.launch(self.source_name)
        # 重新注入登录凭证（可选）。访客接口本身不要求登录，但带着 cookie
        # 请求更像一个真实访客，也避免被当成全新匿名流量。
        # 注意：add_cookies() 在 persistent context 上**不会落盘**（实测：
        # 注入后重启浏览器即消失），所以每次启动都要重注入一次。
        session_cookies = load_session()
        if session_cookies:
            self.browser.inject_cookies(self.source_name, session_cookies)
        all_jobs: list[JobPosting] = []
        seen: set[str] = set()
        loc = quote_plus(self.config.location or "Hong Kong")
        limit_hit = False             # 当日配额在途中用尽：保留已抓结果后收手
        pages_tried = 0               # 诊断「一页都没解析到卡片」用
        cards_seen = 0

        for keyword in self.active_keywords():
            # 岗位类型 → LinkedIn f_JT 参数（按关键词动态生成）：
            # 普通岗用 job_types（实习/兼职），毕业生项目关键词用全职（毕业后入职合法）。
            jt_value = build_linkedin_f_jt(self.config.job_types_for_keyword(keyword))
            jt_fragment = f"&f_JT={jt_value}" if jt_value else ""
            for page_no in range(MAX_PAGES):
                url = GUEST_SEARCH_URL.format(kw=quote_plus(keyword), loc=loc,
                                              start=page_no * PAGE_SIZE, jt=jt_fragment)
                try:
                    self._count_page()
                except LimitReachedError:
                    # 配额在途中用尽：保留本轮已抓到的岗位，不再继续翻页
                    logger.warning(f"[linkedin] 今日配额已用尽：保留本轮已抓到的 "
                                   f"{len(all_jobs)} 个岗位，下次开机接着抓。")
                    limit_hit = True
                    break
                logger.info(f"[linkedin] 打开搜索页：{keyword}（第 {page_no + 1} 页）")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # LinkedIn 偶发出现验证页：只等待，不破解
                self.browser.wait_cloudflare(page)
                # 访客接口偶尔会回一个登录/注册页（被节流时），先做快速失败，
                # 避免对着空页面白等 15 秒再空耗配额。
                if self._page_is_authwall(page):
                    logger.warning(
                        "[linkedin] 访客列表接口被退回登录页：LinkedIn 对这台机器的"
                        "匿名访问做了节流。\n"
                        "  下一步：双击 login_linkedin.bat（Mac 上是 login_linkedin.command），\n"
                        "  用【求职专用小号】登录一次，登录态会被复用，本轮先跳过。")
                    break
                # 等列表项出现；等不到也继续，交给解析按实际数量判定。
                # （不用 networkidle：这个站的长连接永远不让网络空闲。）
                try:
                    page.wait_for_selector(SELECTORS["job_card"], state="attached",
                                           timeout=15000)
                except Exception:
                    pass

                self.browser.human_delay()

                pages_tried += 1
                cards = page.locator(SELECTORS["job_card"]).count()
                cards_seen = max(cards_seen, cards)
                jobs = self._parse_listing(page, keyword)
                fresh = [j for j in jobs
                         if j.url_normalized not in seen
                         and (j.posted_at is None or j.posted_at >= last_seen_cutoff)]
                for j in fresh:
                    seen.add(j.url_normalized)
                all_jobs.extend(fresh)

                if len(jobs) < PAGE_SIZE:
                    break   # 本关键词结果已不足一页，没必要再翻
            if limit_hit:
                break

        logger.info(f"[linkedin] 本轮共解析到 {len(all_jobs)} 个岗位")
        if not all_jobs and pages_tried and cards_seen == 0:
            # 一页卡片都没等到就结束，只说「0 个岗位」等于什么都没说。
            logger.warning(
                f"[linkedin] {pages_tried} 页都没有解析到岗位卡片。两种可能：\n"
                f"    ① 这些关键词今天确实没有符合条件的新岗位；\n"
                f"    ② LinkedIn 又改了页面结构（本适配器换过一次入口，见 GUEST_SEARCH_URL 注释）。\n"
                f"    下一步：先双击 login_linkedin.bat 登录一次再跑；仍然为 0 就把它当成改版，"
                f"把这条日志发给开发者。")
        return all_jobs

    # ------------------------------------------------------------------
    @staticmethod
    def _page_is_authwall(page) -> bool:
        """判断当前页面是否被重定向到 LinkedIn 的登录/注册墙。

        未登录访客首次能看部分岗位，但之后会被节流：搜索页会 302 到
        /login 或 /signup（标题变为「登入 | LinkedIn」「註冊 | LinkedIn」）。
        识别出来是为了立刻停手并提示登录，而不是对着空页面白翻页、空耗配额。
        """
        url = page.url or ""
        title = (page.title() or "").lower()
        if any(frag in url for frag in ("/login", "/signup", "authwall", "session_redirect")):
            return True
        return any(m in title for m in
                   ("註冊", "注册", "登入", "登录", "sign in", "join linkedin", "join now"))

    def _parse_listing(self, page, keyword: str) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        cards = page.locator(SELECTORS["job_card"]).all()
        logger.debug(f"[linkedin] 解析到 {len(cards)} 张岗位卡片")
        for card in cards:
            try:
                job = self._parse_card(card, keyword)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"[linkedin] 单卡解析失败（跳过）：{exc}")
        return jobs

    def _parse_card(self, card, keyword: str) -> Optional[JobPosting]:
        link = card.locator(SELECTORS["job_link"]).first
        if not link.count():
            return None
        href = link.get_attribute("href") or ""
        if not href:
            return None
        # LinkedIn 链接常带 tracking 参数（如 ?refId=...&trackingId=...），
        # normalize_url 会剥掉它们，保证同一岗位换 tracking 也判为同一个
        url = href if href.startswith("http") else BASE_URL + href
        url = url.split("?")[0] if "trackingId" in url else url

        def _text(sel: str) -> str:
            loc = card.locator(sel)
            return (loc.first.inner_text() or "").strip() if loc.count() else ""

        title = _text(SELECTORS["title"]) or (link.inner_text() or "").strip()
        if not title:
            return None

        # 发布时间：优先解析可见文案（"1 小時前"等，精度到小时），
        # 失败再退到 <time> 的 datetime 属性（形如 2026-10-07，只到天），
        # 仍失败则为 None（宁缺不丢岗位）。
        posted_at = None
        time_el = card.locator(SELECTORS["posted"]).first
        if time_el.count():
            posted_at = self._extract_posted_at((time_el.inner_text() or "").strip()) or \
                        self._extract_posted_at((time_el.get_attribute("datetime") or "").strip())

        # LinkedIn 新版列表卡片设计上不显示摘要，也常常不显示类型；
        # 旧版会把 "Full-time" 印在地点那一行里。能读到就读，读不到留给推断。
        site_type = extract_site_job_type(card.inner_text())
        return JobPosting(
            source=self.source_name,
            external_id=self.make_external_id(url, title),
            title=title,
            company=_text(SELECTORS["company"]) or "（公司未标注）",
            url=url,
            url_normalized=self.normalize_url(url),
            salary_text=_text(SELECTORS["salary"]),
            location=_text(SELECTORS["location"]) or "Hong Kong",
            posted_at=posted_at,
            description_snippet=self.snippet(_text(SELECTORS["snippet"])),
            job_type=site_type,
            keyword_matched=keyword,
            direct_apply=self.detect_direct_apply(url),
        )

    # ------------------------------------------------------------------
    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        """LinkedIn 时间文案 → 香港时区绝对时间。

        支持："Just now" / "2 hours ago" / "1 day ago" / "30分鐘前" 等；
        解析失败返回 None（宁可让岗位带着 None 进流水线，也不丢岗位）。
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        low = raw.lower()
        if any(m in low for m in _NOW_MARKERS):
            return now_hk()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(HK_TZ)
        except ValueError:
            pass
        for pattern, to_delta in _RELATIVE_TIME_PATTERNS:
            m = pattern.search(raw)
            if m:
                return now_hk() - to_delta(m)
        logger.debug(f"[linkedin] 无法解析发布时间：{raw!r}")
        return None
