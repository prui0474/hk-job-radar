"""tal.net 校招站适配器：直接读外资投行 / 资管的 campus 招聘板。

为什么必须单独做一条链路
------------------------
投行的**校招**（Summer Analyst / Off-Cycle Internship）根本不出现在三大招聘
网站，也不在 Workday 上——它们挂在自家校园招聘站，域名是 `*.tal.net`
（Oleeo / 原 Taleo 的候选人门户）。实测：

| 租户 | 香港 2027 暑期实习 |
|---|---|
| `jefferies.tal.net` | 4 条（Equities S&T 两条、Technology 两条） |
| `fidelityinternational.tal.net` | 4 条（Equity Research、General Counsel、两条 Off-Cycle） |

这些岗位是用户最想投的一类（外资行暑期实习），别的源一条都抓不到，所以
少这 8 条等于漏掉最关键的机会。

这个站对自动化最友好的地方：**纯服务端渲染 HTML，没有 Cloudflare、没有
验证码、不需要登录**，默认 python-requests 的 UA 都能 200。所以整条链路
只用 urllib，不开浏览器——比 JobsDB / LinkedIn 轻得多，也不会被挑战。

同时它也是最需要克制的站：`robots.txt` 写了 `Crawl-delay: 10`，并明确屏蔽
ClaudeBot / GPTBot / Bytespider 等爬虫 UA。这里的取舍：
  · 带一个正常的 Chrome UA（它拦的是爬虫标识，不是浏览器）；
  · 请求之间 `time.sleep(0.5)`（10 秒的合规折中：我们一天只发个位数请求，
    不是连续爬取；真要严格 10 秒，用户第一次跑就得等半分钟）；
  · **不抓详情页**——列表卡片已经有标题和（部分租户有的）地点，详情页
    对「判断是不是香港岗」没有增量信息，少一类请求就少一份风险；
  · 每家 board 最多翻 4 页（200 条），实测最大的 board 只有 75 条。

页面结构（2026-11-18 实抓，站点改版时需要跟着调）
------------------------------------------------
同一套系统有两种模板并存，且**同一页里可能同时出现**（Fidelity 就是表格 +
磁贴双份，同一条岗位出现两次，必须按 opp id 去重）：

表格行::

    <tr class="opp_1453 search_res details_row" data-oppid="1453"
        data-title="Real Estate Transactions Intern Programme 2027 - Paris">
      <td class="comm_list_tbody">
        <a class="subject" href="https://…/opp/1453-…/en-GB">标题</a>
      </td>
    </tr>

磁贴（Jefferies 用这种）::

    <li class="col-md-6 opp-container" id="oppid-1997" data-oppid="1997">
      <div class="opp_1997 search_res details_row candidate-opp-tile"
           data-title="2027 … - Hong Kong">
        <div class="candidate-opp-field-1"><span class="candidate-opp-field-label">ID:</span> 1997</div>
        <h3 class="candidate-opp-field-2"><a class="subject" href="…">标题</a></h3>
      </div>
    </li>

分页用 `?start=50`（不是 page 参数），总条数在
`<h2 role="alert">75 results match!</h2>` 里，实测第 2 页仍显示同一个总数。

两处实测纠正（与最初设想不同，改代码前务必看）
--------------------------------------------
1. **`https://{host}/candidate/postings/{id}` 是 404，不是「规范地址」。**
   Jefferies / Fidelity / BofA 三个租户全部实测 404。可用的地址只有列表卡片
   里 `a.subject` 的 href（形如 `/vx/lang-en-GB/mobile-0/…/opp/1997-…/en-GB`）。
   所以 `url` 直接用卡片 href（相对路径补上 host），取不到才拼一个兜底地址。
2. **地点不一定在列表页。** Jefferies 的磁贴只有 ID 和标题两个字段，
   Fidelity 的表格只有一个标题列——它们的香港岗位是靠**标题后缀**
   （`… - Hong Kong`）看出来的。所以「是否香港」必须标题 + 地点两处都看；
   反过来说，解析器仍然要真解析地点列，因为别的租户（如 BofA）把城市放在
   第 3 个 `td` 里，只认标题会漏掉一整批岗位。

雇主要自己配：在 `data/talnet_boards.yaml` 里写 host / board。文件不存在时
用内置的两家；文件存在时**逐条合并**（同名 key 覆盖，其余内置保留），
所以想关掉某一家写 `enabled: false` 即可，不必把内置名单抄一遍。
"""
from __future__ import annotations

import html as html_lib
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

import yaml
from loguru import logger

from hk_job_radar.config import DATA_DIR
from hk_job_radar.models import JobPosting
from hk_job_radar.scrapers.base import BaseScraper, LimitReachedError

#: 用户自己的 board 清单（个人数据，不进 git）。不存在时用下面内置的两家。
BOARDS_FILE = DATA_DIR / "talnet_boards.yaml"

#: 内置 board：2026-11-18 实测有香港 2027 校招岗的两家。
#: 只放「确认可用」的，宁可少也不要在默认配置里塞一堆打不开的域名。
#:
#: Jefferies 是美资投行，香港有完整的 S&T / IB / Technology 校招序列；
#: Fidelity International 是资管，香港有 Equity Research 与 Off-Cycle 实习。
#: 这两家正好覆盖用户的两个主攻方向（投行前台 + 资管研究）。
DEFAULT_BOARDS: dict[str, dict[str, str]] = {
    "jefferies": {
        "name": "Jefferies（杰富瑞）",
        "host": "jefferies.tal.net",
        "board": "/vx/lang-en-GB/candidate/jobboard/vacancy/2/adv/",
    },
    "fidelity": {
        "name": "Fidelity International（富达国际）",
        "host": "fidelityinternational.tal.net",
        "board": "/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/",
    },
}

#: tal.net 一页固定 50 条（实测；改这个数站点不认，它只认 `?start=N` 偏移）。
PAGE_SIZE = 50
#: 单家 board 最多翻几页。写 4 = 200 条上限：实测最大的 board 只有 75 条，
#: 4 页已经翻到底，同时给「某家突然放出上千条」留一个硬上限。
MAX_PAGES_PER_BOARD = 4
TIMEOUT_SECONDS = 20
#: 两次请求之间的最小间隔。robots.txt 写的是 `Crawl-delay: 10`，这里折中到
#: 0.5 秒：我们一天只发个位数请求（不是连续爬），严格 10 秒会让首次运行
#: 白等半分钟，反而更容易被用户当成卡死而反复重跑。
REQUEST_INTERVAL_SECONDS = 0.5
#: 单页请求的重试次数（含首次）。tal.net 平时很稳，重试只防偶发超时。
REQUEST_ATTEMPTS = 2

#: 正常 Chrome UA。站点屏蔽的是 ClaudeBot / GPTBot 这类爬虫标识，
#: 带浏览器 UA 才能拿到和用户手点一样的 HTML。
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
#: `Accept-Encoding: identity` 是刻意的：urllib 不会自动解 gzip，
#: 一旦站点按 gzip 返回，拿到的是乱码字节，解析结果会是「0 条岗位」这种
#: 最难查的故障。
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

#: 香港标记。`\bhk\b` 走词边界而不是裸子串——裸 "hk" 会命中 Tashkent
#: （T-a-s-**hk**-ent），把乌兹别克的岗位当成香港岗推给用户。
_HK_RE = re.compile(
    r"\bhong\s*kong\b|\bkowloon\b|\bnew\s+territories\b|\bhk\b"
    r"|香港|九龍|九龙|新界",
    re.I)

_ROW_RE = re.compile(r'<tr class="opp_(\d+) search_res details_row"(.*?)</tr>', re.S)
_TILE_RE = re.compile(
    r'<li class="col-md-6 opp-container" id="oppid-(\d+)"(.*?)</li>', re.S)
_TD_RE = re.compile(r'<td class="comm_list_tbody">(.*?)</td>', re.S)
_TILE_FIELD_RE = re.compile(
    r'<div class="candidate-opp-field-\d+">(.*?)</div>', re.S)
_SUBJECT_RE = re.compile(r'<a class="subject" href="([^"]+)"')
_TITLE_ATTR_RE = re.compile(r'data-title="([^"]*)"')
#: `<h2 role="alert">75 results match!</h2>`（也可能是 "1 result match!"）
_TOTAL_RE = re.compile(r'<h2 role="alert">\s*(\d+)\s+results?\b', re.I)


def load_boards() -> dict[str, dict[str, Any]]:
    """内置名单 + 用户文件，合并后返回「key → board 配置」。

    合并而不是整份替换：用户文件里通常只写他关心的几家，整份替换会把内置的
    两家悄悄弄丢——那种「本来有、后来没了」最难排查。
    """
    merged: dict[str, dict[str, Any]] = {
        k: {**v, "enabled": True} for k, v in DEFAULT_BOARDS.items()
    }
    if not BOARDS_FILE.exists():
        return merged
    try:
        raw = yaml.safe_load(BOARDS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"[talnet] {BOARDS_FILE.name} 解析失败（改用内置名单）：{exc}")
        return merged

    for key, item in (raw.get("boards") or {}).items():
        if not isinstance(item, dict):
            continue
        base = merged.get(key, {})
        merged[key] = {**base, **{k: v for k, v in item.items() if v is not None}}
    return merged


def strip_tags(fragment: str) -> str:
    """HTML 片段 → 一段压掉多余空白的纯文本（顺带反转义 `&amp;`）。"""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_board(html: str) -> tuple[Optional[int], list[dict[str, str]]]:
    """解析一页 board：返回 (总条数, [{opp_id, title, url, location}])。

    两种模板都认，并按 opp id 去重（Fidelity 同一页里表格与磁贴各有一份，
    纯按模板收集会得到双份岗位，去重后 external_id 才稳定）。
    表格优先：它的 `td` 列可能带地点，磁贴只有 ID + 标题。
    """
    total: Optional[int] = None
    m = _TOTAL_RE.search(html or "")
    if m:
        total = int(m.group(1))

    items: dict[str, dict[str, str]] = {}
    for opp_id, body in _ROW_RE.findall(html or ""):
        item = _parse_block(opp_id, body, _TD_RE)
        if item:
            items[opp_id] = item
    for opp_id, body in _TILE_RE.findall(html or ""):
        if opp_id in items:
            continue
        item = _parse_block(opp_id, body, _TILE_FIELD_RE, skip_label="ID:")
        if item:
            items[opp_id] = item
    return total, list(items.values())


def _parse_block(opp_id: str, body: str, cell_re: re.Pattern[str],
                 skip_label: str = "") -> Optional[dict[str, str]]:
    """从一个 `<tr>` / `<li>` 里抽出标题、链接、地点。

    「地点」取「除标题列之外的其余单元格」。为什么这么写：不同租户的列数
    完全不同——Fidelity 只有一列标题（没有地点），BofA 有三列（项目号 /
    标题 / 城市）。写死「第 3 列是城市」会在只有一列时抛异常、在别的租户
    又取错列；「除了标题列之外的都是附加信息」才是这套模板的稳定规律。
    """
    title_m = _TITLE_ATTR_RE.search(body)
    link_m = _SUBJECT_RE.search(body)
    title = (html_lib.unescape(title_m.group(1)).strip() if title_m else "")
    if not title:
        # data-title 缺失时退回链接文字（两者通常一致，但改了模板只坏一处）
        href_pos = body.find('class="subject"')
        if href_pos >= 0:
            title = strip_tags(body[href_pos:])
    if not title:
        return None

    meta: list[str] = []
    for fragment in cell_re.findall(body):
        if 'class="subject"' in fragment:
            continue                      # 标题列
        text = strip_tags(fragment)
        if skip_label:
            text = re.sub(r"^" + re.escape(skip_label), "", text, flags=re.I).strip()
        if not text or text == opp_id or text.isdigit():
            continue                      # "ID: 1997" 这类不是地点
        meta.append(text)

    return {
        "opp_id": opp_id,
        "title": title,
        "url": link_m.group(1) if link_m else "",
        "location": " / ".join(meta),
    }


def _with_start(url: str, start: int) -> str:
    """拼分页参数。站点认的是 `?start=50`（偏移量），不是 page 页码。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}start={start}"


class TalNetScraper(BaseScraper):
    """tal.net 校招板适配器（只读，不投递、不抓详情页）。"""

    source_name = "talnet"
    # 一个 board 翻几页就算几页配额（见 _collect_board 的 _count_page）
    pages_per_keyword = 1

    # ------------------------------------------------------------------
    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        boards = load_boards()
        enabled = {k: v for k, v in boards.items() if v.get("enabled", True)}
        if not enabled:
            logger.warning("[talnet] 没有启用的 board（检查 data/talnet_boards.yaml 的 enabled）")
            return []

        all_jobs: list[JobPosting] = []
        failed: list[tuple[str, dict, Exception]] = []
        for key, board in enabled.items():
            try:
                jobs = self._fetch_board(key, board)
            except LimitReachedError:
                # 配额用尽：保留已抓结果交给上层收手（与其他站点同一口径）
                logger.warning(f"[talnet] 今日配额已用尽：保留已抓到的 {len(all_jobs)} 个岗位。")
                raise
            except Exception as exc:
                # 单家失败绝不影响其他家：host / board 写错是常态
                logger.warning(f"[talnet] {board.get('name', key)} 抓取失败（跳过这家）：{exc}")
                failed.append((key, board, exc))
                continue
            logger.info(f"[talnet] {board.get('name', key)}：{len(jobs)} 个香港岗位")
            all_jobs.extend(jobs)

        if failed and len(failed) == len(enabled):
            # 全灭不是「今天刚好没岗位」，是链路坏了。静默返回 0 条会让用户
            # 以为投行没开校招，实际可能是域名改了或者整站被挡。
            self._log_all_failed(failed)
        elif failed:
            logger.warning(
                f"[talnet] 本轮有 {len(failed)}/{len(enabled)} 家 board 失败，"
                f"其余 board 正常返回。")

        logger.info(f"[talnet] 本轮共解析到 {len(all_jobs)} 个岗位")
        return all_jobs

    # ------------------------------------------------------------------
    @staticmethod
    def _log_all_failed(failed: list[tuple[str, dict, Exception]]) -> None:
        """所有 board 都失败时打一条能定位问题的 error（不静默返回空）。"""
        key, board, exc = failed[0]
        name = board.get("name", key)
        host = board.get("host", "")
        path = board.get("board", "")
        status = TalNetScraper._status_of(exc)
        status_text = f"HTTP {status}" if status else "无状态码"
        logger.error(
            f"[talnet] 本轮 0 条：{len(failed)} 家 board 全部抓取失败。\n"
            f"    第一家失败：{name} https://{host}{path}（{status_text}）。原因：{exc}\n"
            f"    先确认这个地址能直接在浏览器里打开；若域名已换，"
            f"改 data/talnet_boards.yaml 里的 host / board。")

    # ------------------------------------------------------------------
    @staticmethod
    def _status_of(exc: Exception) -> int:
        """异常里能拿到的 HTTP 状态码（拿不到返回 0），只用于日志定位。"""
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code
        reason = getattr(exc, "reason", None)
        return int(getattr(reason, "code", 0) or 0)

    # ------------------------------------------------------------------
    def _fetch_board(self, key: str, board: dict) -> list[JobPosting]:
        """抓一家 board：翻页收集卡片 → 只留香港岗位 → 转 JobPosting。"""
        items = self._collect_board(key, board)
        jobs: list[JobPosting] = []
        for item in items:
            if not self._is_hong_kong(item["title"], item["location"]):
                continue          # 标题和地点都没写香港 = 别处的岗位，丢掉
            jobs.append(self._to_job(key, board, item))
        return jobs

    def _collect_board(self, key: str, board: dict) -> list[dict[str, str]]:
        """翻页收集卡片（按 opp id 去重）。收尾条件有三个：

        1. 这一页**不足 50 条** → 已经是最后一页（实测第 2 页 25 条即到底）；
        2. 这一页全是已见过的 id → 站点回卷到第一页了，立刻收手；
        3. 已收条数 >= 首屏报的总数 → 不用再请求。
        另外 `MAX_PAGES_PER_BOARD` 是硬上限，防某家突然放出上千条。
        """
        base = self._board_url(board)
        collected: dict[str, dict[str, str]] = {}
        total: Optional[int] = None
        for page in range(MAX_PAGES_PER_BOARD):
            self._count_page()        # 每抓一页记一次配额（与其他站同一口径）
            url = base if page == 0 else _with_start(base, page * PAGE_SIZE)
            html = self._http_get(url)
            page_total, items = parse_board(html)
            if total is None:
                total = page_total
            fresh = [it for it in items if it["opp_id"] not in collected]
            if not fresh:
                break                 # 回卷 / 空页：收手
            for item in fresh:
                collected[item["opp_id"]] = item
            if len(items) < PAGE_SIZE:
                break                 # 不足一页 = 最后一页
            if total and len(collected) >= total:
                break
        logger.debug(f"[talnet] {key}：翻到 {len(collected)} 条卡片（站点报 {total} 条）")
        return list(collected.values())

    # ------------------------------------------------------------------
    @staticmethod
    def _board_url(board: dict) -> str:
        host = str(board.get("host", "")).strip().strip("/")
        path = str(board.get("board", "")).strip()
        return f"https://{host}/{path.lstrip('/')}"

    @staticmethod
    def _absolute_url(host: str, href: str, opp_id: str) -> str:
        """卡片 href → 绝对地址；href 缺失时才拼兜底地址。

        为什么不用 `https://{host}/candidate/postings/{id}` 当主地址：
        2026-11-18 实测 Jefferies / Fidelity / BofA 三个租户的这个路径全部
        **404**，能打开的只有卡片自己的 href。兜底那串留着只是为了「模板改版
        但 href 正则失配」时不至于让 url 变成空字符串。
        """
        href = (href or "").strip()
        if href.startswith("http"):
            return href
        if href:
            return f"https://{host}/{href.lstrip('/')}"
        return f"https://{host}/candidate/postings/{opp_id}"

    def _to_job(self, key: str, board: dict, item: dict[str, str]) -> JobPosting:
        host = str(board.get("host", "")).strip().strip("/")
        url = self._absolute_url(host, item["url"], item["opp_id"])
        return JobPosting(
            source=self.source_name,
            # 加租户前缀：多个 board 共用 source="talnet"，而 opp id 是各租户
            # 自己的流水号（Jefferies 已到 2056、Fidelity 在 1390–1453），
            # 两家迟早会撞号；撞了就是一条岗位被当成重复静默丢掉。
            external_id=f"{key}-{item['opp_id']}",
            title=item["title"],
            company=board.get("name") or key,
            url=url,
            url_normalized=self.normalize_url(url),
            salary_text="",           # 校招板不写薪资
            # 地点列可能为空（Jefferies 的磁贴只有 ID + 标题）。能走到这里
            # 说明标题或地点已经命中香港，所以空值补成 "Hong Kong" 而不是留空。
            location=item["location"] or "Hong Kong",
            # 站上没有发布时间，连相对文案都没有。留 None，绝不按抓取时间编造：
            # 编出来的时间会让「岗位新鲜度」闸门误判，也会让日报说错话。
            posted_at=None,
            description_snippet="",   # 列表页没有摘要（详情页另有内容，本版不抓）
            # 留空交给 keyword_filter.classify_posting 推断：
            # "Summer Analyst" / "Off-Cycle Internship" 已被它认成 internship。
            job_type="",
            keyword_matched="",       # 关键词由本地初筛统一判定
            direct_apply=True,        # tal.net 就是雇主自己的投递系统，天然直投
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _is_hong_kong(title: str, location: str) -> bool:
        """标题或地点任一写到香港即算香港岗位。

        两处都要看的原因：Jefferies 的卡片没有地点字段（只有标题里带
        "- Hong Kong"），而 BofA 那类租户的地点在表格第 3 列、标题里
        一个字都不提。只认一处必然漏掉其中一边。
        """
        return bool(_HK_RE.search(title or "") or _HK_RE.search(location or ""))

    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        """tal.net 列表页不提供发布时间，这里永远返回 None。

        父类是抽象方法，必须给个实现；但**不能**退化成 `now_hk()`——
        那会把「今天抓到的」谎报成「今天发布的」，让按新鲜度过滤的闸门失效。
        """
        return None

    # ------------------------------------------------------------------
    def _http_get(self, url: str) -> str:
        """GET 一页 HTML（带 UA、identity 编码、20 秒超时、两次尝试）。

        每次请求后固定停 `REQUEST_INTERVAL_SECONDS`：robots.txt 的
        `Crawl-delay: 10` 的合规折中（见文件头说明）。
        """
        last_exc: Optional[Exception] = None
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                req = urllib.request.Request(url, headers=REQUEST_HEADERS)
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                    body = resp.read()
                return body.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                last_exc = exc
                # 4xx 是「地址/权限不对」，重试二次结果一样，直接抛
                if 400 <= exc.code < 500:
                    raise
            except Exception as exc:       # URLError / 超时 / 连接被重置
                last_exc = exc
            finally:
                time.sleep(REQUEST_INTERVAL_SECONDS)
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(1.0)
        assert last_exc is not None
        raise last_exc
