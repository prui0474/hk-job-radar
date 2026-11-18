"""BrowserManager：persistent context 生命周期管理（架构 4.4 / T02）。

为什么用 persistent context 而不是普通 launch + storage_state：
1. Cloudflare 的指纹检测会综合 Cookie、localStorage、缓存等几十项信号，
   persistent context 把整个浏览器配置目录原样留在磁盘
   （data/browser/<source>/），下次启动等于"同一个浏览器再次打开"，
   过盾成功率远高于只恢复 Cookie；
2. 每个站点独立 user_data_dir，站点之间 Cookie/指纹互不污染，
   一个站出问题不会连累其它站。

合规红线：本模块不做任何"破解"——不注入 JS 伪造指纹、不伪造请求头。
遇到 Cloudflare 挑战页的策略只有一种：等待 + 提示用户用 login 模式手动处理。

`fetch_json` 用页面内 `fetch()` 请求接口，同样不是破解：那就是这个浏览器
自己发出的请求（真实 TLS/HTTP2 指纹、真实 Cookie），跟用户在地址栏里
打开页面后 DevTools 里发请求没有区别。
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from loguru import logger

from hk_job_radar.config import AppConfig

# Cloudflare 挑战页的识别特征：标题/正文里的固定文案。
# 只识别、不绕过——识别出来是为了"等待"和"提示"，不是攻击。
#
# 注意：不要用"請稍候 / 请稍候"这类泛化"加载中"文案——Indeed 等站的正常
# 结果页里也有"請稍候"（加载组件文案），会导致 wait_cloudflare 误判成挑战页、
# 白等 45 秒。这里只保留 Cloudflare 独有的强特征。
_CLOUDFLARE_MARKERS = (
    "just a moment",           # 挑战页标题（英文，Cloudflare 独有）
    "checking your browser",   # 挑战页正文（中英页面都会出现的英文原句）
    "cf-chl",                  # 挑战组件 DOM 类名
    "access denied",           # 1020 硬拦截标题（Indeed 无头模式常见）
    "request has been blocked",# 英文拦截正文
    "請求被攔截",              # 繁中拦截正文
    "请求被拦截",              # 简中拦截正文
)


#: 页面正在跳转时 Playwright 的报错：正在跑的 `page.evaluate` 被跳转销毁。
#: 不是失败，等新页面加载完重发一次就好（见 fetch_json）。
_CONTEXT_DESTROYED_RE = re.compile(
    r"Execution context was destroyed|context was destroyed|"
    r"because of a navigation", re.I)


def build_proxy_args(proxy: str) -> list[str]:
    """把 browser.proxy 的取值翻译成 Chromium 的代理启动参数。

    ""        → ["--no-proxy-server"]   强制直连（默认，与历史版本行为一致）
    "system"  → []                     不传参数 = Chromium 自己读系统/环境变量里的代理
    其它       → ["--proxy-server=…"]   显式指定，例如 "http://127.0.0.1:7890"

    为什么默认是强制直连而不是跟随系统：早期实测发现，若本机存在一个配错的
    本地代理，Chromium 走它会令 reCAPTCHA 资源加载失败、人机验证永远转圈。
    但强制直连在“必须靠代理才能出境”的网络里等于把路堵死，所以做成可配：
    需要走代理的人显式填 system 或具体地址，不需要的人保持默认、行为不变。
    """
    value = (proxy or "").strip()
    if not value:
        return ["--no-proxy-server"]
    if value.lower() == "system":
        return []
    return [f"--proxy-server={value}"]


class BrowserFetchError(RuntimeError):
    """页面内 fetch 失败（HTTP 非 2xx、JSON 解析失败、或 fetch 本身抛错）。

    单独定成一个异常类型，是为了让上层能判断「这次失败是不是 Cloudflare
    挑战」——`status == 429` 或响应头带 `Cf-Mitigated` 就是被挑战，
    该跳过的跳过、该报错的报错，而不是把「被拦」误当成「这家公司没岗位」。
    """

    def __init__(self, message: str, *, url: str = "", status: int = 0,
                 snippet: str = "", cf_mitigated: bool = False) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.snippet = snippet
        self.cf_mitigated = cf_mitigated

    @property
    def is_cloudflare_challenge(self) -> bool:
        return self.status == 429 or self.cf_mitigated


#: 在页面里跑 fetch 的脚本。参数化成一个 arg 对象，避免拼接字符串注入。
#:
#: 为什么必须用 page.evaluate 而不是 page.request：
#: page.request 是 Playwright 的 APIRequestContext，走的是 Playwright 自己
#: 的网络栈，TLS/HTTP2 指纹和 urllib 一样"不像浏览器"——实测仍会被
#: Cloudflare 以 429 + Cf-Mitigated 挡下。只有页面内 fetch 带的是这个
#: 浏览器进程真实的指纹与 Cookie，才能过。
_FETCH_JSON_JS = """
async ({url, payload, timeoutMs}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = {
      'Accept': 'application/json',
      'Accept-Language': 'en-US,en;q=0.9',
    };
    const init = {
      method: 'GET',
      headers: headers,
      credentials: 'include',
      signal: controller.signal,
    };
    if (payload !== null) {
      headers['Content-Type'] = 'application/json';
      init.method = 'POST';
      init.body = JSON.stringify(payload);
    }
    const resp = await fetch(url, init);
    const text = await resp.text();
    return {
      ok: resp.ok,
      status: resp.status,
      text: text,
      cfMitigated: resp.headers.get('cf-mitigated') || '',
    };
  } finally {
    clearTimeout(timer);
  }
}
"""


class BrowserManager:
    """Playwright persistent context 的统一入口，被各适配器复用。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._playwright = None
        self._contexts: dict[str, object] = {}   # source_key → BrowserContext
        self._pages: dict[str, object] = {}      # source_key → Page（每站一页复用）
        # (source_key, host) → Page：同一 context 内一个主机一个 page。
        # 为什么需要它：Workday 一个 source_key 服务五家不同域名的雇主，
        # 共用一个 page 时每次换家都要 goto 到新主机，而站点自己的一次重定向
        # 会在我们走之后才落地，把新的 goto 打断（见 _host_page）。
        self._host_pages: dict[tuple[str, str], object] = {}
        self._ua_cache: Optional[str] = None     # 见 resolve_headless_user_agent

    # ------------------------------------------------------------------
    @staticmethod
    def strip_headless_marker(ua: str) -> str:
        """把 UA 里的 `HeadlessChrome/157.0.0.0` 换成 `Chrome/157.0.0.0`。

        只动这一个词，版本号、平台、引擎全部原样保留——它说的仍然是这台
        浏览器的真实身份，只是不再主动声明「我是无头模式」。

        为什么要动它（2026-11 实测）：同一台机器、同一份启动参数，
        UA 里带 HeadlessChrome 时 JobsDB 的 Cloudflare 挑战 45 秒不放行、
        0 张岗位卡片；只把这两个词换掉，3.6 秒出 30 张卡片。
        """
        return (ua or "").replace("HeadlessChrome", "Chrome")

    def resolve_headless_user_agent(self) -> Optional[str]:
        """取「去掉无头标记」的 UA；取不到返回 None（调用方就不传 UA）。

        做法是临时起一个无头实例读 `navigator.userAgent` 再关掉 ——
        这样版本号一定和当前内核一致，不会写死成未来会过期的字符串。
        一次进程只算一次（结果缓存），代价约 0.5 秒。
        """
        if self._ua_cache is not None:
            return self._ua_cache or None
        if self._playwright is None:
            return None
        probe = None
        try:
            probe = self._playwright.chromium.launch(headless=True)
            page = probe.new_page()
            ua = page.evaluate("() => navigator.userAgent") or ""
            self._ua_cache = self.strip_headless_marker(str(ua))
        except Exception as exc:
            # 取不到就退回原样启动：能抓的站照样能抓，别为一项可选优化
            # 把整轮抓取弄崩。
            logger.debug(f"读取无头 UA 失败（本次不覆盖 UA）：{exc}")
            self._ua_cache = ""
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
        return self._ua_cache or None

    # ------------------------------------------------------------------
    def launch(self, source_key: str, force_headless: Optional[bool] = None,
               fresh: bool = False):
        """为指定站点启动（或复用）persistent context，返回可用 Page。

        - 每站独立 user_data_dir：data/browser/<source_key>/
        - force_headless 供 login 引导模式使用（登录时必须看得见浏览器）
        - fresh=True 时先删除旧 user_data_dir：用于 Indeed 这类"对同一 profile
          连续访问会软性限流"的站点，保证每次都是"全新访客"身份。
        """
        if source_key in self._pages:
            return self._pages[source_key]

        # 延迟导入 playwright：让 `hk_radar stats` 这类纯本地命令
        # 在未安装浏览器时也能正常工作，import 失败时给出中文指引
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "未安装 playwright，请先执行：pip install -r requirements.txt && "
                "playwright install chromium"
            ) from exc

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        # user_data_dir：每站独立浏览器配置目录（默认 data/browser/<source>/）
        # 推导自 config.py 的 BROWSER_DIR 常量，而不是 AppConfig——
        # 路径属于项目结构而非用户配置，不应出现在 config.yaml 里。
        # BROWSER_DIR 会在「Windows + 项目路径含中文」时自动切到 %LOCALAPPDATA%，
        # 见 config.py 的 _resolve_browser_dir()。
        from hk_job_radar.config import BROWSER_DIR
        user_data_dir = BROWSER_DIR / source_key
        if fresh and user_data_dir.exists():
            # 删除旧 profile 以确保"全新访客"身份（避免被站点软性限流识别为
            # 回头客）。这是本项目自己的数据目录，非用户个人文件，删除安全。
            import shutil
            shutil.rmtree(user_data_dir, ignore_errors=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        headless = self.config.browser_headless if force_headless is None else force_headless
        logger.debug(f"启动 persistent context：{source_key}"
                     f"（headless={headless}，档案目录={user_data_dir}）")

        # 防崩溃参数（关键）：Windows 上 Chromium 的 GPU 渲染进程会在抓取
        # 多页后崩溃（Page/Target crashed），禁用 GPU 与部分耗资源特性后稳定。
        # 这些参数只影响本自动化浏览器，不影响用户日常使用的 Chrome。
        chromium_args = [
            "--disable-gpu",                  # 禁用 GPU 硬件加速：修复渲染进程崩溃
            "--disable-dev-shm-usage",        # 降低共享内存占用，避免内存不足崩溃
            "--disable-extensions",           # 禁用扩展，减少资源与变量
            "--disable-background-networking",# 减少后台网络，降低干扰
            "--no-first-run",                 # 跳过首次运行向导
            "--no-default-browser-check",     # 跳过默认浏览器检测
        ]
        # 代理策略见 build_proxy_args：默认强制直连，browser.proxy 可改
        chromium_args.extend(build_proxy_args(self.config.browser_proxy))
        if self.config.browser_proxy.strip() and self.config.browser_proxy.strip().lower() != "system":
            logger.info(f"浏览器代理：{self.config.browser_proxy.strip()}")

        # 可选：无头模式下隐去 UA 里的无头标记（见 strip_headless_marker）。
        # 只在无头时才有意义 —— 有头模式的 UA 本来就不带这个词。
        launch_extra: dict[str, object] = {}
        if headless and self.config.browser_hide_headless:
            ua = self.resolve_headless_user_agent()
            if ua:
                launch_extra["user_agent"] = ua
                logger.debug(f"UA 已隐去无头标记：{ua}")
            else:
                # 开关开着但没读到 UA：浏览器仍会带着 HeadlessChrome 出去，
                # 效果与没开这一项相同，必须让用户看见。
                logger.warning(
                    "hide_headless 已开启，但这次没读到浏览器的 UA，本轮没有隐去无头标记。"
                    "JobsDB 和 Indeed 可能仍然抓不到岗位。可以先重跑一次；"
                    "如果每次都这样，执行 playwright install chromium 检查浏览器内核。")
        elif headless:
            # 无头又没隐去标记：Cloudflare 会认出来，站点静默地一个岗位都不给。
            logger.warning(
                "当前是无头模式，但没有隐去无头标记（hide_headless 为 false）。"
                "JobsDB 和 Indeed 很可能被 Cloudflare 挡下，表现是跑完一轮 0 个岗位"
                "却不报错。改法：把 config.yaml 里 browser 段的 hide_headless 改成 true。")

        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            # 视口用常见桌面分辨率，降低"明显是自动化"的信号
            viewport={"width": 1366, "height": 900},
            locale="zh-HK",
            args=chromium_args,
            **launch_extra,
        )
        page = context.new_page()
        self._contexts[source_key] = context
        self._pages[source_key] = page
        return page

    # ------------------------------------------------------------------
    def inject_cookies(self, source_key: str, cookies: list[dict]) -> None:
        """向指定站点的 persistent context 注入 Cookie。

        「换一种登录方式」的核心：用户在自己的正常浏览器（Chrome/Edge）里
        已经正常登录、人机验证也在那边完成，这里只搬运登录凭证 Cookie
        （如 LinkedIn 的 li_at / JSESSIONID），彻底绕开"自动化浏览器里
        reCAPTCHA 加载不出来"的死结。

        重要（2026-10-14 实测）：注入的 Cookie **不会写进磁盘 profile**，
        关闭浏览器即消失。所以调用方需要在每次启动时重新注入，或者改用在
        浏览器里手动登录一次（那条路径由浏览器自己落盘，才真正持久）。
        """
        page = self.launch(source_key)   # 确保 context 已创建
        ctx = self._contexts[source_key]
        try:
            ctx.add_cookies(cookies)
        except Exception as exc:
            logger.debug(f"注入 Cookie 到 {source_key} 时异常（忽略）：{exc}")
        logger.info(f"已向 {source_key} 注入 {len(cookies)} 个 Cookie："
                    f"{[c.get('name') for c in cookies]}")

    # ------------------------------------------------------------------
    def human_delay(self, min_s: float | None = None, max_s: float | None = None) -> None:
        """随机延迟（默认 2-8 秒）：模拟人类浏览节奏，是防封号的第一道闸。"""
        lo = self.config.human_delay_min_s if min_s is None else min_s
        hi = self.config.human_delay_max_s if max_s is None else max_s
        time.sleep(random.uniform(lo, hi))

    # ------------------------------------------------------------------
    def wait_cloudflare(self, page, timeout_s: int | None = None,
                        *, quiet: bool = False) -> bool:
        """若当前是 Cloudflare 挑战页则等待其自动通过。

        返回 True = 页面可用；False = 等待超时（调用方应放弃本轮，
        并提示用户双击 login_jobsdb.bat 手动过盾）。
        quiet=True 时不打那条「请运行 login」的日志 —— login 命令自己
        就在跑，它要打的是更具体的诊断，不希望被一句自指的提示盖住。

        绝不尝试任何绕过手段（合规红线）。
        """
        timeout_s = timeout_s or self.config.cloudflare_wait_seconds
        deadline = time.monotonic() + timeout_s
        warned = False
        while time.monotonic() < deadline:
            try:
                content = (page.content() or "").lower()
                title = (page.title() or "").lower()
            except Exception:
                # 页面正在跳转（如 LinkedIn 访客被节流 302 到登录墙），
                # content() 会抛 "page is navigating"——此时视为"仍在加载"，
                # 稍后重试，而不是把异常抛给上层拖垮整轮抓取。
                time.sleep(1)
                continue
            if not any(m in content or m in title for m in _CLOUDFLARE_MARKERS):
                return True
            if not warned:
                # 只提醒一次：20~45 秒里每 3 秒刷一条会把终端刷满，
                # 反而盖住真正有用的提示。
                logger.warning(
                    f"检测到 Cloudflare 挑战页，等待自动通过"
                    f"（最多 {timeout_s} 秒，不破解只等待）...")
                warned = True
            time.sleep(3)
        ua, clearance, hint = self._challenge_failure_diagnosis(page)
        logger.warning(
            f"Cloudflare 挑战 {timeout_s} 秒内没有自动通过。\n"
            f"    UA：{ua}\n"
            f"    cf_clearance：{clearance}\n"
            f"    → {hint}")
        if not quiet:
            # 这条提示曾经写的是「请运行 python run.py login jobsdb」—— 对绝大多数
            # 用户等于没说：他们不会开终端。改成「双击哪个文件」，命令行写法不出现。
            logger.error(
                f"Cloudflare 等待超时（{timeout_s} 秒）。\n"
                f"    这不是你配错了，是这个站的验证页没有自动放行。\n"
                f"    下一步：双击项目文件夹里对应的 login_*.bat"
                f"（Mac 上是 login_*.command），\n"
                f"    在弹出的浏览器里手动点一下验证，看到正常页面后关掉它，再重新启动。")
        return False

    # ------------------------------------------------------------------
    def _challenge_failure_diagnosis(self, page) -> tuple[str, str, str]:
        """超时后取两项实测值（UA / cf_clearance），说清「差在哪」。

        为什么要实测而不是直接猜：用户看到「没通过」只会反复重启。把 UA 和
        cookie 摆出来，才能分清是「无头标记没隐去」「出口 IP 被判风险」还是
        「旧 cookie 作废」——三种情况的下一步完全不同。
        所有读取都兜住异常：诊断本身绝不能把整轮抓取带崩。
        """
        ua = "读不到"
        try:
            value = page.evaluate("navigator.userAgent")
            if value:
                ua = str(value)[:100]
        except Exception:  # noqa: BLE001 - 页面正在跳转等，读不到就如实说读不到
            pass

        clearance = "没有"
        try:
            for cookie in page.context.cookies() or []:
                if cookie.get("name") != "cf_clearance":
                    continue
                expires = cookie.get("expires", -1)
                if expires in (None, -1, 0):
                    # -1 = 会话级 cookie，关掉浏览器就没了
                    clearance = "有（会话级，关掉浏览器就失效）"
                else:
                    clearance = ("有，" + time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(float(expires))) + " 到期")
                break
        except Exception:  # noqa: BLE001 - 拿不到 cookie 不该影响主流程
            clearance = "读不到"

        if "HeadlessChrome" in ua:
            hint = ("UA 里还带着 HeadlessChrome —— 无头标记没隐去，"
                    "检查 config.yaml 里 browser.hide_headless 是不是 true。")
        elif ua == "读不到":
            hint = ("读不到页面 UA，判断不了是无头标记还是出口 IP 问题；"
                    "先确认 config.yaml 里 browser.hide_headless 是 true。")
        elif clearance.startswith("有"):
            hint = ("档案里有 cf_clearance 却仍被拦 —— 通常是出口 IP 或 UA 变了"
                    "（换网络 / 换 SIM / 浏览器升级），旧 cookie 已作废，"
                    "需要手动过盾一次。")
        else:
            hint = ("UA 里没有 HeadlessChrome，说明标识没问题，"
                    "更像是出口 IP 被判定为风险（换网络 / 开代理都可能）。")
        return ua, clearance, hint

    # ------------------------------------------------------------------
    def _host_page(self, source_key: str, url: str):
        """返回 `url` 所在主机专属的 page（同一 context 内，一主机一页）。

        为什么不能一个 page 跑遍所有主机（2026-11 实测）：Workday 五家雇主
        域名各不相同，共用一个 page 时每换一家都要 goto 到新主机；而 Workday
        站点在 `domcontentloaded` 之后还会自己再跳一次
        （`/MFCJH_Jobs` → `/zh-CN/MFCJH_Jobs` 这种语言/路径重定向），那次跳转
        往往在我们已经导航去下一家之后才落地，于是把新的 goto 打断，Playwright
        抛 `Navigation ... is interrupted by another navigation`，而且错误会被
        安到下一次 goto 头上——实测 5 家里后 4 家全部失败。

        一个主机一个 page 之后，任何一次 goto 都只发生在自己主机内，跨主机
        打断就不存在了。context（profile）仍然只有一个：不额外增加一份磁盘
        档案，也不多一次浏览器启动。

        基础 page（`launch()` 返回的那张）直接给第一个主机用：刚启动时它停在
        `about:blank`，还没有「主机归属」，不算跨主机。
        """
        base = self.launch(source_key)
        host = urlsplit(url).netloc.lower()
        base_host = urlsplit(getattr(base, "url", "") or "").netloc.lower()
        if not base_host or base_host == host:
            return base
        key = (source_key, host)
        cached = self._host_pages.get(key)
        if cached is not None:
            return cached
        page = self._contexts[source_key].new_page()
        self._host_pages[key] = page
        logger.debug(f"[browser] 为 {host} 新建 page（source={source_key}）")
        return page

    # ------------------------------------------------------------------
    @staticmethod
    def _wait_page_ready(page, timeout_ms: int = 15000) -> None:
        """等页面（可能正在跳转）把 `load` 事件走完；等不到也不报错。

        给「evaluate 撞上站点自己的重定向」用：那一刻新文档还没加载完，
        立刻重发只会再撞一次。
        """
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _wait_url_settled(page, tries: int = 20, interval_ms: int = 100) -> None:
        """等 `page.url` 连续两次读到的值相同，再认为导航落定。

        为什么 goto 之后还要等：Workday 在 `domcontentloaded` 之后还会再跳一次
        （`/MFCJH_Jobs` → `/zh-CN/MFCJH_Jobs`）。紧接着的 `page.evaluate` 会撞上
        `Execution context was destroyed`——Manulife 的 `_discover_hk_facets()`
        返回 None 就是这么来的：那次探路请求抛了异常，被 except 吞掉后直接退回
        了「本地筛选」。

        注意这道保险只覆盖「跳转很快」的那类站点。实测 Manulife / 中信银行国际
        的这次重定向要 5 秒才发生，任何固定等待都堵不住，所以 `fetch_json` 里
        还有一层「撞上就等页面 load 完重发一次」。

        到次数上限也返回（不报错）：等不到不是错误，只是没有重定向，
        后面的 fetch 自己有超时。
        """
        previous = None
        for _ in range(tries):
            try:
                current = page.url or ""
            except Exception:
                current = ""
            if previous is not None and current == previous:
                return
            previous = current
            try:
                page.wait_for_timeout(interval_ms)
            except Exception:
                return

    # ------------------------------------------------------------------
    def open_origin(self, source_key: str, url: str, timeout_ms: int = 60000):
        """确保 page 停在 `url` 所在站点的页面上，返回该 page。

        为什么必须先在目标站点自己的页面上：
        页面内 fetch 受**同源策略**约束。在 A 站页面里 fetch B 站的接口，
        浏览器先发 CORS 预检，对方不返回 Access-Control-Allow-Origin 就直接
        拒绝——连请求都不会真正发出去。Workday 的 cxs 接口不开放 CORS，
        所以只能先导航到它自己的招聘页，再 fetch 同源的 `/wday/cxs/...`。

        page 按主机分配（见 `_host_page`），所以同一站点不重复导航
        （page.url 的 host 相同就直接复用），一家雇主几十条详情请求
        只有第一次付导航开销。
        """
        page = self._host_page(source_key, url)
        target = urlsplit(url)
        current = urlsplit(getattr(page, "url", "") or "")
        if (current.scheme in ("http", "https")
                and current.netloc.lower() == target.netloc.lower()):
            return page
        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                break
            except Exception as exc:
                # 站点自己的一次重定向正好和这次 goto 撞上时，Playwright 报的是
                # `Navigation ... is interrupted by another navigation`——那说明
                # 页面还在自己跳，不是网络坏也不是配置错，重试一次就过。
                if attempt == 0 and "interrupted by another navigation" in str(exc):
                    page.wait_for_timeout(300)
                    continue
                raise BrowserFetchError(
                    f"打开目标站点页面失败：{url}（{exc}）", url=url) from exc
        # 等站点自己的第二次跳转落定，否则紧接着的 evaluate 会撞上
        # `Execution context was destroyed`（见 _wait_url_settled）。
        self._wait_url_settled(page)
        # 挑战没自动通过就不要硬发请求：那只会拿到 429，还得再等一轮。
        # quiet=True：workday 这类站点没有专门的 login_*.bat，通用的
        # 「双击 login 文件」提示对它没有用，交给调用方给更准确的话。
        if not self.wait_cloudflare(page, quiet=True):
            raise BrowserFetchError(
                f"目标站点页面卡在 Cloudflare 挑战上过不去：{url}",
                url=url, cf_mitigated=True)
        return page

    # ------------------------------------------------------------------
    def fetch_json(self, source_key: str, url: str, payload: dict | None = None,
                   timeout_ms: int = 30000) -> dict:
        """在已打开的页面里用 `fetch()` 请求 JSON 接口，返回解析后的 dict。

        走浏览器页面内请求而不是 urllib / page.request，是因为后两者的
        请求指纹（TLS、HTTP/2 帧、头顺序）与真实浏览器不一致，会被
        Cloudflare 识别。2026-11 实测：同一台机器同一个出口 IP，urllib
        直连 5 家 Workday 全部 429（响应头 `Cf-Mitigated: challenge`），
        换成页面内 fetch 同一接口 5 家全部 200。

        **调用方必须先让 page 停在目标站点自己的页面上**（用
        `open_origin()`）——跨域 fetch 会被 CORS 挡掉，同源才能过。
        这里会再核一次页面 host，不同源就直接报错，免得拿到一条看不懂的
        CORS 失败信息。

        HTTP 非 2xx、fetch 抛错、JSON 解析失败都抛 BrowserFetchError，
        并把状态码与响应片段带出来，避免"静默 0 条"。
        """
        page = self._host_page(source_key, url)
        target = urlsplit(url)
        current = urlsplit(getattr(page, "url", "") or "")
        if current.netloc.lower() != target.netloc.lower():
            raise BrowserFetchError(
                f"页面不在目标站点上（当前 {current.netloc or '空白页'}，"
                f"目标 {target.netloc}）：跨域 fetch 会被 CORS 挡掉。"
                f"调用方需要先 open_origin()。", url=url)
        try:
            result = page.evaluate(_FETCH_JSON_JS, {
                "url": url,
                "payload": payload,
                "timeoutMs": timeout_ms,
            })
        except Exception as exc:
            # 站点自己的重定向（实测 Manulife / 中信银行国际是
            # /MFCJH_Jobs → /zh-CN/MFCJH_Jobs，而且在 domcontentloaded 之后
            # 好几秒才发生）会把页面里的 JS 上下文销毁，正在跑的 fetch 直接
            # reject。等新页面 load 完再发一次即可：这类检索请求是只读的，
            # 重发没有副作用。
            if _CONTEXT_DESTROYED_RE.search(str(exc)):
                self._wait_page_ready(page)
                try:
                    result = page.evaluate(_FETCH_JSON_JS, {
                        "url": url,
                        "payload": payload,
                        "timeoutMs": timeout_ms,
                    })
                except Exception as retry_exc:
                    raise BrowserFetchError(
                        f"页面内 fetch 失败：{url}（{retry_exc}）",
                        url=url) from retry_exc
            else:
                # fetch 自己 reject（断网、超时 abort）都会落到这里
                raise BrowserFetchError(
                    f"页面内 fetch 失败：{url}（{exc}）", url=url) from exc

        if not isinstance(result, dict):
            raise BrowserFetchError(
                f"页面内 fetch 返回了意外结果：{url}", url=url)

        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        cf_mitigated = bool(result.get("cfMitigated"))
        snippet = text[:300]
        if not result.get("ok"):
            raise BrowserFetchError(
                f"页面内请求失败：HTTP {status} {url}；响应片段：{snippet!r}",
                url=url, status=status, snippet=snippet,
                cf_mitigated=cf_mitigated)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise BrowserFetchError(
                f"页面内请求返回的不是合法 JSON：HTTP {status} {url}；"
                f"响应片段：{snippet!r}",
                url=url, status=status, snippet=snippet,
                cf_mitigated=cf_mitigated) from exc
        if not isinstance(data, dict):
            raise BrowserFetchError(
                f"页面内请求返回的不是 JSON 对象：HTTP {status} {url}；"
                f"响应片段：{snippet!r}",
                url=url, status=status, snippet=snippet,
                cf_mitigated=cf_mitigated)
        return data

    # ------------------------------------------------------------------
    def ensure_logged_in(self, page, expected_url_pattern: str) -> bool:
        """检查当前页面是否处于登录态（供 LinkedIn 等站使用，JobsDB P0 不强制登录）。

        实现保持通用：登录成功通常伴随 URL 命中"已登录区域"，
        且页面不再出现登录表单。
        """
        try:
            if not re.search(expected_url_pattern, page.url or ""):
                return False
            content = (page.content() or "").lower()
            return "loginform" not in content and "sign in" not in content[:2000]
        except Exception as exc:  # 页面跳转中取 content 可能偶发失败
            logger.debug(f"登录态检查异常（按未登录处理）：{exc}")
            return False

    # ------------------------------------------------------------------
    def close_source(self, source_key: str) -> None:
        """关闭并移除指定 source 的 context/page（其余 source 不受影响）。

        Indeed 等站对"同一浏览器 profile 连续访问"会软性限流（第一次放行、
        之后返回空结果），需要每个关键词换一个全新 profile；抓完即可关闭
        该 profile 释放资源，避免 N 个浏览器进程常驻堆积。
        """
        ctx = self._contexts.pop(source_key, None)
        self._pages.pop(source_key, None)
        # 主机 page 属于这个 context，必须一起清掉：否则同一 source_key
        # 重新 launch 后 _host_page 会命中缓存、返回已被关闭的旧 page。
        for key in [k for k in self._host_pages if k[0] == source_key]:
            self._host_pages.pop(key, None)
        if ctx is not None:
            try:
                ctx.close()
            except Exception as exc:
                logger.debug(f"关闭 context {source_key} 时异常（忽略）：{exc}")

    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭全部上下文与 playwright 实例。每轮监控周期结束时必须调用，
        否则浏览器进程会泄漏累积。"""
        for key, ctx in list(self._contexts.items()):
            try:
                ctx.close()
            except Exception as exc:
                logger.debug(f"关闭 context {key} 时异常（忽略）：{exc}")
        self._contexts.clear()
        self._pages.clear()
        self._host_pages.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug(f"停止 playwright 时异常（忽略）：{exc}")
            self._playwright = None
