"""Workday 招聘门户适配器：直接读雇主官网的岗位 JSON 接口。

为什么值得单独做一条链路
------------------------
三大招聘网站（JobsDB / LinkedIn / Indeed）都是「别人转述的岗位」，而 Workday 是
**雇主自己的招聘系统**——岗位从雇主系统里直接出来，信息最全、最新，也没有中间层。

技术上它比另外三个站点都简单，也稳得多：

| | 三大站点 | Workday |
|---|---|---|
| 传输 | 浏览器渲染整页 | 纯 HTTP + JSON |
| 反爬 | Cloudflare 挑战 / 登录墙 / 限流 | 无（公开的岗位检索接口） |
| 发布时间 | Indeed 拿不到、Others 只能解析相对文案 | 接口直接给 |
| 完整 JD | 要开详情页 | 接口直接给 |
| 还在招吗 | 要开详情页猜文案 | 接口直接给布尔值 `canApply` |

最后一行是关键：`canApply` 让我们不用打开任何页面就知道这个岗位还能不能投。
这正好补上「列表页看不到关闭状态」那个天生盲区。

接口形如（并非官方文档，站点改版时需要跟着调）：
    POST {base}/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "data analyst"}
    GET  {base}/wday/cxs/{tenant}/{site}{externalPath}
         → {"jobPostingInfo": {title, jobDescription, postedOn, canApply, ...}}

雇主要自己配：在 data/workday_employers.yaml 里写 tenant / site / base_url。
文件不存在时用内置名单；文件存在时**逐条覆盖**内置值（同名 key 以文件为准），
所以想关掉某一家写 `enabled: false` 即可，不必把内置名单抄一遍。

怎么找某家公司是不是用 Workday
------------------------------
打开它的招聘页，地址栏形如 `https://[tenant].wd[1|3|5].myworkdayjobs.com/[site]`，
三段分别对应下面的 base_url / tenant / site。
探测技巧：tenant 或 site 写错时接口返回 **422**（不是 404），
所以「422」= 这家公司可能用 Workday，但参数没猜对。

三条踩过的坑（改这个文件前务必看）
----------------------------------
1. **筛选参数名每家都不同**，不能写死。实测同一件事在四家叫四个名字：
   `locations` / `locationCountry` / `primaryLocation` / `locationHierarchy2`。
   正确做法是读返回里 facet **内层** group 自带的 `facetParameter`——
   外层那个名字（如 `locationMainGroup`）拿去请求会 400。
2. **`offset` 有效，但第 2 页起 `total` 会莫名变成 0**（不是 0 条，只是总数丢了）。
   所以不能拿 `(页数+1)*20 >= total` 判断收尾，否则永远只拿第一页。
   现在的收尾条件是「整页都是已见过的」——顺带挡住某些站点翻到底后回卷到第一页。
3. **`limit` 上限是 20**，写 21 直接 400（不是截断到 20）。
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Optional

import yaml
from loguru import logger

from hk_job_radar.config import DATA_DIR
from hk_job_radar.models import HK_TZ, JobPosting, now_hk
from hk_job_radar.scrapers.base import BaseScraper, LimitReachedError

#: 用户自己的雇主清单（个人数据，不进 git）。不存在时用下面内置的三家。
EMPLOYERS_FILE = DATA_DIR / "workday_employers.yaml"

#: 内置雇主：2026-10-20 逐个实测通过、且确实挂着香港岗位的机构。
#: 只放「确认可用」的，宁可少也不要在默认配置里塞一堆 422 让用户以为坏了。
#:
#: 前两家（Manulife / Sun Life）是香港强积金与保险市场的大户，
#: 香港数据科学、AI、数据分析岗主要就出在这类机构；后三家是中资/美资银行。
#: 三家银行目前的香港岗位以 VP / Director 级为主，未必有应届能投的坑，
#: 留着是因为用户自己要加的公司大概也是这种「大机构」形态，先给个能跑的样子。
DEFAULT_EMPLOYERS: dict[str, dict[str, str]] = {
    "manulife": {
        "name": "Manulife（宏利）",
        "base_url": "https://manulife.wd3.myworkdayjobs.com",
        "tenant": "manulife",
        "site": "MFCJH_Jobs",
    },
    "sunlife": {
        "name": "Sun Life（永明）",
        "base_url": "https://sunlife.wd3.myworkdayjobs.com",
        "tenant": "sunlife",
        # 注意这是「有经验者」站点；Sun Life 另有校招站点，需要时自己加一条
        "site": "Experienced-Jobs",
    },
    "bofa-apac": {
        "name": "Bank of America（亚太）",
        "base_url": "https://ghr.wd1.myworkdayjobs.com",
        "tenant": "ghr",
        "site": "lateral-apac",
    },
    "hlb": {
        "name": "Hong Leong Bank",
        "base_url": "https://hlb.wd3.myworkdayjobs.com",
        "tenant": "hlb",
        "site": "HLBCareers",
    },
    "cncbi": {
        "name": "中信银行国际",
        "base_url": "https://cncbinternational.wd3.myworkdayjobs.com",
        "tenant": "cncbinternational",
        "site": "CNCBIExternalCareerSite",
    },
}

#: 接口硬限制：limit 超过 20 会直接 400（实测），不是「最多返回 20」而是「报错」。
PAGE_SIZE = 20
#: 单个雇主最多翻几页列表（20 条/页）。上限防的是「某家公司放出几千个岗位」
#: 把一轮抓取拖成半小时；正常公司几十到几百条，12 页已经覆盖 240 条。
MAX_PAGES_PER_EMPLOYER = 12
#: 单个雇主最多取多少条详情。详情 = 1 次请求，用来拿 canApply 与完整 JD。
#: 实测一次详情约 1 秒，所以下面用 6 个线程并发，60 条约 10 秒。
MAX_DETAILS_PER_EMPLOYER = 60
#: 取详情的并发线程数。Workday 没有反爬，6 并发既快又不会给对方压力。
DETAIL_WORKERS = 6
#: 即便 canApply=True，发布超过这么多天的岗位也不再推。
#: 为什么还要有这条：有些雇主把「长期招聘」的 requisition 挂几个月不关，
#: canApply 一直是 true，但推给用户一条三个月前的岗位只会消耗信任。
MAX_VERIFIED_AGE_DAYS = 30
TIMEOUT_SECONDS = 20

#: 明确属于香港的地点关键词。命中任一即视为香港岗位。
#: 注意不要用裸 "hk"：它会出现在 Tashkent 这类地名里，造成误判。
_HK_MARKERS = ("hong kong", "kowloon", "new territories",
               "香港", "九龍", "九龙", "新界")

#: Workday 用「2 Locations」表示这个岗位挂在多个城市，仅凭列表看不出是哪里。
_MULTI_LOCATION = re.compile(r"\d+\s+locations?|multiple locations|多個地點|多个地点", re.I)

#: 「Posted Today / Posted 6 Days Ago / Posted 30+ Days Ago」
_POSTED_RE = re.compile(
    r"posted\s*(?:(?P<today>today)|(?P<yesterday>yesterday)|"
    r"(?P<num>\d+)\+?\s*(?P<unit>day|week|month|hour)s?\s*ago)",
    re.I)


def load_employers() -> dict[str, dict[str, Any]]:
    """内置名单 + 用户文件，合并后返回「key → 雇主配置」。

    合并而不是覆盖整份：用户文件里通常只写他关心的几家，
    若整份替换就会把内置的三家悄悄弄丢——那种「本来有、后来没了」最难排查。
    """
    merged: dict[str, dict[str, Any]] = {
        k: {**v, "enabled": True} for k, v in DEFAULT_EMPLOYERS.items()
    }
    if not EMPLOYERS_FILE.exists():
        return merged
    try:
        raw = yaml.safe_load(EMPLOYERS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"[workday] {EMPLOYERS_FILE.name} 解析失败（改用内置名单）：{exc}")
        return merged

    for key, item in (raw.get("employers") or {}).items():
        if not isinstance(item, dict):
            continue
        base = merged.get(key, {})
        merged[key] = {**base, **{k: v for k, v in item.items() if v is not None}}
    return merged


class _TextExtractor(HTMLParser):
    """把 JD 的 HTML 转成纯文本（岗位正文常带 <p>/<ul>/<br>）。

    不引第三方库：标准库够用，也避免为一个字段多装一个依赖。
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:  # noqa: D102
        self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def strip_html(html: str) -> str:
    """HTML → 纯文本（单段、空白压缩）。解析失败时退化为粗暴去标签。"""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


class WorkdayScraper(BaseScraper):
    """Workday 雇主门户适配器（只读，不投递）。"""

    source_name = "workday"
    # 一次搜索请求 = 一页配额
    pages_per_keyword = 1

    # ------------------------------------------------------------------
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        employers = load_employers()
        enabled = {k: v for k, v in employers.items() if v.get("enabled", True)}
        if not enabled:
            logger.warning("[workday] 没有启用的雇主（检查 data/workday_employers.yaml 的 enabled）")
            return []

        all_jobs: list[JobPosting] = []
        for key, employer in enabled.items():
            try:
                jobs = self._fetch_employer(key, employer, last_seen_cutoff)
            except LimitReachedError:
                # 配额用尽：保留已抓结果交给上层收手（与其他站点同一口径）
                logger.warning(f"[workday] 今日配额已用尽：保留已抓到的 {len(all_jobs)} 个岗位。")
                raise
            except Exception as exc:
                # 单家失败绝不影响其他家：公司清单里混进一个写错的 tenant 是常态
                logger.warning(f"[workday] {employer.get('name', key)} 抓取失败（跳过这家）：{exc}")
                continue
            logger.info(f"[workday] {employer.get('name', key)}：{len(jobs)} 个香港岗位")
            all_jobs.extend(jobs)

        logger.info(f"[workday] 本轮共解析到 {len(all_jobs)} 个岗位")
        return all_jobs

    # ------------------------------------------------------------------
    def _fetch_employer(self, key: str, employer: dict,
                        last_seen_cutoff: datetime) -> list[JobPosting]:
        """抓一家公司：列出香港岗位 → 取详情 → 过滤掉已停止收简历的。

        为什么不是「按关键词搜索」：企业门户里香港岗位本来就不多（实测几十条），
        与其为 24 个关键词各发一次搜索请求，不如把香港岗位一次列全，
        再交给本地的关键词初筛去判——请求更少、召回更全。
        """
        hk_facets = self._discover_hk_facets(employer)
        try:
            listing = self._collect_postings(employer, hk_facets)
        except urllib.error.HTTPError as exc:
            # 筛选参数名对不上时接口会 400（实测有的站点这么干）。
            # 与其让整家公司消失，不如不要筛选、把岗位全捞回来本地筛。
            if exc.code != 400 or not hk_facets:
                raise
            logger.debug(f"[workday] {employer.get('name', key)} 不接受筛选参数 "
                         f"{hk_facets[0]}，退回本地筛选")
            listing = self._collect_postings(employer, None)
        if not listing:
            return []

        now = now_hk()
        hard_ceiling = now - timedelta(days=MAX_VERIFIED_AGE_DAYS)
        candidates = []
        for path, item in listing.items():
            if not self._looks_hong_kong(item.get("locationsText") or ""):
                continue
            posted = self._extract_posted_at(item.get("postedOn") or "")
            if posted is not None and posted < hard_ceiling:
                continue          # 挂了太久：就算是长期招聘的 requisition 也不再推
            candidates.append((path, item, posted))

        if len(candidates) > MAX_DETAILS_PER_EMPLOYER:
            # 砍到上限时的取舍顺序：
            #   1. 标题命中关键词的**先保**——它们几乎肯定会过本地初筛，
            #      要是因为「排位靠后」被砍掉，漏的就是最该看到的那条；
            #   2. 其余按发布时间从新到旧补足。
            # 两次稳定排序叠加即可，不用写复合 key（方向相反时很容易写反）。
            keywords = [k.lower() for k in self.config.all_keywords() if k]
            candidates.sort(key=lambda c: c[2] or now, reverse=True)     # 新的在前
            candidates.sort(                                            # 标题命中优先
                key=lambda c: 0 if any(k in (c[1].get("title") or "").lower()
                                       for k in keywords) else 1)
            logger.info(f"[workday] {employer.get('name', key)}：香港候选 "
                        f"{len(candidates)} 个，本轮只取 {MAX_DETAILS_PER_EMPLOYER} 个详情"
                        f"（标题命中的优先，其余按最新）")
            candidates = candidates[:MAX_DETAILS_PER_EMPLOYER]

        details = self._fetch_details(employer, [c[0] for c in candidates])
        jobs: list[JobPosting] = []
        skipped_closed = 0
        for path, item, listed_posted in candidates:
            detail = details.get(path)
            if detail is None:
                continue
            if detail.get("canApply") is False:
                # 这就是「已过期」的官方答案，不用打开页面猜文案
                skipped_closed += 1
                continue
            job = self._to_job(key, employer, item, detail, path, listed_posted)
            if job is not None:
                jobs.append(job)
        if skipped_closed:
            logger.info(f"[workday] {employer.get('name', key)}："
                        f"{skipped_closed} 个岗位已停止接收申请，已跳过")
        return jobs

    # ------------------------------------------------------------------
    # 列表获取
    # ------------------------------------------------------------------
    def _discover_hk_facets(self, employer: dict) -> Optional[tuple[str, list[str]]]:
        """找出「香港」在筛选参数里怎么传：返回 (参数名, [香港的 id...])。

        为什么参数名要动态读：实测同一件事四家四个名字——`locations` /
        `locationCountry` / `primaryLocation` / `locationHierarchy2`，写死一个
        就只能在那一家用。名字藏在 facet 内层 group 的 `facetParameter` 里，
        外层（如 `locationMainGroup`）拿去请求会 400。

        为什么可能返回多个 id：同一个「Hong Kong」会被拆成几条
        （如「Hong Kong」和「Hong Kong, Central and Western」），
        只取第一条会漏掉另一批岗位。

        找不到就返回 None，退回本地筛选——不支持筛选的站点照样能用。
        """
        try:
            data = self._search(employer, applied_facets={}, offset=0)
        except Exception as exc:
            logger.debug(f"[workday] 读取筛选参数失败（退回本地筛选）：{exc}")
            return None
        for facet in data.get("facets") or []:
            for group in facet.get("values") or []:
                if not isinstance(group, dict):
                    continue
                param = group.get("facetParameter") or facet.get("facetParameter")
                if not param:
                    continue
                ids = [str(v["id"]) for v in (group.get("values") or [])
                       if isinstance(v, dict) and v.get("id")
                       and self._has_hk_marker(v.get("descriptor") or "")]
                if ids:
                    return str(param), ids
        return None

    def _collect_postings(self, employer: dict,
                          hk_facets: Optional[tuple[str, list[str]]]) -> dict[str, dict]:
        """翻页收集岗位列表（externalPath → 列表项）。能筛选时只要香港的。

        翻页收尾有两个坑（都实测过）：
        - 第 2 页起 `total` 会变成 0，所以不能拿 total 算收尾，只能记第一页的；
        - 翻到底之后有的站点会**回卷到第一页**继续返回数据（offset 越界不报错），
          所以真正可靠的收尾信号是「这一页的岗位全都已经收过了」。
        """
        applied = {hk_facets[0]: hk_facets[1]} if hk_facets else {}
        collected: dict[str, dict] = {}
        first_total = 0
        for page in range(MAX_PAGES_PER_EMPLOYER):
            self._count_page()
            data = self._search(employer, applied_facets=applied,
                                offset=page * PAGE_SIZE)
            items = data.get("jobPostings") or []
            if not items:
                break
            fresh = [it for it in items
                     if it.get("externalPath") and it["externalPath"] not in collected]
            if not fresh:
                break                 # 整页都是拿过的 = 已经回卷到第一页了
            if page == 0:
                first_total = int(data.get("total") or 0)
            for it in fresh:
                collected[it["externalPath"]] = it
            if first_total and len(collected) >= first_total:
                break
        return collected

    def _fetch_details(self, employer: dict,
                       paths: list[str]) -> dict[str, Optional[dict]]:
        """并发取详情（每个 1 次请求，实测一次约 1 秒）。

        为什么要并发：一家公司的香港候选常有五六十条，串行要一分钟；
        6 个线程压到十秒左右。Workday 是公开的岗位接口、没有反爬，
        6 并发既快又谈不上给对方压力。
        """
        if not paths:
            return {}
        if len(paths) == 1:
            return {paths[0]: self._detail(employer, paths[0])}
        results: dict[str, Optional[dict]] = {}
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            futures = {pool.submit(self._detail, employer, p): p for p in paths}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    results[path] = fut.result()
                except Exception:      # _detail 内部已兜过，这里再兜一层
                    results[path] = None
        return results

    def _request(self, url: str, payload: dict | None = None) -> dict:
        """发一次请求并解析 JSON。payload 为 None 时是 GET，否则 POST。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # 固定英文：发布时间文案是按 locale 返回的，中文下会变成「3 天前」，
        # 多一套文案就多一处解析会坏的地方。
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        req.add_header("User-Agent",
                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                raise ValueError("tenant / site 参数不对（接口返回 422），"
                                 "请核对 data/workday_employers.yaml") from exc
            raise

    def _search(self, employer: dict, applied_facets: dict, offset: int = 0,
                keyword: str = "") -> dict:
        """调一次岗位检索。keyword 留空 = 不按文字筛，只按 facet（地点等）筛。"""
        url = (f"{employer['base_url']}/wday/cxs/{employer['tenant']}"
               f"/{employer['site']}/jobs")
        return self._request(url, {
            "appliedFacets": applied_facets,
            "limit": PAGE_SIZE,     # >20 会 400，别改
            "offset": offset,
            "searchText": keyword,
        })

    def _detail(self, employer: dict, external_path: str) -> Optional[dict]:
        url = (f"{employer['base_url']}/wday/cxs/{employer['tenant']}"
               f"/{employer['site']}{external_path}")
        try:
            return (self._request(url) or {}).get("jobPostingInfo")
        except Exception as exc:
            # 单个岗位详情失败不该拖垮整家：最多就是这条不推
            logger.debug(f"[workday] 取详情失败（跳过该岗位）{external_path}：{exc}")
            return None

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @staticmethod
    def _has_hk_marker(text: str) -> bool:
        """文本里有没有明确写出香港（严格版：没有就是没有）。

        给「筛选参数里哪个值是香港」用——那里不能宽容，
        空描述算「可能是香港」会把整个筛选参数认错。
        """
        low = (text or "").lower()
        return any(m in low for m in _HK_MARKERS)

    @classmethod
    def _looks_hong_kong(cls, locations_text: str) -> bool:
        """列表页的地点文案能不能判定为香港。

        「2 Locations」这类多地点岗位判断不了，按「可能是」放行——
        宁可多取一次详情，也不要把香港的多地点岗位漏掉。
        """
        low = (locations_text or "").lower()
        if cls._has_hk_marker(low):
            return True
        return bool(_MULTI_LOCATION.search(low)) or not low.strip()

    def _to_job(self, key: str, employer: dict, listing: dict, detail: dict,
                external_path: str, listed_posted: Optional[datetime] = None
                ) -> Optional[JobPosting]:
        title = (detail.get("title") or listing.get("title") or "").strip()
        if not title:
            return None

        # 详情里的 location 比列表页更准；多地点岗位只有详情能定下来
        location = (detail.get("location") or listing.get("locationsText") or "").strip()
        if location and not self._looks_hong_kong(location):
            return None

        # 岗位 ID：雇主前缀 + Workday 内部编号。加前缀是因为多个雇主共用
        # source="workday"，两侧的 reqId 完全可能撞号。
        req_id = str(detail.get("jobReqId") or detail.get("jobPostingId") or "").strip()
        if not req_id:
            req_id = hashlib.sha1(external_path.encode("utf-8")).hexdigest()[:16]
        external_id = f"{key}-{req_id}"

        url = (detail.get("externalUrl") or "").strip()
        if not url.startswith("http"):
            url = f"{employer['base_url']}/en-US/{employer['site']}{external_path}"

        posted_at = (self._parse_date_field(detail.get("startDate"))
                     or listed_posted
                     or self._extract_posted_at(detail.get("postedOn") or ""))

        return JobPosting(
            source=self.source_name,
            external_id=external_id,
            title=title,
            company=employer.get("name") or key,
            url=url,
            url_normalized=self.normalize_url(url),
            salary_text="",           # Workday 的岗位检索接口不返回薪资
            location=location or "Hong Kong",
            posted_at=posted_at,
            description_snippet=self.snippet(strip_html(detail.get("jobDescription") or "")),
            keyword_matched="",       # 关键词由本地初筛统一判定
            direct_apply=True,        # 就是雇主官网，天然是直投
            # 接口已明确说「还能申请」——允许它跳过「岗位新鲜度」那道年龄闸门。
            # 这正是这个源存在的意义：能确认还在招的岗位，不该因为挂得久就被扔掉。
            verified_open=True,
        )

    # ------------------------------------------------------------------
    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        """Workday 的发布时间文案 → 香港时区绝对时间。

        实测只有四种形态：Posted Today / Posted Yesterday / Posted N Days Ago /
        Posted 30+ Days Ago。解析不出来返回 None（宁可让岗位带着 None 进流水线，
        也不因为一个文案变化就把它丢掉）。
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        low = raw.lower()
        now = now_hk()
        m = _POSTED_RE.search(low)
        if not m:
            return None
        if m.group("today"):
            return now
        if m.group("yesterday"):
            return now - timedelta(days=1)
        num = int(m.group("num"))
        unit = m.group("unit")
        if unit == "hour":
            return now - timedelta(hours=num)
        if unit == "day":
            return now - timedelta(days=num)
        if unit == "week":
            return now - timedelta(weeks=num)
        return now - timedelta(days=30 * num)

    @staticmethod
    def _parse_date_field(raw: Any) -> Optional[datetime]:
        """详情里的 startDate（形如 "2026-10-20"）→ 当天零点的香港时间。

        为什么优先用它：相对文案（Posted Today）没有具体时刻，
        而同一天发的岗位可能已经挂了 20 小时——日期至少是准确的。
        """
        if not raw:
            return None
        text = str(raw).strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(text, fmt)
                return (dt.replace(tzinfo=HK_TZ) if dt.tzinfo is None
                        else dt.astimezone(HK_TZ))
            except ValueError:
                continue
        # 只保留日期部分的兜底（有些站点会返回带时间的 ISO）
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=HK_TZ)
        return None
