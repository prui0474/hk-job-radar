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
- 已登录（persistent context 里有 Cookie）→ 正常解析；
- 未登录 → 记录 WARNING 并以访客模式尽力解析（访客也能看到部分岗位），
  绝不自动提交任何登录表单（合规红线：不接受把账号密码交给脚本）。
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
from hk_job_radar.scrapers.job_types import build_linkedin_f_jt

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

# 搜索 URL：f_TPR=r86400 = 最近 24 小时；sortBy=DD = 按发布时间倒序（最新在最前）
# {jt} 由 build_linkedin_f_jt 动态生成（如 &f_JT=I%2CP，实习+兼职），
# 从 config.job_types 映射而来——学生签证在读期间只抓实习/兼职，绝不抓全职。
SEARCH_URL = (BASE_URL + "/jobs/search"
              "?keywords={kw}&location={loc}&f_TPR=r86400&sortBy=DD&start={start}{jt}")

# 每页 25 条是 LinkedIn 的默认值；本适配器最多翻 2 页（克制原则）
MAX_PAGES = 2
PAGE_SIZE = 25

# 选择器集中在此：LinkedIn DOM 变化频繁，改版时只需改这几行。
#
# 注意（2026-10 实测）：登录后 LinkedIn 会切到「新版 UI」，岗位卡片用的是
# div.job-card-container 一套 class（job-card-list__* / artdeco-entity-lockup__*），
# 与访客模式（li.jobs-search-results__list-item / div.base-card / h3.base-search-card__title）
# 完全不同。这里两种都保留、用逗号并列，登录态有效时命中新版，会话失效退回
# 访客时命中旧版，二者互不冲突（不同 UI 世代，class 不会重叠）。
SELECTORS: dict[str, str] = {
    # 登录后新版：div.job-card-container；访客旧版：li.jobs-search-results__list-item 等
    "job_card": "div.job-card-container, li.jobs-search-results__list-item, "
                "div.base-card, div.job-search-card",
    "job_link": "a.job-card-list__title--link, a.base-card__full-link, "
                "a.job-search-card__link",
    # 标题就是 a.job-card-list__title--link（新版，href 形如 /jobs/view/4476277417/）
    # 或 h3.base-search-card__title（旧版）。原来的 span.sr-only 是站内无障碍标签
    # （如导航 logo 的 "LinkedIn"），会造成标题串位，已移除。
    "title": "a.job-card-list__title--link, h3.base-search-card__title",
    "company": "div.artdeco-entity-lockup__subtitle, "
               "h4.base-search-card__subtitle a, a.job-search-card__subtitle-link, "
               "h4.base-search-card__subtitle",
    "location": "ul.job-card-container__metadata-wrapper li, span.job-search-card__location",
    # 新版发布时间在卡片页脚 ul.job-card-list__footer-wrapper li（如「25 分钟前」），
    # 旧版在 time[datetime]。.first 取第一个命中即可。
    "posted": "ul.job-card-list__footer-wrapper li, time[datetime], "
              "time.job-search-card__listdate, time",
    "snippet": "p.job-search-card__snippet",
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
        # 重新注入登录凭证。注意：add_cookies() 在 persistent context 上
        # **不会落盘**（实测：注入后重启浏览器即消失），所以必须每次启动重注入。
        # 本文件只在使用 `import_linkedin_cookie.bat`（搬运正常浏览器 Cookie）时
        # 才会生成；若你是用 login_linkedin.bat 在浏览器里手动登录的，登录态由
        # profile 自己持久化（data/browser/linkedin/），此处注入的旧值不会生效。
        session_cookies = load_session()
        if session_cookies:
            self.browser.inject_cookies(self.source_name, session_cookies)
        all_jobs: list[JobPosting] = []
        seen: set[str] = set()
        loc = quote_plus(self.config.location or "Hong Kong")
        limit_hit = False             # 当日配额在途中用尽：保留已抓结果后收手
        # 登录态检查必须等页面真正跳到 LinkedIn 之后再判断：
        # launch() 刚返回的页面是 about:blank，URL 与正文都是空的，
        # ensure_logged_in() 此时必然返回 False → 无论是否已登录都会误报
        # 「未检测到登录态」。因此延迟到第一次 goto 之后再检查，且只检查一次。
        login_checked = False

        for keyword in self.active_keywords():
            # 岗位类型 → LinkedIn f_JT 参数（按关键词动态生成）：
            # 普通岗用 job_types（实习/兼职），毕业生项目关键词用全职（毕业后入职合法）。
            jt_value = build_linkedin_f_jt(self.config.job_types_for_keyword(keyword))
            jt_fragment = f"&f_JT={jt_value}" if jt_value else ""
            for page_no in range(MAX_PAGES):
                url = SEARCH_URL.format(kw=quote_plus(keyword), loc=loc,
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
                # 先判断是否被重定向到登录/注册墙（访客被节流时会跳墙）。
                # 放最前面做快速失败，避免对着空页面白等 15 秒再空耗配额。
                if self._page_is_authwall(page):
                    logger.warning(
                        "[linkedin] 页面被重定向到登录/注册墙：未登录状态下 LinkedIn 会节流访客。\n"
                        "  请运行 `python run.py login linkedin` 用【求职专用小号】登录一次，\n"
                        "  登录态保存在 data/browser/linkedin/，之后自动复用。")
                    break
                # 页面已真正加载，此时判断登录态才有意义（只判断第一次）
                if not login_checked:
                    login_checked = True
                    self._warn_if_not_logged_in(page)
                # 关键：LinkedIn 页面存在长连接（实时推送等），networkidle 几乎
                # 永不触发，直接等它会在 30s 后抛超时异常、拖垮整轮抓取。
                # 改为"等岗位卡片出现"（有结果时），超时也放行交给解析判定。
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
                    break   # 本关键词结果已不足一页，没必要再翻
            if limit_hit:
                break

        logger.info(f"[linkedin] 本轮共解析到 {len(all_jobs)} 个岗位")
        return all_jobs

    # ------------------------------------------------------------------
    def _warn_if_not_logged_in(self, page) -> None:
        """未登录时给出中文指引（不自动登录、不接受账号密码）。

        必须在 page 已经 goto 到 LinkedIn 之后调用：刚 launch 出来的
        页面是 about:blank，此时检查必然误判为「未登录」。
        """
        if self.browser.ensure_logged_in(page, r"linkedin\.com/jobs"):
            return
        logger.warning(
            "[linkedin] 未检测到登录态，将以访客模式抓取（结果可能不完整）。\n"
            "  建议：运行 `python run.py login linkedin` 用【求职专用小号】登录一次，\n"
            "  登录态保存在 data/browser/linkedin/，后续自动复用。"
        )

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
