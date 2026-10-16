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
"""
from __future__ import annotations

import random
import re
import time
from typing import Optional

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


class BrowserManager:
    """Playwright persistent context 的统一入口，被各适配器复用。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._playwright = None
        self._contexts: dict[str, object] = {}   # source_key → BrowserContext
        self._pages: dict[str, object] = {}      # source_key → Page（每站一页复用）

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
        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            # 视口用常见桌面分辨率，降低"明显是自动化"的信号
            viewport={"width": 1366, "height": 900},
            locale="zh-HK",
            # 防崩溃参数（关键）：Windows 上 Chromium 的 GPU 渲染进程会在抓取
            # 多页后崩溃（Page/Target crashed），禁用 GPU 与部分耗资源特性后稳定。
            # 这些参数只影响本自动化浏览器，不影响用户日常使用的 Chrome。
            args=[
                "--disable-gpu",                  # 禁用 GPU 硬件加速：修复渲染进程崩溃
                "--disable-dev-shm-usage",        # 降低共享内存占用，避免内存不足崩溃
                "--disable-extensions",           # 禁用扩展，减少资源与变量
                "--disable-background-networking",# 减少后台网络，降低干扰
                "--no-first-run",                 # 跳过首次运行向导
                "--no-default-browser-check",     # 跳过默认浏览器检测
                "--no-proxy-server",              # 强制直连、不走系统代理
                                                  # 关键：本机环境有代理(127.0.0.1:4699)，
                                                  # 若 Chromium 走了它会导致 Google/reCAPTCHA
                                                  # 加载失败（LinkedIn 登录的人机验证无法完成）。
                                                  # 香港网络直连 Google 正常，故强制直连。
            ],
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
    def wait_cloudflare(self, page, timeout_s: int | None = None) -> bool:
        """若当前是 Cloudflare 挑战页则等待其自动通过。

        返回 True = 页面可用；False = 等待超时（调用方应放弃本轮，
        并提示用户运行 `python run.py login jobsdb` 手动过盾）。
        绝不尝试任何绕过手段（合规红线）。
        """
        timeout_s = timeout_s or self.config.cloudflare_wait_seconds
        deadline = time.monotonic() + timeout_s
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
            logger.warning("检测到 Cloudflare 挑战页，等待自动通过（不破解，只等待）...")
            time.sleep(3)
        logger.error(f"Cloudflare 等待超时（{timeout_s}s）。"
                     f"请运行: python run.py login jobsdb 手动完成验证。")
        return False

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
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug(f"停止 playwright 时异常（忽略）：{exc}")
            self._playwright = None
