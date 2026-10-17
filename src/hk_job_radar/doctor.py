"""一键自检（doctor）——把「装错了哪里」变成程序自己说出来的话。

★ 为什么需要这个模块 ★
这个项目最大的流失点不是功能不够，而是**新手装不起来**：.bat 乱码、cp936 解码崩溃、
Mac 双击打不开、群机器人 webhook 少复制一段、API key 填错了位置……每一种都会让人
得出「这东西是坏的」的结论。翻文档要时间，问作者要等回复。

doctor 把这些排查经验固化成 15 项体检：一次跑完，用中文说清「哪一项没过、
为什么、具体怎么修」，最后给一句总结。

设计原则
1. **只读**：不修改任何配置或数据。唯一写盘的动作是「试写一个临时文件再删掉」。
2. **互不牵连**：前一项失败不影响后面还能查的项，能查的一律照查。
3. **能自动判断的绝不问用户**：路径、版本、依赖、浏览器内核全部自己探测。
4. **联网项可关**：AI 连通性默认实测（只消耗几个 token），`--no-llm` 可跳过。
"""
from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from hk_job_radar import tier
from hk_job_radar.config import (
    BROWSER_DIR,
    CAREER_STAGE_JOB_TYPES,
    CONFIG_PATH,
    DATA_DIR,
    DB_PATH,
    PROFILE_PATH,
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    load_config,
)

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_SYMBOLS: dict[str, str] = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "⏭️ "}

#: 依赖包（import 名 → pip 包名），与 requirements.txt 一一对应
_REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("yaml", "PyYAML"),
    ("loguru", "loguru"),
    ("requests", "requests"),
    ("apscheduler", "apscheduler"),
    ("playwright", "playwright"),
    ("openai", "openai"),
    ("tenacity", "tenacity"),
)

#: 画像 background 少于这个字数就认为「没认真填」，打分必然偏粗
_PROFILE_MIN_CHARS = 80


@dataclass
class Check:
    """一项体检结果。"""

    group: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def symbol(self) -> str:
        return _SYMBOLS.get(self.status, "?")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _display_width(text: str) -> int:
    """终端显示宽度：中文字符占 2 列，用来对齐输出。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _same_dir(a: object, b: object) -> bool:
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return False


def _writable(directory: Path) -> tuple[bool, str]:
    """能否在该目录里创建文件（不存在则尝试创建）。返回 (可行, 错误信息)。"""
    created: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=str(directory), prefix=".doctor_")
        os.close(fd)
        created = Path(name)
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        if created is not None:
            try:
                created.unlink()
            except OSError:
                pass


def _venv_python() -> Path:
    if os.name == "nt":
        return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# 一、环境
# ---------------------------------------------------------------------------
def _check_python() -> Check:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    if info >= (3, 10):
        return Check("环境", "Python 版本", OK, version)
    return Check(
        "环境", "Python 版本", FAIL, f"{version}（太旧）",
        "本项目需要 Python 3.10 或更高。请到 python.org/downloads 下载新版安装包，"
        "安装第一步务必勾选「Add python.exe to PATH」，装完重开一个窗口再试。")


def _check_venv() -> Check:
    venv_python = _venv_python()
    if not venv_python.is_file():
        return Check(
            "环境", "虚拟环境", FAIL, "没有找到 .venv",
            "先跑一遍安装：Windows 双击 install.bat，Mac 双击 install.command。"
            "它会自动建好 .venv 并装齐依赖。")
    if _same_dir(sys.prefix, PROJECT_ROOT / ".venv"):
        return Check("环境", "虚拟环境", OK, ".venv（当前正在使用它）")
    return Check(
        "环境", "虚拟环境", WARN, ".venv 存在，但你现在的 Python 不是它",
        "你可能是用系统 Python 跑的。建议改成双击 install.bat / install.command，"
        "或用项目里的 .venv 里的 Python 运行，否则可能缺依赖。")


def _check_dependencies() -> Check:
    import importlib.util

    missing = [pip_name for module, pip_name in _REQUIRED_MODULES
               if importlib.util.find_spec(module) is None]
    if not missing:
        return Check("环境", "依赖包", OK, f"{len(_REQUIRED_MODULES)} 个全部就绪")
    return Check(
        "环境", "依赖包", FAIL, f"缺 {len(missing)} 个：{'、'.join(missing)}",
        "双击 install.bat（Windows）/ install.command（Mac）重新装一遍，"
        "或手动执行：pip install -r requirements.txt")


def _check_chromium() -> Check:
    """浏览器内核是否下载好了 —— 没它就一个岗位也抓不到。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - 依赖缺失时给中文提示，不抛给用户
        return Check("环境", "浏览器内核", SKIP, "playwright 未安装，先修好「依赖包」",
                     f"（{type(exc).__name__}）")
    try:
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if exe and Path(exe).is_file():
            return Check("环境", "浏览器内核", OK, "Chromium 已就绪")
        return Check(
            "环境", "浏览器内核", FAIL, "Chromium 没有下载",
            "在项目目录里执行一句：playwright install chromium"
            "（约 130MB，装一次就够）。")
    except Exception as exc:  # noqa: BLE001
        return Check(
            "环境", "浏览器内核", FAIL, f"检查失败：{type(exc).__name__}",
            "执行：playwright install chromium。若反复失败，把这条报错发给我。")


#: 过盾和人机验证各依赖一个第三方域名。
#: 关键认知：网站本身打得开，不代表这两个域名打得开 —— 而验证能否走完，
#: 恰恰取决于它们。
_NET_PROBES: tuple[tuple[str, str], ...] = (
    ("Cloudflare 验证组件", "challenges.cloudflare.com"),
    ("人机验证组件", "www.google.com"),
)


def tcp_probe(host: str, port: int = 443, timeout: float = 2.5) -> bool:
    """能不能跟这个域名建立 TCP 连接（不做 TLS，只判断通不通）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _system_proxy() -> str:
    """读出系统里配的代理地址，没配返回空串。

    只用于诊断：「系统里明明配了代理，但程序默认不走」这件事，用户很难自己想到。
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if not enabled:
                    return ""
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                return str(server or "").strip()
        except (OSError, ImportError):
            return ""
    if sys.platform == "darwin":
        try:
            import subprocess

            out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                                 text=True, timeout=2).stdout
        except (OSError, ValueError):
            return ""
        host = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
        port = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
        if host:
            return f"{host.group(1)}:{port.group(1)}" if port else host.group(1)
    return ""


def _check_network(cfg: Optional[AppConfig]) -> Check:
    """安全验证要用的两个域名通不通 —— 这是「一直转圈」最常见的原因。

    为什么要专门查这一项：用户看到的是「转圈」，真实原因往往是「验证所需的
    那段脚本根本下载不下来」。前者让人怀疑自己配错了，后者是网络问题，
    两者的下一步完全不同，靠猜要猜很久。
    """
    unreachable = [name for name, host in _NET_PROBES if not tcp_probe(host)]
    proxy_setting = (cfg.browser_proxy if cfg is not None else "").strip()
    system_proxy = _system_proxy()

    if not unreachable:
        if proxy_setting and proxy_setting.lower() != "system":
            detail = f"验证组件都可达（经 {proxy_setting}）"
        elif proxy_setting.lower() == "system":
            detail = "验证组件都可达（跟随系统代理）"
        else:
            detail = "验证组件都可达"
        return Check("环境", "网络出口", OK, detail)

    detail = ("验证组件都连不上" if len(unreachable) == len(_NET_PROBES)
              else f"{unreachable[0]}连不上")

    if not proxy_setting and system_proxy:
        hint = (
            f"系统里配了代理（{system_proxy}），但程序默认直连、不读它。\n"
            '把 config.yaml 里 browser.proxy 改成 "system" 再跑一次；\n'
            "不想改文件就双击 setup.bat，在页面里改。")
    elif proxy_setting.lower() == "system":
        hint = (
            "已经设为跟随系统代理，目标仍然不通 —— 多半是代理软件没在运行，\n"
            "或当前节点连不到这两个域名。先确认代理软件开着，再换个节点试试。")
    elif proxy_setting:
        hint = (
            f"已经指定代理（{proxy_setting}），目标仍然不通。\n"
            "核对地址和端口是否与代理软件界面上显示的一致，以及软件是否在运行。")
    else:
        hint = (
            "直连不通，系统里也没有代理。你需要一条能访问这两个域名的通道：\n"
            "开代理软件的 TUN / 虚拟网卡模式（这种模式在网络层接管流量，\n"
            "程序不需要额外配置），或者换一个网络环境。")

    return Check("环境", "网络出口", WARN, detail, hint)


# ---------------------------------------------------------------------------
# 二、配置
# ---------------------------------------------------------------------------
def _check_config_file() -> tuple[Check, Optional[AppConfig]]:
    """读 config.yaml。返回 (检查结果, 解析到的配置或 None)。"""
    if not CONFIG_PATH.exists():
        return Check(
            "配置", "配置文件", FAIL, "没有找到 config.yaml",
            "把同目录下的 config.example.yaml 复制一份、改名为 config.yaml 即可"
            "（Windows 也可双击 install.bat，它会自动帮你复制）。"), None
    # 刻意用宽松模式：doctor 的职责就是「配置有问题时还能打开」，不能自己先被拦住
    try:
        cfg = load_config(require_runnable=False)
    except ConfigError as exc:
        message = str(exc)
        # PyYAML 的报错是英文的，新手看不懂。先给中文常见原因，再附原文用于定位行号。
        raw = message.split("：", 1)[-1] if "：" in message else message
        hint = ("常见原因有三种：① 缩进用了 Tab（YAML 只认空格）；② 某行漏了冒号；"
                "③ 引号只写了半边。\n"
                "对照 config.example.yaml 逐段比较最快；改不动的把下面这段报错发给我。\n"
                f"具体报错：{raw.strip()[:280]}")
        return Check("配置", "配置文件", FAIL, "内容有错，读不出来", hint), None
    except Exception as exc:  # noqa: BLE001
        return Check("配置", "配置文件", FAIL,
                     f"读取失败：{type(exc).__name__}", str(exc)), None
    return Check("配置", "配置文件", OK, str(CONFIG_PATH.name)), cfg


def _check_keywords(cfg: Optional[AppConfig]) -> Check:
    if cfg is None:
        return Check("配置", "关键词", SKIP, "先修好「配置文件」")
    if not cfg.keywords:
        return Check(
            "配置", "关键词", FAIL, "还没有填",
            "打开 config.yaml，找到 keywords: 那一行，把 [] 换成你自己的岗位名称，"
            "一行一个、用英文双引号包起来，例如：\n"
            "keywords:\n"
            "  - \"data analyst\"\n"
            "  - \"business analyst\"")
    total = len(cfg.keywords) + len(cfg.graduate_keywords)
    sample = "、".join(cfg.keywords[:3]) + ("…" if len(cfg.keywords) > 3 else "")
    if tier.is_premium():
        return Check("配置", "关键词", OK, f"{total} 个：{sample}")
    if cfg.tier_notes:      # 被免费版额度裁过，说明用户填超了
        return Check("配置", "关键词", WARN, f"填了 {total} 个，实际生效 7 个",
                     "免费版最多 7 个（含毕业生项目关键词）。"
                     "把不需要的删掉，留下最想找的几个。")
    return Check("配置", "关键词", OK, f"{total} 个：{sample}")


def _check_career_stage(cfg: Optional[AppConfig]) -> Check:
    if cfg is None:
        return Check("配置", "求职阶段", SKIP, "先修好「配置文件」")
    if cfg.career_stage not in CAREER_STAGE_JOB_TYPES:
        return Check(
            "配置", "求职阶段", FAIL, f"取值不认识：{cfg.career_stage}",
            "career_stage 只能是 student / graduate / both 三个值之一。")
    if not cfg.job_types:
        return Check("配置", "求职阶段", FAIL, "没有可抓的岗位类型",
                     "把 career_stage 设成 student / graduate / both 之一即可自动推导。")
    return Check("配置", "求职阶段", OK,
                 f"{cfg.career_stage} → {' / '.join(cfg.job_types)}")


def _check_profile() -> Check:
    if not PROFILE_PATH.exists():
        return Check(
            "配置", "个人画像", FAIL, "没有找到 data/profiles/profile.yaml",
            "把同目录的 profile.example.yaml 复制一份、改名为 profile.yaml，"
            "再按里面的说明填上你自己的背景。")
    try:
        import yaml

        raw = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return Check("配置", "个人画像", FAIL,
                     f"读不出来：{type(exc).__name__}", str(exc))
    if not isinstance(raw, dict):
        return Check("配置", "个人画像", FAIL, "文件格式不对（顶层应为键值结构）",
                     "对照 profile.example.yaml 检查缩进。")
    background = raw.get("background")
    text = background.strip() if isinstance(background, str) else ""
    if not text:
        return Check("配置", "个人画像", WARN, "background 段是空的",
                     "background 是 AI 打分的依据。不填的话分数会很粗，"
                     "等于白花钱调 AI。")
    if len(text) < _PROFILE_MIN_CHARS:
        return Check("配置", "个人画像", WARN, f"只有 {len(text)} 个字符，偏少",
                     "把你读什么专业、做过什么、想找什么方向写清楚，"
                     "AI 才知道该给哪些岗位打高分。")
    return Check("配置", "个人画像", OK, f"已填写（{len(text)} 个字符）")


# ---------------------------------------------------------------------------
# 三、凭据
# ---------------------------------------------------------------------------
def _check_api_key(cfg: Optional[AppConfig]) -> tuple[Check, str]:
    """返回 (检查结果, 解析到的 key)，避免后面联网检查再解析一次。"""
    if cfg is None:
        return Check("凭据", "AI 密钥", SKIP, "先修好「配置文件」"), ""
    key = (cfg.llm.resolve_api_key() or "").strip()
    if not key:
        return Check(
            "凭据", "AI 密钥", FAIL, "没找到",
            "两种填法任选一种：\n"
            "           ① 在项目目录新建 api_key.txt，里面只放 key 本身（推荐）\n"
            "           ② 设环境变量 DEEPSEEK_API_KEY\n"
            "没去申请过？到 platform.deepseek.com 注册 → API keys → 创建。"), ""
    source = "环境变量"
    if not os.environ.get(cfg.llm.api_key_env):
        source = "api_key.txt"
    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "已填写"
    return Check("凭据", "AI 密钥", OK, f"{masked}（来自 {source}）"), key


def _check_llm_connectivity(cfg: Optional[AppConfig], key: str, enabled: bool) -> Check:
    if not enabled:
        return Check("凭据", "AI 连通", SKIP, "已按 --no-llm 跳过")
    if cfg is None or not key:
        return Check("凭据", "AI 连通", SKIP, "先修好「AI 密钥」")
    try:
        from openai import OpenAI
    except Exception:  # noqa: BLE001
        return Check("凭据", "AI 连通", SKIP, "openai SDK 未安装")
    try:
        client = OpenAI(api_key=key, base_url=cfg.llm.base_url,
                        timeout=cfg.llm.timeout_seconds)
        started = time.time()
        resp = client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": "只回复两个字：可用"}],
            max_tokens=8,
            temperature=0,
        )
        elapsed = time.time() - started
        reply = ((resp.choices[0].message.content or "") if resp.choices else "").strip()
        return Check("凭据", "AI 连通", OK,
                     f"{cfg.llm.model} 回「{reply or '（空）'}」，耗时 {elapsed:.1f} 秒")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        detail = f"{name}: {str(exc)[:120]}"
        if "authentication" in name.lower() or "401" in str(exc):
            hint = "key 不被接受：多半是复制时少了/多了字符，或这个 key 已被删除。重新复制一次。"
        elif "insufficient" in str(exc).lower() or "402" in str(exc):
            hint = "账户余额不足，到 platform.deepseek.com 充值后再试。"
        elif "timeout" in name.lower() or "timed out" in str(exc).lower():
            hint = ("连不上服务器：检查网络能否访问 api.deepseek.com；"
                    "公司/学校网络可能需要代理。")
        else:
            hint = "检查 base_url 是否正确（默认 https://api.deepseek.com），以及网络是否通。"
        return Check("凭据", "AI 连通", FAIL, detail, hint)


def _check_channels(cfg: Optional[AppConfig]) -> Check:
    if cfg is None:
        return Check("凭据", "推送通道", SKIP, "先修好「配置文件」")
    if not cfg.notify.channels:
        return Check(
            "凭据", "推送通道", FAIL, "一个都没启用",
            "打开 config.yaml 的 notify 段，在 channels 里写一个：\n"
            "notify:\n"
            "  channels: [\"wecom\"]     # 或 wxpusher / email / telegram\n\n"
            "再填好它下面那几行。四个通道任选其一，完全平等。")
    try:
        from hk_job_radar.notify.dispatch import Notifier

        notifier = Notifier.from_config(cfg)
        passed, problems = [], []
        for channel in notifier.channels:
            good, message = channel.check()
            (passed if good else problems).append(
                channel.label if good else f"{channel.label}（{message}）")
    except Exception as exc:  # noqa: BLE001
        return Check("凭据", "推送通道", FAIL,
                     f"装配失败：{type(exc).__name__}", str(exc))
    if passed:
        detail = "、".join(passed)
        if problems:
            return Check("凭据", "推送通道", WARN, f"可用：{detail}；有问题：{'、'.join(problems)}",
                         "至少有一个能用就不影响收日报，但建议把出问题的那个也修好。")
        return Check("凭据", "推送通道", OK, detail)
    return Check("凭据", "推送通道", FAIL, "全部配置不全", "、".join(problems))


# ---------------------------------------------------------------------------
# 四、数据
# ---------------------------------------------------------------------------
def _check_data_dir() -> Check:
    good, error = _writable(DATA_DIR)
    if good:
        return Check("数据", "数据目录", OK, str(DATA_DIR))
    return Check("数据", "数据目录", FAIL, f"写不进去：{error}",
                 "多半是文件夹权限问题。把整个项目挪到「文档」或个人目录下再试，"
                 "不要放在 C 盘根目录或 Program Files 里。")


def _check_database() -> Check:
    try:
        from hk_job_radar.db import Database

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = Database(str(DB_PATH))
        row = db.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        total = int(row["n"]) if row else 0
        db.conn.close()
        return Check("数据", "数据库", OK, f"可以打开（已存 {total} 个岗位）")
    except Exception as exc:  # noqa: BLE001
        return Check("数据", "数据库", FAIL, f"{type(exc).__name__}: {str(exc)[:100]}",
                     "数据库文件可能损坏。可以先把它改名备份（data/ 下的 .db 文件），"
                     "下次运行会自动重建；已抓的岗位记录会丢失，但配置不受影响。")


def _check_browser_dir() -> Check:
    good, error = _writable(BROWSER_DIR)
    if good:
        return Check("数据", "浏览器档案", OK, str(BROWSER_DIR))
    return Check("数据", "浏览器档案", FAIL, f"写不进去：{error}",
                 "登录状态（Cookie）存在这里，写不进去会导致每次都要求重新登录。"
                 "检查文件夹权限，或把项目换个位置。")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def run_checks(*, llm_test: bool = True) -> list[Check]:
    """跑完全部 15 项体检，顺序即展示顺序。"""
    checks: list[Check] = []

    checks.append(_check_python())
    checks.append(_check_venv())
    checks.append(_check_dependencies())
    checks.append(_check_chromium())

    config_check, cfg = _check_config_file()
    checks.append(_check_network(cfg))
    checks.append(config_check)
    checks.append(_check_keywords(cfg))
    checks.append(_check_career_stage(cfg))
    checks.append(_check_profile())

    key_check, api_key = _check_api_key(cfg)
    checks.append(key_check)
    checks.append(_check_llm_connectivity(cfg, api_key, llm_test))
    checks.append(_check_channels(cfg))

    checks.append(_check_data_dir())
    checks.append(_check_database())
    checks.append(_check_browser_dir())
    return checks


def render(checks: list[Check], *, use_color: bool = False) -> str:
    """把检查结果排成给人看的一段文字（按【环境】/【配置】/【凭据】/【数据】分组）。"""
    lines = ["", "香港岗位雷达 · 一键自检", "─" * 52]
    current_group = ""
    for index, check in enumerate(checks, start=1):
        if check.group != current_group:
            current_group = check.group
            lines.append(f"【{current_group}】")
        lines.append(f"  {check.symbol} {index:>2}. {_pad(check.name, 12)}{check.detail}")
        if check.hint:
            # 只有第一行带「→」，续行缩进对齐到文字起始位置，
            # 这样多行示例（比如要填的 YAML 片段）看起来才像一段引言而不是一堆箭头。
            for offset, hint_line in enumerate(check.hint.splitlines()):
                prefix = "        → " if offset == 0 else "          "
                lines.append(f"{prefix}{hint_line}")

    counts = {status: sum(1 for c in checks if c.status == status)
              for status in (OK, WARN, FAIL, SKIP)}
    lines.append("─" * 52)
    summary = f"共 {len(checks)} 项：{counts[OK]} 项通过"
    if counts[WARN]:
        summary += f" · {counts[WARN]} 项提醒"
    if counts[FAIL]:
        summary += f" · {counts[FAIL]} 项需要处理"
    if counts[SKIP]:
        summary += f" · {counts[SKIP]} 项跳过"
    lines.append(summary)

    if counts[FAIL]:
        numbers = "、".join(str(i) for i, c in enumerate(checks, start=1) if c.status == FAIL)
        lines.append(f"❌ 请先处理第 {numbers} 项，按上面的「→」改完再跑一次。")
    elif counts[WARN]:
        lines.append("⚠️  没有必须修的问题，但建议看一眼带 ⚠️ 的几项。")
    else:
        lines.append("✅ 全部通过，可以开始用了（双击 start.bat / start.command）。")
    lines.append("")
    return "\n".join(lines)


def run_doctor(*, llm_test: bool = True, echo: bool = True) -> int:
    """执行体检并打印；返回退出码（有 ❌ 时为 1）。

    日志已在 cli.main() 里初始化过，这里不再重复 setup_logging（重复调用会
    叠加 sink，导致每条日志打印两遍）。
    """
    checks = run_checks(llm_test=llm_test)
    report = render(checks)
    if echo:
        print(report)
    else:
        logger.info("自检完成\n" + report)
    return 1 if any(c.status == FAIL for c in checks) else 0


__all__ = ["Check", "run_checks", "render", "run_doctor", "OK", "WARN", "FAIL", "SKIP"]
