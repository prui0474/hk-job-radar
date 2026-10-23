"""图形岗位工作台（P2）：在浏览器里管理已经抓到的岗位。

为什么做成「本地网页」而不是桌面程序
------------------------------------
分发形态是「解压、双击脚本」，多一个 exe 就多一份杀毒软件误报、多一套签名与
架构兼容问题。本地网页只依赖 Python 自带的标准库（`http.server`），用户看到的
是一个浏览器标签页，关掉窗口就等于退出，没有任何东西常驻。

安全边界（本地服务最容易被忽视的部分）
--------------------------------------
1. **只绑 127.0.0.1**：不是 0.0.0.0。局域网里的别人连不上。
2. **每次启动生成随机令牌**，写在启动时打印的地址里（`?t=`）。没带令牌的请求
   一律 403，**不做「自动跳到带令牌地址」这种方便事**——那等于把令牌白送。
3. **校验 Host 头**：只接受 127.0.0.1 / localhost。这是防 DNS rebinding 的
   保险——恶意页面可以把域名解析到 127.0.0.1 来打本地端口，但它的 Host 头
   是那个域名，骗不过这一关。
4. 请求体限制 4KB，只接受 JSON；不提供任何文件读取接口。

数据从哪来
----------
直接读本地 SQLite，不缓存、不复制。按按钮就是把 `jobs.status` 改掉，
所以关掉看板之后，日报、统计、以后的功能看到的都是同一份状态。
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from collections import Counter
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger

from hk_job_radar.db import Database, composite_score
from hk_job_radar.dedup import NO_KEY, dedupe_key_of
from hk_job_radar.matcher import MatchError, ResumeMatcher
from hk_job_radar.models import now_hk
from hk_job_radar.resume import ResumeError, load_resume_text

#: 页面文件与模块放在一起：打包时不需要额外的资源目录约定。
UI_PATH = Path(__file__).with_name("workbench_ui.html")

#: 令牌传递：地址栏带 `?t=`，前端读出来后塞进这个请求头。
#: 为什么不用 Cookie：Cookie 会被同域的其他本地服务看到，请求头不会。
TOKEN_HEADER = "X-Board-Token"
TOKEN_QUERY = "t"

#: 请求体上限。看板只收一个 uid 和一个动作名，4KB 已经十倍富余。
_MAX_BODY_BYTES = 4096

#: 四个按钮。键是前端传来的动作名，值是写进 `jobs.status` 的内容。
#: 「撤销」不在这张表里：它不是一个固定状态，而是「回到上一次的状态」，
#: 由 `Database.undo_board_status()` 按状态历史推出来。
BOARD_ACTIONS: dict[str, str] = {
    "applied": "applied",
    "interview": "interview",
    "closed": "closed",
    "rejected": "rejected",
}

#: 撤销的动作名（与 BOARD_ACTIONS 分开，因为它要查历史表）。
ACTION_UNDO = "undo"

#: 筛选标签 → 对应的数据库状态。`pending` 是默认视图：
#: 用户打开看板想看的是「还没处理的新岗位」，不是全部历史。
VIEW_FILTERS: dict[str, tuple[str, ...]] = {
    "pending": ("new", "reported"),
    "applied": ("applied",),
    "interview": ("interview",),
    "closed": ("closed",),
    "rejected": ("rejected",),
}

#: 单个岗位摘要回传上限。JobsDB 的摘要本来只有 500 字，Workday 的 JD 有 11KB，
#: 全塞进 JSON 会让首屏变慢；展开时再看更多没有意义——真要细读就点原链接。
_SNIPPET_LIMIT = 1500

#: 模块被导入的时刻。用它判断「磁盘上的代码是不是比本进程里的新」：
#: 页面 HTML 每次请求都重新读盘，所以界面永远是最新的；而 Python 代码只
#: 在启动那一刻装进内存一次。两者对不上时，用户会看到新按钮点了没反应，
#: 却不知道只要重启就好——这是实际发生过的困惑，所以主动报出来。
_STARTED_AT = time.time()

#: 改动这些文件必须重启才生效（新增功能时如果发现漏了哪个，加进来）。
_WATCHED_FILES = ("workbench.py", "workbench_ui.html", "db.py")


def _outdated_source() -> str:
    """本进程加载的代码是否已被更新的文件取代。

    返回需要提示的文件名；没有这种情况时返回空串（前端据此隐藏提示条）。
    容忍 1.5 秒误差：导入瞬间读到的 mtime 与 `_STARTED_AT` 本来就贴得很近，
    判太严会把「刚装好就启动」误报成旧版。
    """
    newest, name = _STARTED_AT + 1.5, ""
    base = Path(__file__).parent
    for fname in _WATCHED_FILES:
        try:
            mtime = (base / fname).stat().st_mtime
        except OSError:
            continue                    # 文件不在（打包裁剪过）就当它没意见
        if mtime > newest:
            newest, name = mtime, fname
    return name


def _job_payload(job) -> dict[str, Any]:
    """把 JobPosting 转成前端要的形状（字段名短一些，省带宽也省 JS 里的字）。"""
    posted = job.posted_at or job.first_seen_at
    age_days = ""
    # 综合排序的分数：卡片上要显示，所以在这里算好一起发给前端。
    # 用带小数的天数（不是 age_days 的整日），才能和 SQL 里的排序值对上。
    smart = None
    if posted:
        try:
            span = (now_hk() - posted).total_seconds() / 86400.0
            age_days = max(0, int(span))
            smart = composite_score(job.llm_score, span)
        except TypeError:          # 时区缺失的老数据（naive datetime）
            age_days = ""
    return {
        "uid": job.job_uid,
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "salary": job.salary_text or "",
        "url": job.url,
        "source": job.source,
        "score": job.llm_score,
        "reason": job.llm_reason or "",
        "eligibility": getattr(job, "eligibility_note", "") or "",
        "keyword": job.keyword_matched or "",
        "status": job.status,
        "direct": bool(job.direct_apply),
        "posted": posted.isoformat() if posted else "",
        "age_days": age_days,
        # 这个天数是哪来的：岗位自己写了发布日（"posted"），还是只能退到
        # 「我们第一次看到它」（"seen"）。前端要据此选措辞——补录进来的
        # 那条只有投递日，标成「23 天前发布」就是编了个发布日期。
        "age_from": "posted" if job.posted_at else "seen",
        # 综合分（分数 × 新鲜度折扣）。前端只在「综合排序」下显示，
        # 因为分数优先模式下这张卡的位置跟它无关，摆出来反而多个数字要解释。
        "smart": round(smart, 1) if smart is not None else None,
        "snippet": (job.description_snippet or "")[:_SNIPPET_LIMIT],
        # 跨站去重指纹（前端不用，统计完重复数就删掉）
        "key": dedupe_key_of(job.company, job.title),
    }


def _with_duplicate_counts(jobs) -> list[dict[str, Any]]:
    """给每条岗位标上「同岗位还有几条」。

    看板上真的会遇到同一个岗位挂了三条（同一家公司把同一个 MT 项目分别以
    不同 requisition 发出来）。去重模块的结论是**不合并**——合并错了会让用户
    永远看不到那个机会；但在看板上把三条一模一样的卡片摊开同样是噪音。
    折中：全部保留，只标出「另有 N 条同岗位」，用户一眼知道不用逐条看。
    """
    payloads = [_job_payload(j) for j in jobs]
    counts = Counter(p["key"] for p in payloads if p["key"] and p["key"] != NO_KEY)
    for item in payloads:
        key = item.pop("key", "")
        item["dup"] = (counts.get(key, 1) - 1) if key and key != NO_KEY else 0
    return payloads


class _BoardServer(ThreadingHTTPServer):
    """带令牌、数据库句柄、配置的服务器。"""

    daemon_threads = True       # Ctrl+C 时不必等空闲连接超时

    def __init__(self, addr: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 token: str, db: Database, cfg: Any = None) -> None:
        super().__init__(addr, handler)
        self.token = token
        self.db = db
        # cfg 只在「分析」接口里用到（要读 resume_path 与 LLM 配置）。
        # 允许为 None：老测试和只想看卡片的人不需要它。
        self.cfg = cfg
        # 简历文本按 (路径, 修改时间) 缓存在内存里：一次分析要几十秒，
        # 不值得每次点按钮都重新解析一遍 docx；但简历改了要能立刻生效。
        self.resume_cache: tuple[tuple[str, float], str] | None = None
        # 投递副驾：同一时刻只允许一次（每次都会弹一个浏览器窗口），
        # 后台线程在第一次点「投递」时才起来。
        self.apply_lock = threading.Lock()
        self._apply_worker: _ApplyWorker | None = None

    def apply_worker(self) -> "_ApplyWorker":
        """取（或懒创建）投递副驾的后台线程。"""
        if self._apply_worker is None:
            self._apply_worker = _ApplyWorker(self.cfg)
        return self._apply_worker


class _ApplyWorker:
    """投递副驾的后台线程：整个进程只有一个，浏览器窗口由它一个人管。

    为什么必须单独一个线程：Playwright 的同步接口绑在**创建它的线程**上，
    换个线程调 `page.goto` 会直接抛绿色线程（greenlet）错误。而工作台的每个
    HTTP 请求都是一个新线程，所以浏览器只能交给一个固定线程持有。

    顺带的好处：这个窗口在所有点击之间一直活着，用户可以从容地核对、
    点 Continue、亲手提交——不会被「请求结束」顺手关掉。
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._queue: queue.Queue = queue.Queue()
        self._browser: Any = None
        self._thread = threading.Thread(
            target=self._run, name="board-apply", daemon=True)
        self._thread.start()

    # -- 给 HTTP 线程用的入口 ------------------------------------------
    def submit(self, job: Any, form_answers: dict[str, Any], *,
               dry_run: bool = False, timeout_s: float = 180.0) -> Any:
        """排一次填表，等它跑完并返回 `AssistReport`。

        超时抛 `TimeoutError`；填表本身抛的异常原样抛给调用方——
        接口层要把它们翻成中文提示。
        """
        box: dict[str, Any] = {"done": threading.Event()}
        self._queue.put((job, form_answers, dry_run, box))
        if not box["done"].wait(timeout_s):
            raise TimeoutError("等浏览器响应超时（页面可能卡在验证页）")
        if "error" in box:
            raise box["error"]
        return box["result"]

    # -- 后台线程本体 ---------------------------------------------------
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:                 # 退出哨兵
                return
            job, form_answers, dry_run, box = item
            try:
                box["result"] = self._fill(job, form_answers, dry_run)
            except BaseException as exc:     # noqa: BLE001 - 必须回传，否则调用方死等
                box["error"] = exc
            finally:
                box["done"].set()

    def _fill(self, job: Any, form_answers: dict[str, Any], dry_run: bool) -> Any:
        from hk_job_radar.copilot import jobsdb_apply
        from hk_job_radar.scrapers.browser import BrowserManager

        if self._browser is None:
            self._browser = BrowserManager(self.cfg)
        url = jobsdb_apply.apply_url(job)
        logger.info(f"[副驾] 打开申请页 {url}")
        page = self._browser.launch("jobsdb", force_headless=False)
        # ★ 等 domcontentloaded 而不是 networkidle：JobsDB 页面永远有后台请求，
        #   networkidle 在真实页面上从来不会就绪（实测每次都超时）。
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if not self._browser.wait_cloudflare(page):
            raise RuntimeError(
                "这条岗位的页面卡在「正在验证浏览器」过不去。\n"
                "先双击项目目录里的 login_jobsdb.bat（Mac 上是 login_jobsdb.command）"
                "手动过一次验证，回来再点「投递」。")
        page.wait_for_timeout(3000)          # 给前端渲染申请表的时间
        return jobsdb_apply.assist(page, job, form_answers, dry_run=dry_run)


class _Handler(BaseHTTPRequestHandler):
    """极简路由器：四个端点，写成 if/elif 比引入框架清楚。"""

    server_version = "HKRadarBoard/1.0"
    # 默认 HTTP/1.1 + keep-alive：卡片连续点按钮时少一次握手。
    protocol_version = "HTTP/1.1"

    # -- 基础工具 -------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003
        """屏蔽逐请求访问日志：控制台留给「怎么用」的说明文字。"""

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body: bytes, filename: str, content_type: str) -> None:
        """以「另存为」的方式回一个文件。

        `filename` 用 ASCII：HTTP 头里放中文要按 RFC 5987 编码，
        各浏览器表现不一致，文件名乱码比文件名不好看更烦人。
        """
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- 安全门 ---------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _token_of(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        return (self.headers.get(TOKEN_HEADER) or (query.get(TOKEN_QUERY) or [""])[0]).strip()

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self._token_of(), self.server.token)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求内容不是合法 JSON：{exc}") from exc
        return data if isinstance(data, dict) else {}

    # -- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:                                  # noqa: N802
        if not self._host_ok():
            self._send_json({"ok": False, "error": "只允许从本机访问。"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            if not self._token_ok():
                self._send_html(_forbidden_page(), 403)
                return
            self._send_html(_load_ui(self.server.token))
            return

        if path == "/api/jobs":
            if not self._token_ok():
                self._send_json({"ok": False, "error": "令牌无效，请用控制台里打印的地址打开。"}, 403)
                return
            view = (query.get("view") or ["pending"])[0]
            statuses = VIEW_FILTERS.get(view)          # 未知视图当作「全部」
            limit = _int_arg(query, "limit", 200)
            min_score = _int_arg(query, "min_score", 0)
            text = (query.get("q") or [""])[0].strip()
            # 来源是用户从下拉框选的，值原样进参数化查询；只截断长度，
            # 免得一条超长 URL 把日志和 SQL 参数都撑大。
            source = (query.get("source") or [""])[0].strip()[:40]
            # 排序只认白名单（db 层还会再挡一次），未知值退回默认口径。
            sort = (query.get("sort") or ["default"])[0]
            jobs = self.server.db.board_jobs(
                statuses=statuses, limit=limit, min_score=min_score,
                query=text, source=source, sort=sort)
            self._send_json({"ok": True, "jobs": _with_duplicate_counts(jobs)})
            return

        if path == "/api/summary":
            if not self._token_ok():
                self._send_json({"ok": False, "error": "令牌无效。"}, 403)
                return
            self._send_json({"ok": True, "counts": self.server.db.board_counts(),
                             "stale": _outdated_source()})
            return

        if path == "/api/insights":
            if not self._token_ok():
                self._send_json({"ok": False, "error": "令牌无效。"}, 403)
                return
            days = _int_arg(query, "days", 30)
            self._send_json({"ok": True,
                             "insights": self.server.db.board_insights(days=days)})
            return

        if path == "/api/export":
            if not self._token_ok():
                self._send_json({"ok": False, "error": "令牌无效。"}, 403)
                return
            self._export(query)
            return

        self._send_json({"ok": False, "error": "没有这个页面。"}, 404)

    def _export(self, query: dict[str, list[str]]) -> None:
        """把报告作为附件回给浏览器（P5）。

        只支持 HTML 与 Excel：PDF 要现起一个 Chromium，几秒钟且更吃内存，
        放在看板上点一下就能触发不合适——那种操作留在命令行更清楚。
        """
        from hk_job_radar import export as export_mod

        fmt = (query.get("fmt") or ["html"])[0]
        if fmt not in ("html", "xlsx"):
            self._send_json({"ok": False, "error": "只支持 html / xlsx。"}, 400)
            return
        days = _int_arg(query, "days", 30)
        try:
            data = export_mod.collect(self.server.db, days=days)
            stem = export_mod.default_stem()
            if fmt == "html":
                self._send_download(export_mod.render_html(data).encode("utf-8"),
                                    f"{stem}.html", "text/html; charset=utf-8")
            else:
                if not export_mod.xlsx_available():
                    self._send_html(
                        "<meta charset='utf-8'><p>导出 Excel 需要先装 openpyxl："
                        "<code>.venv\\Scripts\\pip install openpyxl</code></p>"
                        "<p>或者先用 HTML 报告。</p>", 500)
                    return
                self._send_download(
                    export_mod.build_xlsx(data), f"{stem}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as exc:                 # noqa: BLE001
            logger.error(f"导出失败：{exc}")
            self._send_json({"ok": False, "error": f"导出失败：{exc}"}, 500)

    def do_POST(self) -> None:                                 # noqa: N802
        if not self._host_ok():
            self._send_json({"ok": False, "error": "只允许从本机访问。"}, 403)
            return
        if not self._token_ok():
            self._send_json({"ok": False, "error": "令牌无效，请重新打开工作台。"}, 403)
            return

        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/status":
            uid = str(payload.get("uid") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not uid:
                self._send_json({"ok": False, "error": "参数不完整。"}, 400)
                return
            if action == ACTION_UNDO:
                restored = self.server.db.undo_board_status(uid)
                if restored is None:
                    self._send_json({"ok": False, "error": "这条岗位不在库里（可能已被清理）。"}, 404)
                else:
                    self._send_json({"ok": True, "uid": uid, "status": restored})
                return
            status = BOARD_ACTIONS.get(action)
            if status is None:
                self._send_json({"ok": False, "error": "参数不完整。"}, 400)
                return
            if self.server.db.set_board_status(uid, status):
                self._send_json({"ok": True, "uid": uid, "status": status})
            else:
                self._send_json({"ok": False, "error": "这条岗位不在库里（可能已被清理）。"}, 404)
            return

        if path == "/api/analyze":
            self._handle_analyze(payload)
            return

        if path == "/api/sync-applied":
            self._handle_sync_applied(payload)
            return

        if path == "/api/apply":
            self._handle_apply(payload)
            return

        self._send_json({"ok": False, "error": "没有这个接口。"}, 404)

    # -- 投递副驾（P3）--------------------------------------------------
    def _handle_apply(self, payload: dict[str, Any]) -> None:
        """在看得见的浏览器里把「雇主问题」那一步的选择题答了。

        这个接口会等十几到几十秒（起浏览器 + 等页面）。三件**不做**的事：

        - 不点提交，连后面两步的 `Continue` 都不点；
        - 不替你标「已投」——你可能核对后又决定不投；
        - 不关浏览器——剩下几步要你自己在那个窗口里点。

        只支持 JobsDB 站内申请：其它站（跳去雇主 ATS 的）页面结构完全
        不同，硬填等于乱填，这里直接说清楚「自己去填」。
        """
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            self._send_json({"ok": False, "error": "参数不完整。"}, 400)
            return

        srv = self.server
        if srv.cfg is None:
            self._send_json(
                {"ok": False, "error": "工作台启动时没加载配置，无法投递。"}, 400)
            return

        job = srv.db.get_job(uid)
        if job is None:
            self._send_json({"ok": False, "error": "这条岗位不在库里（可能已被清理）。"}, 404)
            return
        if job.source != "jobsdb":
            self._send_json({
                "ok": False,
                "error": (f"这条岗位来自 {job.source}，申请页结构和 JobsDB 不一样，"
                          f"副驾代填只会填错。请点「打开原链接」自己填。"),
            }, 400)
            return

        if not srv.apply_lock.acquire(blocking=False):
            self._send_json({
                "ok": False,
                "error": "上一次投递还在进行（浏览器窗口应该已经打开了），先把那边处理完。",
            }, 409)
            return
        try:
            from hk_job_radar.copilot.profile import ProfileStore

            rules = ProfileStore().load().raw.get("screening_rules") or {}
            form_answers = dict(rules.get("form_answers") or {})
            report = srv.apply_worker().submit(
                job, form_answers, dry_run=bool(payload.get("dry_run")))
        except Exception as exc:                 # noqa: BLE001 - 别让页面挂住
            logger.exception(f"投递副驾失败 {uid}：{exc}")
            self._send_json({"ok": False, "error": f"投递副驾失败：{exc}"}, 500)
            return
        finally:
            srv.apply_lock.release()

        self._send_json({
            "ok": True,
            "onsite": report.onsite,
            "summary": report.summary(),
            "detail": report.detail_lines(),
            "filled": len(report.answers),
            "changed": len(report.changed),
            "blank": len(report.left_blank),
            "note": report.note,
        })

    # -- 同步已投（P3.5）------------------------------------------------
    def _handle_sync_applied(self, payload: dict[str, Any]) -> None:
        """把 JobsDB 上的投递记录读回来，自动标「已投」。

        这个请求要等**几十秒**（起浏览器 + 翻五页）。前端按钮在这期间显示
        「同步中…」，这里刻意不设超时：同步到一半掐断会留下半套状态，
        比多等一会儿更难解释。

        用 headless：工作台本来就是个网页，再弹一个浏览器窗口只会让人以为
        出问题了。万一本机 headless 过不了验证页，错误信息会指向命令行。
        """
        srv = self.server
        if srv.cfg is None:
            self._send_json(
                {"ok": False, "error": "工作台启动时没加载配置，无法同步。"}, 400)
            return

        from hk_job_radar import applied_sync

        try:
            report = applied_sync.sync(
                srv.db, srv.cfg, headless=True,
                dry_run=bool(payload.get("dry_run")))
        except applied_sync.SyncError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        except Exception as exc:                 # noqa: BLE001 - 别让页面挂住
            logger.error(f"同步已投失败：{exc}")
            self._send_json({"ok": False, "error": f"同步失败：{exc}"}, 500)
            return

        self._send_json({
            "ok": True,
            "summary": report.summary(),
            "detail": report.detail_lines(),
            "found": report.found,
            "pages": report.pages,
            "marked": len(report.marked),
            "already": report.already,
            "kept": len(report.kept),
            "unmatched": len(report.unmatched),
        })

    # -- 分析（P4）-----------------------------------------------------
    def _handle_analyze(self, payload: dict[str, Any]) -> None:
        """简历-JD 差距分析。

        这个接口会**等几十秒**（要调模型）。浏览器那边显示「分析中…」，
        `ThreadingHTTPServer` 保证等待期间别的请求照常响应。
        错误一律用 400 + 中文消息返回——页面上要能直接给用户看。
        """
        uid = str(payload.get("uid") or "").strip()
        force = bool(payload.get("force"))
        if not uid:
            self._send_json({"ok": False, "error": "参数不完整。"}, 400)
            return
        srv = self.server
        if srv.cfg is None:
            self._send_json({"ok": False, "error": "工作台启动时没加载配置，无法分析。"}, 400)
            return

        job = srv.db.get_job(uid)
        if job is None:
            self._send_json({"ok": False, "error": "这条岗位不在库里（可能已被清理）。"}, 404)
            return

        try:
            resume_text = _resume_text_for(srv)
            matcher = ResumeMatcher(srv.cfg.llm, resume_text)
            signature = matcher.signature()
            cached = None if force else srv.db.get_analysis(uid, signature)
            if cached:
                self._send_json({"ok": True, "analysis": cached, "cached": True})
                return
            result = matcher.analyze(job)
            srv.db.save_analysis(uid, signature, matcher.model, result)
            self._send_json({"ok": True, "analysis": result, "cached": False})
        except ResumeError as exc:
            # 简历没配/读不出来：这是「去改个配置」就能解决的问题，必须说清楚改哪里
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except MatchError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
        except Exception as exc:                     # noqa: BLE001 - 兜底，别让页面挂住
            logger.error(f"岗位分析失败 {uid}：{exc}")
            self._send_json({"ok": False, "error": f"分析失败：{exc}"}, 500)


def _int_arg(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int((query.get(name) or [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _resume_text_for(server: _BoardServer) -> str:
    """取简历文本（按路径 + 修改时间缓存）。没配路径时给一句「改哪里」的提示。"""
    cfg = server.cfg
    path = cfg.resolve_resume_path() if cfg is not None else None
    if path is None:
        raise ResumeError(
            "还没告诉我在哪份简历上做分析。\n"
            "打开项目目录下的 config.yaml，在最后加一行：\n"
            '  resume_path: "C:/Users/你的用户名/Desktop/简历/CV.docx"\n'
            "存盘后回来按 Ctrl+C 关掉工作台，再重新运行 run.py board。")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0            # 交给 load_resume_text 报「找不到文件」的中文错
    key = (str(path), mtime)
    if server.resume_cache is not None and server.resume_cache[0] == key:
        return server.resume_cache[1]
    text = load_resume_text(path)
    server.resume_cache = (key, text)
    return text


def _forbidden_page() -> str:
    """令牌不对时给用户看的页面（比一行 403 有用得多）。"""
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>需要从控制台打开</title></head>"
        "<body style=\"max-width:620px;margin:80px auto;padding:0 24px;"
        "font:16px/1.9 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "color:#1f2328;background:#fff\">"
        "<h2 style='font-size:19px'>这个地址打不开</h2>"
        "<p>工作台只接受<b>带密钥的完整地址</b>，直接输 IP 和端口是不行的。</p>"
        "<p>请回到那个黑色命令行窗口，里面打印了一行这样的地址：</p>"
        "<pre style=\"background:#f4f6f9;border:1px solid #e3e7ee;border-radius:8px;"
        "padding:10px 12px;overflow-x:auto\">http://127.0.0.1:8787/?t=…</pre>"
        "<p>把它整行复制到浏览器地址栏即可。</p>"
        "</body></html>"
    )


def _load_ui(token: str) -> str:
    """读页面文件并注入令牌。"""
    try:
        html = UI_PATH.read_text(encoding="utf-8")
    except OSError:
        return ("<h1>页面文件缺失</h1>"
                "<p>找不到 workbench_ui.html —— 安装包可能不完整，请重新解压。</p>")
    return html.replace("__BOARD_TOKEN__", token)


def serve(db: Database, port: int = 0, open_browser: bool = True,
          cfg: Any = None) -> int:
    """启动工作台，阻塞到 Ctrl+C。返回进程退出码。

    `port=0` 表示自动找空端口；指定端口被占时直接报错退出（用户显式要求了
    某个端口，静默换掉反而会让他找不到）。
    `cfg` 只有「分析」按钮需要（简历路径 + LLM 配置）；不传也能用，只是点了
    分析会提示「没加载配置」。
    """
    token = secrets.token_urlsafe(18)
    if port:
        try:
            server = _BoardServer(("127.0.0.1", port), _Handler, token, db, cfg)
        except OSError as exc:
            print(f"端口 {port} 用不了（{exc}）。换一个：run.py board --port 8790", flush=True)
            return 1
    else:
        server = _find_free_port(token, db, cfg)
        if server is None:
            print("找不到可用端口（8787–8987 全被占了）。关掉别的程序再试。", flush=True)
            return 1

    host, real_port = server.server_address[:2]
    url = f"http://{host}:{real_port}/?t={token}"
    # flush=True：输出重定向到文件/管道时 Python 默认整块缓冲，用户会盯着一个
    # 空窗口以为程序没起来。这类「告诉用户该干什么」的文字必须立刻可见。
    print("─" * 58, flush=True)
    print("岗位工作台已启动。在浏览器里打开这一行地址：", flush=True)
    print(flush=True)
    print(f"  {url}", flush=True)
    print(flush=True)
    print("  · 关掉窗口或按 Ctrl+C 结束（不常驻、不写注册表）", flush=True)
    print("  · 地址里的密钥每次启动都会变，刷新页面请用同一个标签页", flush=True)
    print("─" * 58, flush=True)

    if open_browser:
        # 延时打开：先把上面的说明打印完，否则浏览器抢焦点会让人错过地址。
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n工作台已关闭。", flush=True)
    finally:
        server.server_close()
    return 0


def _find_free_port(token: str, db: Database, cfg: Any = None) -> _BoardServer | None:
    for port in range(8787, 8987):
        try:
            return _BoardServer(("127.0.0.1", port), _Handler, token, db, cfg)
        except OSError:
            continue
    return None
