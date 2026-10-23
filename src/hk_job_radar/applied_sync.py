"""从招聘网站的「已投列表」回填工作台状态（P3.5）。

为什么需要它
------------
工作台上的「已投」按钮得靠手点，但没人会记得每条都点一遍。招聘网站自己
记着你投过什么（JobsDB 的 Activity → Applied），把这个列表读回来，就能一次
补全：工作台的进度页、投递漏斗、「近 7 天投递」才有真实数据。

数据从哪来（两条路，优先第一条）
--------------------------------
1. **页面自己发的 GraphQL 请求**：`hk.jobsdb.com/graphql` 的 `appliedJobs`
   一次返回全部记录（`first: 100`），每条带 `job.id`（岗位编号）、
   `job.title`、`job.advertiser.name`、`appliedAt.dateTimeUtc`（精确到秒）。
   我们只是打开页面、听它自己的响应，不构造任何查询。
2. **读页面 DOM**（兜底）：万一接口改版或被拦，退回按卡片文字解析。这条路
   拿不全——列表是虚拟滚动的，同一时刻 DOM 里只有十几张卡（实测站点 94 条，
   滚到底也只能凑出 13 条），所以只当保险，报告里会说明。

边界（必须提前说清，否则用户会对它产生错误期待）
------------------------------------------------
- **只认这个网站上的申请**。从雷达点「打开原链接」跳到公司官网填表投的，
  网站不知道，这里也读不到（`isExternal` 能标出「跳转官网投的」那批）。
- 站点只保留**最近 90 天、最多 100 条**（页面自己写着），更早的查不到。
- 匹配优先用 `jobsdb:<job.id>` 直接对库里的 uid（准）；对不上才退回
  「公司 + 职位名」归一化后**完全相等**，不做模糊匹配——把没投的标成已投，
  比漏标烦得多。
- 匹配不上的一律不动库，只列进报告。
- 不覆盖用户手动标过的「不感兴趣」「已结束」，也不把「约面」改回「已投」。
- 重复跑安全：状态没变就不写历史（`set_board_status` 本身就这么判断）。

它不投递、不点击页面上的任何按钮，只读。唯一会写的只有本地 SQLite。
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from hk_job_radar.config import BROWSER_DIR
from hk_job_radar.db import Database
from hk_job_radar.dedup import NO_KEY, dedupe_key_of
from hk_job_radar.models import HK_TZ, JobPosting, now_hk

#: 抓取的站点（与 scrapers 的 source_key 一致，档案目录也叫这个名字）。
SOURCE = "jobsdb"

#: 「我的投递记录」页。注意是 `applied-jobs` 带连字符——
#: `/my-activity/applied`、`/appliedjobs` 都是 404，实测过。
APPLIED_URL = "https://hk.jobsdb.com/my-activity/applied-jobs"

#: 等页面把 appliedJobs 请求发出来的上限（秒）。正常 4–8 秒就有，45 是保险。
CAPTURE_TIMEOUT_S = 45

#: DOM 兜底路径：站点最多 100 条（约 8 页），多留两页余量。
MAX_PAGES = 10

#: DOM 兜底：每页等多久（客户端渲染，首屏要几秒）。
_PAGE_SETTLE_MS = 5000

#: DOM 兜底：最多滚几轮。
_SCROLL_ROUNDS = 14

#: 这些状态「不比已投更早」，同步时不覆盖。
#: interview / closed 是用户自己推进的，rejected 是他明确否掉的。
_KEEP_STATUS = {"applied", "interview", "closed", "rejected"}


class SyncError(Exception):
    """同步失败：带中文提示，CLI 与工作台都直接显示给用户。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppliedEntry:
    """站点上的一条投递记录。"""

    title: str
    company: str
    job_id: str = ""               # JobsDB 岗位编号（GraphQL 的 job.id）
    location: str = ""
    applied_on: str = ""           # ISO 日期
    applied_at: str = ""           # ISO 时间（UTC，精确到秒）
    external: bool = False         # 跳转到雇主官网投的
    active: bool = True            # 岗位是否还在招

    @property
    def uid(self) -> str:
        """库里的 job_uid 形式。有编号时优先用它，这是最可靠的对应关系。"""
        return f"{SOURCE}:{self.job_id}" if self.job_id else ""

    @property
    def key(self) -> str:
        """与跨站去重同一套归一化：剥法律后缀、招聘编号、括号、NFKC。"""
        return dedupe_key_of(self.company, self.title)

    @property
    def when(self) -> datetime | None:
        """投递时刻（带时区）。解析不出来就返回 None，调用方退回「现在」。"""
        if not self.applied_at:
            return None
        try:
            parsed = datetime.fromisoformat(self.applied_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(HK_TZ)

    def brief(self) -> str:
        when = self.applied_on or "日期未知"
        extra = "，跳转官网" if self.external else ""
        return f"{self.title} — {self.company}（{when}{extra}）"


@dataclass
class SyncReport:
    """一次同步的结果。字段命名按「用户读得懂的」来，不按数据库来。"""

    pages: int = 0
    found: int = 0
    via: str = ""                                   # "graphql" / "dom"
    marked: list[tuple[str, AppliedEntry]] = field(default_factory=list)
    already: int = 0
    kept: list[tuple[str, str, AppliedEntry]] = field(default_factory=list)
    unmatched: list[AppliedEntry] = field(default_factory=list)
    backfilled: list[tuple[str, AppliedEntry]] = field(default_factory=list)

    def summary(self) -> str:
        if self.via == "graphql":
            parts = [f"站点上有 {self.found} 条投递记录。"]
        elif self.pages:
            parts = [f"读了 {self.pages} 页，站点上有 {self.found} 条投递记录。"]
        else:
            parts = [f"站点上有 {self.found} 条投递记录。"]
        if self.marked:
            # 按「岗位」而不是「记录」报数：同一条投递可能命中库里两条
            # 重复抓取的同名岗位，说「条」会让用户对不上账。
            parts.append(f"新标记「已投」{len(self.marked)} 个岗位。")
        if self.backfilled:
            parts.append(
                f"补录了 {len(self.backfilled)} 个雷达当初没抓到的岗位"
                "（只有标题、公司、投递日期，没有岗位描述和分数）。")
        if self.already:
            parts.append(f"本来就是已投、跳过 {self.already} 条。")
        if self.kept:
            parts.append(f"状态比已投更靠后（或已否决），没有覆盖 {len(self.kept)} 条。")
        if self.unmatched:
            parts.append(f"本地库里找不到这些岗位，没动 {len(self.unmatched)} 条。")
        if not (self.marked or self.backfilled or self.already or self.kept
                or self.unmatched):
            parts.append("没有可同步的记录。")
        if self.via == "dom":
            parts.append("（这次是读页面拿到的，可能不全。）")
        return " ".join(parts)

    def detail_lines(self, limit: int = 8) -> list[str]:
        """给人看的分项明细（CLI 打印用；工作台只显示 summary）。"""
        lines: list[str] = []
        if self.kept:
            lines.append("这些岗位你标过更靠后的状态，保留原样：")
            for uid, status, entry in self.kept[:limit]:
                lines.append(f"  · [{status}] {entry.brief()}")
        if self.backfilled:
            lines.append("雷达没抓到、这次补进库的岗位：")
            for _uid, entry in self.backfilled[:limit]:
                lines.append(f"  · {entry.brief()}")
            if len(self.backfilled) > limit:
                lines.append(f"  · …… 还有 {len(self.backfilled) - limit} 条")
        if self.unmatched:
            lines.append("站点上有、本地库里没有（可能已过期被清理，或当初没抓到）：")
            for entry in self.unmatched[:limit]:
                lines.append(f"  · {entry.brief()}")
            if len(self.unmatched) > limit:
                lines.append(f"  · …… 还有 {len(self.unmatched) - limit} 条")
        return lines


# ---------------------------------------------------------------------------
# 主路径：听页面自己的 GraphQL 响应
# ---------------------------------------------------------------------------
def entry_from_node(node: dict[str, Any]) -> AppliedEntry | None:
    """把 GraphQL 返回的一个 node 转成 AppliedEntry。

    真实结构（2026-10 实测）：
        node.job.id / node.job.title / node.job.advertiser.name
        node.job.location.label
        node.appliedAt.dateTimeUtc = "2026-10-21T07:46:51.006Z"
        node.isExternal = 是否跳转雇主官网
        node.isActive   = 岗位是否还在招

    接口返回的 `edges` 每个元素是 `{"node": {...}}`，而字段其实都在 `node`
    里面。传 edge 或 node 都接受，省得调用方每次都要拆一层。
    """
    if isinstance(node, dict) and "job" not in node and isinstance(node.get("node"), dict):
        node = node["node"]
    job = node.get("job") or {}
    job_id = str(job.get("id") or node.get("id") or "").strip()
    title = str(job.get("title") or "").strip()
    company = str((job.get("advertiser") or {}).get("name") or "").strip()
    if not job_id and not (title and company):
        return None
    applied = str((node.get("appliedAt") or {}).get("dateTimeUtc") or "")
    return AppliedEntry(
        title=title,
        company=company,
        job_id=job_id,
        location=str((job.get("location") or {}).get("label") or ""),
        applied_on=applied[:10],
        applied_at=applied,
        external=bool(node.get("isExternal")),
        active=node.get("isActive") is not False,
    )


# ---------------------------------------------------------------------------
# 兜底路径：读页面 DOM
# ---------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}

_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")

#: 卡片上是「字段名一行、值一行」，字段名对屏幕阅读器可见，恰好可以当锚。
_FIELD_LABELS = {"job title": "title", "advertiser": "company", "location": "location"}


def _visible(text: str) -> str:
    """剥掉零宽字符与双向控制符（保留换行/制表）。

    站点会把无障碍文案（"twenty-one October, two thousand and twenty-six"）
    和看不见的 WORD JOINER（U+2060）一起塞进文本节点，不剥掉会把字段读偏。
    """
    return "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cf", "Cc"))


def parse_applied_date(raw: str) -> str:
    """把 `21 Oct 2026` 转成 `2026-10-21`；认不出就返回空串。

    不用 `strptime("%d %b %Y")`：它依赖进程 locale，中文 Windows 上会失败。
    """
    match = _DATE_RE.search(raw or "")
    if not match:
        return ""
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name[:3].lower())
    if not month:
        return ""
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:                 # 例如 31 Feb 这种脏数据
        return ""


def parse_card(text: str) -> AppliedEntry | None:
    """解析一张投递卡。字段缺失（不是投递卡）时返回 None。

    按行扫描而不是正则跨行匹配：标题里万一出现「Location」「Applied on」
    这类词，正则会把后面的字段整段吃掉，行扫描只认「整行等于字段名」。
    """
    lines = [_visible(line).strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]

    got: dict[str, str] = {}
    i = 0
    while i < len(lines):
        low = lines[i].lower()
        if low in _FIELD_LABELS:
            if i + 1 < len(lines):
                got[_FIELD_LABELS[low]] = lines[i + 1]
            i += 2
            continue
        if low.startswith("applied on"):
            got["channel"] = lines[i][len("applied on"):].strip()
            for j in range(i + 1, min(i + 3, len(lines))):
                if _DATE_RE.search(lines[j]):
                    got["applied_on"] = parse_applied_date(lines[j])
                    break
            i += 1
            continue
        i += 1

    title = got.get("title", "").strip()
    company = got.get("company", "").strip()
    if not title or not company:
        return None
    return AppliedEntry(
        title=title,
        company=company,
        location=got.get("location", "").strip(),
        applied_on=got.get("applied_on", ""),
        external="employer" in got.get("channel", "").lower(),
    )


#: 在页面里把每张卡片的纯文本取出来。
#: 卡片的 class 全是编译出来的乱码（`_9fbd6t0` 这类），一改版就失效，
#: 所以用「文字锚点」定位：每张卡必然有一个最内层的「Applied on …」节点，
#: 从它往上找到第一个同时含 Job Title 与 Advertiser 的祖先就是卡片容器。
_CARD_JS = r"""
() => {
  const anchors = [...document.querySelectorAll('span,div')].filter(n =>
      n.children.length === 0 && /^Applied on/i.test((n.textContent || '').trim()));
  const out = [];
  for (const a of anchors) {
    let box = a, text = '';
    for (let i = 0; i < 8 && box.parentElement; i++) {
      box = box.parentElement;
      const t = box.innerText || '';
      // 长度上限防「向上找过头」：再往上就是包含多张卡片的外层容器了。
      if (t.length < 1200 && /Job Title/.test(t) && /Advertiser/.test(t)) { text = t; break; }
    }
    if (text) out.push(text);
  }
  return out;
}
"""

#: 边滚边收：虚拟列表会把视口外的卡片卸载，所以不能只看当前 DOM，
#: 每滚一步就把看到的卡片并入结果，按文本去重。
def _harvest_page(page: Any) -> list[str]:
    """把当前页能看到的卡片文本全部收下（边滚边收）。"""
    seen: dict[str, None] = {}
    for _ in range(_SCROLL_ROUNDS):
        for text in (page.evaluate(_CARD_JS) or []):
            seen.setdefault(text, None)
        info = page.evaluate(
            "() => ({y: Math.round(window.scrollY),"
            " h: document.body.scrollHeight, w: window.innerHeight})") or {}
        if info.get("y", 0) + info.get("w", 0) >= info.get("h", 0) - 6:
            break
        page.evaluate("window.scrollBy(0, 620)")
        page.wait_for_timeout(550)
    for text in (page.evaluate(_CARD_JS) or []):
        seen.setdefault(text, None)
    return list(seen)


def _collect_from_dom(page: Any, max_pages: int = MAX_PAGES,
                      first_loaded: bool = False
                      ) -> tuple[list[AppliedEntry], int]:
    """兜底：逐页读 DOM。拿不全（虚拟列表），但接口挂了也不至于空手。

    `first_loaded=True` 表示第 1 页已经在页面里了（调用方刚导航过来），
    不用再导航一次——省一次整页加载。
    """
    seen: set[str] = set()
    entries: list[AppliedEntry] = []
    pages = 0
    for page_no in range(1, max_pages + 1):
        if not (page_no == 1 and first_loaded):
            url = APPLIED_URL if page_no == 1 else f"{APPLIED_URL}?page={page_no}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:             # noqa: BLE001
                logger.warning(f"打开投递记录第 {page_no} 页失败：{exc}")
                break
            page.wait_for_timeout(_PAGE_SETTLE_MS)
        fresh = 0
        for raw in _harvest_page(page):
            entry = parse_card(raw)
            if entry is None:
                continue
            key = entry.key
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            entries.append(entry)
            fresh += 1
        if fresh == 0:
            # 这页全是旧数据：要么到底了，要么页码越界被回卷到第一页。
            break
        pages = page_no
    return entries, pages


def collect_applied(page: Any, timeout_s: int = CAPTURE_TIMEOUT_S,
                    max_pages: int = MAX_PAGES
                    ) -> tuple[list[AppliedEntry], int, str]:
    """打开投递记录页并读完，返回 (记录列表, 页数, 数据来源)。

    监听必须在 `goto` **之前**挂上：`appliedJobs` 的请求是在页面加载过程中
    发出去的，导航完再挂就永远等不到。所以「注册监听」和「导航」放在同一个
    函数里，不给调用方拆开的机会。
    """
    box: dict[str, Any] = {}

    def on_response(response: Any) -> None:
        if "/graphql" not in (response.url or ""):
            return
        try:
            if response.request.method != "POST":
                return
            payload = response.json()
        except Exception:                    # noqa: BLE001
            return
        viewer = (payload.get("data") or {}).get("viewer") or {}
        edges = (viewer.get("appliedJobs") or {}).get("edges")
        if edges:
            box["edges"] = edges

    page.on("response", on_response)
    page.goto(APPLIED_URL, wait_until="domcontentloaded", timeout=60000)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and "edges" not in box:
        page.wait_for_timeout(500)

    if box.get("edges"):
        entries = [e for e in (entry_from_node(n) for n in box["edges"]) if e]
        if entries:
            return entries, 1, "graphql"
        logger.warning("拿到了 appliedJobs 响应，但里面没有可解析的记录。")

    logger.warning("没等到 appliedJobs 接口响应，改读页面（可能读不全）。")
    page.wait_for_timeout(_PAGE_SETTLE_MS)      # 页面已经开着，等它渲染完
    entries, pages = _collect_from_dom(page, max_pages=max_pages, first_loaded=True)
    return entries, pages, "dom"


# ---------------------------------------------------------------------------
# 浏览器档案
# ---------------------------------------------------------------------------
def has_login(profile: Path) -> bool:
    """档案里有没有 JobsDB 的登录态。

    只读 cookie 的**名字**、不读值：登录后 SEEK 会下发一个
    `auth0.<随机串>.is.authenticated` 标记，游客没有这个 cookie。
    以只读 + immutable 方式打开，避免和正在运行的浏览器抢锁。
    """
    cookies = Path(profile) / "Default" / "Network" / "Cookies"
    if not cookies.exists():
        return False
    try:
        uri = cookies.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM cookies WHERE host_key LIKE '%jobsdb%'"
                " AND name LIKE '%is.authenticated%' LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug(f"读 cookie 库失败（当作未登录）：{exc}")
        return False
    return row is not None


#: 复制档案时丢掉纯缓存目录：它们体积大、与登录无关，而且复制时可能被占用。
_PROFILE_SKIP = ("Cache", "Code Cache", "GPUCache", "Service Worker", "ShaderCache",
                 "DawnCache", "GrShaderCache", "BrowserMetrics", "Crashpad")


def _find_donor_profile(browser_dir: Path) -> Path | None:
    """在同一个父目录下找「别的副本里已登录的 jobsdb 档案」。

    用户换过安装位置时（比如把项目换到新文件夹、或者解压了一份新的），
    登录态还留在旧目录里。与其让他自己翻文件夹复制，不如就近找一找——
    但只认「确实存在且已登录」，不做跨盘全盘搜索。
    """
    try:
        project = browser_dir.parent.parent          # .../hk_job_radar_pro
        siblings = list(project.parent.iterdir())
    except (OSError, IndexError):
        return None
    for sibling in siblings:
        if not sibling.is_dir() or sibling.resolve() == project.resolve():
            continue
        candidate = sibling / "data" / "browser" / SOURCE
        if has_login(candidate):
            return candidate
    return None


def ensure_profile(browser_dir: Path | None = None) -> str:
    """保证 jobsdb 档案存在，返回一句给用户看的说明（无事可做时为空）。

    只负责「能借到就借」：借不到就等真打开页面时由结果说话（未登录会读不到
    任何记录，报告里是 0 条）。
    """
    root = Path(browser_dir) if browser_dir is not None else BROWSER_DIR
    target = root / SOURCE
    if target.exists():
        return ""                      # 已有档案：用不用得上交给页面去说
    donor = _find_donor_profile(root)
    if donor is None:
        return ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(donor, target,
                        ignore=shutil.ignore_patterns(*_PROFILE_SKIP))
    except OSError as exc:
        logger.warning(f"复制浏览器登录状态失败：{exc}")
        return ""
    where = donor.parent.parent.parent.name
    return f"已从「{where}」复制浏览器登录状态（只复制一次，之后各用各的）。"


# ---------------------------------------------------------------------------
# 写库
# ---------------------------------------------------------------------------
def _library_index(db: Database) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """建两个索引：uid → 行（最准），归一化「公司+职位」→ 行（兜底）。"""
    try:
        rows = db.conn.execute(
            "SELECT job_uid, company, title, status, source FROM jobs").fetchall()
    except sqlite3.Error as exc:
        raise SyncError(f"读本地库失败：{exc}") from exc
    by_uid: dict[str, list[Any]] = {}
    by_key: dict[str, list[Any]] = {}
    for row in rows:
        by_uid.setdefault(str(row["job_uid"]), []).append(row)
        key = dedupe_key_of(row["company"], row["title"])
        if key and key != NO_KEY:
            by_key.setdefault(key, []).append(row)
    return by_uid, by_key


def _history_note(entry: AppliedEntry) -> str:
    """写进状态历史的备注：来源 + **真实投递日期**。

    日期是关键——进度页的「近 7 天/30 天投递」按这个算，比「同步那天的日期」准。
    """
    when = entry.applied_on or "日期未知"
    if entry.external:
        return f"JobsDB 已投 {when}（跳转雇主官网）"
    return f"JobsDB 已投 {when}"


def _backfill_note(entry: AppliedEntry) -> str:
    """补录那批的备注：除了日期，还要写明「这条不是雷达抓到的」。

    用户日后翻进度页看到一条没描述、没分数的岗位时，备注能解释清楚它
    是怎么来的——否则会以为是抓取出了问题。
    """
    return f"{_history_note(entry)}（补录：雷达没抓到这条）"


def _backfill_entry(db: Database, entry: AppliedEntry) -> bool:
    """把一条「站点上有、库里没有」的投递记录补进库，返回是否成功。

    只补**站点给了岗位编号**的那种：没有编号拼不出 `/job/{id}` 链接，
    补进来是一行没有链接、没有描述的空壳，看板上点了也打不开。

    补进去的行长这样：有标题、公司、地点、链接、真实投递日期，**没有**
    岗位描述（站点列表页不给）和分数（不会为了补录去逐条调模型）。
    代价是这些卡片显示「未打分」——诚实地空着，好过编一个分数。

    `first_seen_at` 用投递日期而不是现在：这是它在这场求职里「出现」的
    时间点，拿来做年龄排序才有意义；用现在会让 30 天前投的岗位看着像新的。
    """
    if not entry.job_id:
        return False
    url = f"https://hk.jobsdb.com/job/{entry.job_id}"
    when = entry.when or now_hk()
    job = JobPosting(
        source=SOURCE,
        external_id=entry.job_id,
        title=entry.title or "（职位名未提供）",
        company=entry.company or "（公司名未提供）",
        url=url,
        url_normalized=url,
        location=entry.location,
        posted_at=None,                 # 列表页不给发布时间，别编
        description_snippet="",
        keyword_matched="",
        llm_score=None,                 # 不打分，卡片会显示「未打分」
        llm_reason="",
        status="new",                   # 先插 new，再走 set_board_status 留痕
        first_seen_at=when,
        dedupe_key=entry.key if entry.key != NO_KEY else "",
    )
    if not db.upsert_job(job):
        return False
    # 顺序很重要：先掐掉「进日报」的可能，再改成已投。
    # get_unreported_jobs 只看 reported，不看 status——漏了这一步，下一份
    # 日报会冒出一批「未打分、而且早就投过」的岗位。
    db.suppress_from_report(entry.uid)
    return db.set_board_status(entry.uid, "applied",
                               note=_backfill_note(entry), when=entry.when)


def apply_entries(db: Database, entries: list[AppliedEntry], report: SyncReport,
                  *, dry_run: bool = False, backfill: bool = False) -> None:
    """把投递记录落到库里。匹配不上的默认只记进报告，不动任何行。

    `backfill=True` 时，匹配不上但带岗位编号的记录会被**补进库**（见
    `_backfill_entry`）。默认关：往库里凭空添几十行是用户该自己决定的事，
    不能因为点了「同步已投」就悄悄发生。
    """
    by_uid, by_key = _library_index(db)
    for entry in entries:
        rows = by_uid.get(entry.uid, []) if entry.uid else []
        if not rows:
            # 编号对不上才退回文字匹配。只认同站的行：这个岗位在 LinkedIn 上
            # 也抓到过，但那次并不是你在 JobsDB 投的——标它就是凭空造数据。
            key = entry.key
            rows = [r for r in by_key.get(key, []) if str(r["source"]) == SOURCE] \
                if key and key != NO_KEY else []
        if not rows:
            if backfill and entry.job_id:
                if dry_run or _backfill_entry(db, entry):
                    report.backfilled.append((entry.uid, entry))
                    continue
            report.unmatched.append(entry)
            continue
        for row in rows:
            uid = str(row["job_uid"])
            status = str(row["status"] or "new")
            if status in _KEEP_STATUS:
                if status == "applied":
                    report.already += 1
                else:
                    report.kept.append((uid, status, entry))
                continue
            if dry_run or db.set_board_status(uid, "applied",
                                              note=_history_note(entry),
                                              when=entry.when):
                report.marked.append((uid, entry))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def sync(db: Database, cfg: Any, *, headless: bool | None = None,
         max_pages: int = MAX_PAGES, dry_run: bool = False,
         timeout_s: int = CAPTURE_TIMEOUT_S,
         backfill: bool = False) -> SyncReport:
    """读已投列表并回填状态。`dry_run=True` 只算不写（先看报告再决定）。

    会启动一个真实浏览器（用你已登录的档案）。`headless=None` 时跟随
    `config.yaml` 的 `browser_headless`——默认有界面，方便看见它在干什么，
    也更容易过 Cloudflare。
    """
    note = ensure_profile()
    if note:
        logger.info(note)

    # 延迟导入：`run.py stats` 这类纯本地命令不该因为没装 playwright 就起不来。
    from hk_job_radar.scrapers.browser import BrowserManager

    manager = BrowserManager(cfg)
    entries: list[AppliedEntry] = []
    pages, via = 0, ""
    try:
        page = manager.launch(SOURCE, force_headless=headless)
        if not manager.wait_cloudflare(page):
            raise SyncError(
                "JobsDB 的验证页没有自动通过。\n"
                "先双击项目目录里的 login_jobsdb.bat 手动过一次验证，再回来同步。")
        try:
            entries, pages, via = collect_applied(
                page, timeout_s=timeout_s, max_pages=max_pages)
        except Exception as exc:             # noqa: BLE001
            raise SyncError(f"读取投递记录失败：{exc}") from exc
    except SyncError:
        raise
    except Exception as exc:                 # noqa: BLE001
        raise SyncError(f"启动浏览器失败：{exc}") from exc
    finally:
        manager.close()

    report = SyncReport(pages=pages, found=len(entries), via=via)
    if not entries:
        logger.warning(
            "没读到任何投递记录。两种可能：这个档案没登录 JobsDB，"
            "或者最近 90 天确实没在站内投过。")
        return report
    apply_entries(db, entries, report, dry_run=dry_run, backfill=backfill)
    return report
