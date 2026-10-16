"""配置加载、校验与全局日志初始化（架构文档 T01 / 第 8 节共享约定）。

设计要点：
1. 唯一配置入口 config.yaml（复制自 config.example.yaml）；
2. 凭据永不入 git：API key 只从环境变量读取（api_key_env 指定变量名）；
3. 防御性校验：配置缺失/写错时，用中文给出可操作的错误提示，而不是让
   用户面对一长串英文 traceback（本产品用户是学习者，报错可读性 = 产品质量）；
4. 配置只在进程启动时读取一次，重启生效（PRD P0-7 明确允许）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 注意：os.name == "nt" 判断 Windows，比 sys.platform 更直接（本文件已用 os）

import yaml
from loguru import logger

from hk_job_radar.models import HK_TZ

# ---------------------------------------------------------------------------
# 路径常量：从本文件位置反推项目根目录。
# 为什么不用 os.getcwd()：用户从任意目录启动 CLI 时 cwd 不可靠，
# 而 config.py 的物理位置是固定的，据此推导最稳。
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # .../hk_job_radar/
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "radar.db"
PROFILE_PATH = DATA_DIR / "profiles" / "profile.yaml"


def _resolve_browser_dir() -> Path:
    """浏览器档案根目录（每个站点在它下面各占一个子目录）。

    默认就是项目内 data/browser/（好处：整个文件夹删掉 = 卸载干净）。
    唯一的例外：**Windows 上如果项目路径含非 ASCII 字符**（比如用户名叫
    「张三」，路径是 C:\\Users\\张三\\Desktop\\hk_job_radar\\），就改用系统目录。

    为什么只对 Windows 兜底：Chromium 在 Windows 下对非 ASCII 的
    user_data_dir 支持不稳定，可能出现启动失败或档案写坏；macOS/Linux 的
    文件系统原生 UTF-8，不存在这个问题，所以不改变它们的默认行为（也就不会
    让已有用户的登录状态"搬家"）。
    兜底位置选 %LOCALAPPDATA%：属「本机应用数据」，不会被 OneDrive 同步，
    也不会因为移动项目文件夹而失效。
    """
    preferred = DATA_DIR / "browser"
    if os.name != "nt":
        return preferred
    try:
        str(PROJECT_ROOT).encode("ascii")
    except UnicodeEncodeError:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "hk_job_radar" / "browser"
    return preferred


BROWSER_DIR = _resolve_browser_dir()


class ConfigError(Exception):
    """配置错误：携带中文提示，cli.py 捕获后直接打印给用户。"""


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: int = 20

    def resolve_api_key(self) -> str:
        """解析 API key：环境变量优先，其次项目根目录的 api_key.txt。

        为什么要回落读 api_key.txt：本项目面向「双击 bat 的新手」，bat 会把
        api_key.txt 读进环境变量；但直接用 `python run.py once` 的人没有 bat
        这一步，若不回落就会出现「明明填了 api_key.txt，却说未配置」的怪现象。
        用 utf-8-sig 读取，可顺带吃掉记事本可能写进去的 BOM。
        """
        key = os.environ.get(self.api_key_env, "").strip()
        if key:
            return key
        try:
            return (PROJECT_ROOT / "api_key.txt").read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""


@dataclass
class AppConfig:
    """全量配置（含所有默认值，保证"缺配置也能跑基础命令"）。"""

    sources: dict[str, bool] = field(
        default_factory=lambda: {"jobsdb": True, "linkedin": False, "indeed": False}
    )
    keywords: list[str] = field(
        # 兜底默认值，只在 config.yaml 完全没写 keywords 时生效。
        # 这里刻意放【通用示例】（与 config.example.yaml 保持一致），
        # 实际使用请填你自己的求职方向，见 config.example.yaml 顶部的填词四规则。
        default_factory=lambda: ["business analyst", "product manager"]
    )
    keyword_synonyms: dict[str, list[str]] = field(default_factory=dict)
    # 毕业生项目关键词（毕业后入职，如银行管培生）：这些岗位在招聘网站上
    # 标注为「全职（full-time）」，但属于「毕业后入职」——毕业转 IANG 后完全
    # 合法，且投递黄金期就是毕业前一年。因此它们用「全职」类型过滤抓取，
    # 与普通数据岗（用 job_types 实习/兼职过滤）分开处理，避免被误杀。
    graduate_keywords: list[str] = field(
        default_factory=lambda: ["management trainee", "graduate trainee",
                                 "graduate programme", "graduate program",
                                 "management associate"]
    )
    # 关键词轮转（P0-2 配套）：初筛是"逐关键词搜索"，每搜索一个关键词就吃 1–2 页
    # 每日配额（配额按香港自然日 0 点重置，不用就作废）。"每轮抓几个关键词"应与
    # 开机习惯匹配：电脑常开 → auto（把配额摊到全天）；每天只开一小会儿 → all
    # （开一次就全扫一遍，反正配额不用白不用）。
    # 取值："auto"（按每日配额 ÷ 每日轮数自动推算）| "all"（每轮全抓）| "5"（固定个数）
    keyword_rotation_per_cycle: str = "auto"
    # 按站点覆盖上面的值（如 LinkedIn 对自动化流量最敏感 → 小步慢走）。留空即不为该站覆盖。
    keyword_rotation_per_source: dict[str, str] = field(default_factory=dict)
    # 岗位类型过滤（合规红线）：学生签证在读期间只能实习/兼职，全职违法。
    # 该列表会动态映射成各站自己的 URL 过滤参数，并在 scoring/keyword_filter.py
    # 里再加一道"岗位类型安全网"兜底，双保险杜绝全职岗位进入流水线。
    job_types: list[str] = field(
        default_factory=lambda: ["internship", "part time"]
    )
    location: str = "Hong Kong"
    poll_interval_minutes: int = 20
    human_delay_min_s: float = 2.0
    human_delay_max_s: float = 8.0
    daily_limits: dict[str, int] = field(
        default_factory=lambda: {"jobsdb": 60, "linkedin": 40, "indeed": 40}
    )
    browser_headless: bool = False
    cloudflare_wait_seconds: int = 45
    llm: LLMConfig = field(default_factory=LLMConfig)
    score_threshold: int = 60
    realtime_threshold: int = 80
    show_unscored: bool = True
    max_job_age_days: int = 2   # 岗位新鲜度上限：只保留最近 N 天内发布的岗位
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    feedback_poll_seconds: int = 60
    report_time: str = "09:00"

    def enabled_sources(self) -> list[str]:
        """当前启用的抓取源列表（顺序固定，便于日志对照）。"""
        return [s for s in ("jobsdb", "linkedin", "indeed") if self.sources.get(s, False)]

    def all_keywords(self) -> list[str]:
        """全部待抓取关键词 = 普通关键词 + 毕业生项目关键词（顺序：普通在前）。"""
        return self.keywords + self.graduate_keywords

    def job_types_for_keyword(self, keyword: str) -> list[str]:
        """某关键词搜索时应使用的岗位类型过滤。

        毕业生项目关键词（管培生等）→ 用「全职」过滤（它们是毕业后入职的
        full-time 岗，合法）；其余关键词 → 用 job_types（默认实习/兼职）。
        """
        if keyword in self.graduate_keywords:
            return ["full time"]
        return self.job_types

    def keywords_per_cycle_for_source(self, source: str) -> int:
        """本轮该站点抓几个关键词；返回 0 表示「不轮转，抓全部关键词」。

        优先级：per_source 覆盖 > per_cycle（全局）。
        - "all" / "0" → 0（每轮抓全部）
        - 数字 N      → N（每轮固定抓 N 个）
        - "auto"      → 每日页数上限 ÷ 每日轮数 = 每轮可用的页数预算，
                        直接取该预算作为每轮关键词个数（每关键词至少 1 页），
                        让配额刚好被摊满一整天，而不是在前几小时烧光。

        举例（轮询 30 分钟 → 一天 48 轮）：
        - jobsdb：150 ÷ 48 = 3 → 每轮抓 3 个关键词，一天刚好用完 144 页；
        - linkedin：40 ÷ 48 = 0 → 兜底为 1 个，一天约覆盖 40 个关键词搜索。

        ⚠️ 注意：每关键词还要乘上该站的翻页数（LinkedIn 是 2 页/关键词），
        所以配额消耗 = 关键词数 × 翻页数，见 scrapers 里各适配器的 pages_per_keyword。
        """
        total = len(self.all_keywords())
        raw = str(self.keyword_rotation_per_source.get(source)
                  or self.keyword_rotation_per_cycle or "auto").strip().lower()
        if raw in ("all", "0", "off", "false", "none"):
            return 0                      # 不轮转：每轮抓全部
        if raw.isdigit():
            n = int(raw)
            return 0 if n <= 0 or n >= total else n
        # auto（默认）：按配额反推
        limit = int(self.daily_limits.get(source, 0) or 0)
        if limit <= 0 or total <= 0:
            return 0
        cycles_per_day = max(1, 1440 // max(1, self.poll_interval_minutes))
        return max(1, min(total, limit // cycles_per_day))


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------
def load_config(path: Path | None = None) -> AppConfig:
    """读取 config.yaml → AppConfig。

    文件不存在时不抛异常：返回默认配置并打 WARNING（stats 等只读命令
    仍可用）；真正需要凭据的 run/report 由 require_telegram() 把关。
    """
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        logger.warning(f"未找到 {cfg_path.name}，使用内置默认配置。"
                       f"请复制 config.example.yaml 为 config.yaml 并填写。")
        return AppConfig()

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 格式有误（YAML 解析失败）：{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 顶层必须是键值对结构，请对照 config.example.yaml 检查。")

    cfg = AppConfig()
    cfg.sources = {**cfg.sources, **(raw.get("sources") or {})}
    cfg.keywords = [str(k).strip().lower() for k in raw.get("keywords", cfg.keywords) if str(k).strip()]
    cfg.keyword_synonyms = {
        str(k).strip().lower(): [str(v).strip().lower() for v in vs]
        for k, vs in (raw.get("keyword_synonyms") or {}).items()
    }
    cfg.job_types = [str(j).strip().lower() for j in raw.get("job_types", cfg.job_types)
                     if str(j).strip()]
    cfg.graduate_keywords = [str(k).strip().lower() for k in
                             raw.get("graduate_keywords", cfg.graduate_keywords)
                             if str(k).strip()]
    rotation_raw = raw.get("keyword_rotation")
    if isinstance(rotation_raw, dict):
        if rotation_raw.get("per_cycle") is not None:
            cfg.keyword_rotation_per_cycle = str(rotation_raw.get("per_cycle")).strip().lower()
        per_source = rotation_raw.get("per_source")
        if isinstance(per_source, dict):
            cfg.keyword_rotation_per_source = {
                str(k).strip().lower(): str(v).strip().lower()
                for k, v in per_source.items() if str(v).strip()
            }
    cfg.location = str(raw.get("location", cfg.location))

    cfg.poll_interval_minutes = int(raw.get("poll_interval_minutes", cfg.poll_interval_minutes))
    hd = raw.get("human_delay") or {}
    cfg.human_delay_min_s = float(hd.get("min_s", cfg.human_delay_min_s))
    cfg.human_delay_max_s = float(hd.get("max_s", cfg.human_delay_max_s))

    cfg.daily_limits = {**cfg.daily_limits, **{
        str(k): int(v) for k, v in (raw.get("daily_limits") or {}).items()
    }} if isinstance(raw.get("daily_limits"), dict) else cfg.daily_limits

    br = raw.get("browser") or {}
    cfg.browser_headless = bool(br.get("headless", cfg.browser_headless))
    cfg.cloudflare_wait_seconds = int(br.get("cloudflare_wait_seconds", cfg.cloudflare_wait_seconds))

    llm_raw = raw.get("llm") or {}
    cfg.llm = LLMConfig(
        provider=str(llm_raw.get("provider", cfg.llm.provider)),
        base_url=str(llm_raw.get("base_url", cfg.llm.base_url)),
        model=str(llm_raw.get("model", cfg.llm.model)),
        api_key_env=str(llm_raw.get("api_key_env", cfg.llm.api_key_env)),
        timeout_seconds=int(llm_raw.get("timeout_seconds", cfg.llm.timeout_seconds)),
    )

    cfg.score_threshold = int(raw.get("score_threshold", cfg.score_threshold))
    cfg.realtime_threshold = int(raw.get("realtime_threshold", cfg.realtime_threshold))
    cfg.show_unscored = bool(raw.get("show_unscored", cfg.show_unscored))
    cfg.max_job_age_days = int(raw.get("max_job_age_days", cfg.max_job_age_days))

    tg_raw = raw.get("telegram") or {}
    cfg.telegram = TelegramConfig(
        bot_token=str(tg_raw.get("bot_token", "")).strip(),
        chat_id=str(tg_raw.get("chat_id", "")).strip(),
    )
    cfg.feedback_poll_seconds = int(raw.get("feedback_poll_seconds", cfg.feedback_poll_seconds))
    cfg.report_time = str(raw.get("report_time", cfg.report_time)).strip()

    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    """防御性校验：把"启动 3 小时后才炸"的配置错误提前到启动瞬间。"""
    if not cfg.keywords:
        raise ConfigError("keywords 不能为空：请在 config.yaml 中至少配置一个关键词。")
    if not cfg.job_types:
        raise ConfigError("job_types 不能为空：学生签证在读期间必须只抓实习/兼职，"
                          "请在 config.yaml 中至少配置一个岗位类型（如 internship、part time）。")
    # 全职有多种写法（full time / full-time / fulltime / permanent），
    # 只要出现任一写法都提醒一句（不阻断：毕业后持有 IANG 的用户确需全职）。
    _fulltime_variants = {"full time", "full-time", "fulltime", "permanent"}
    if any(jt in _fulltime_variants for jt in cfg.job_types):
        logger.warning("job_types 含全职类型（full time）：全职对学生签证在读学生违法，"
                       "请确认你已毕业并取得 IANG 签证后再开启。")
    # 关键词轮转取值校验：写错的话轮转会静默失效，不如启动即报错
    rot_values = {"per_cycle": cfg.keyword_rotation_per_cycle,
                  **{f"per_source.{k}": v for k, v in cfg.keyword_rotation_per_source.items()}}
    for label, value in rot_values.items():
        rot = str(value or "auto").strip().lower()
        if rot not in ("auto", "all") and not rot.isdigit():
            raise ConfigError(
                f"keyword_rotation.{label} 取值非法（{value}）：只支持 auto（自动推算）、"
                f"all（每轮全抓）或一个正整数（每轮抓几个关键词）。")
        if rot.isdigit() and int(rot) <= 0:
            raise ConfigError(f"keyword_rotation.{label} 若写数字，必须 >= 1。")
    if not (15 <= cfg.poll_interval_minutes <= 60):
        # PRD 推荐 15-30 分钟；放宽到 60 上限但更频繁会明显增加封号风险
        raise ConfigError(f"poll_interval_minutes={cfg.poll_interval_minutes} 不合理，"
                          f"推荐 15-30（防封号要求）。")
    if cfg.human_delay_min_s < 1 or cfg.human_delay_max_s < cfg.human_delay_min_s:
        raise ConfigError("human_delay 配置错误：要求 min_s >= 1 且 max_s >= min_s。")
    if not re.fullmatch(r"\d{1,2}:\d{2}", cfg.report_time):
        raise ConfigError(f"report_time 格式错误（{cfg.report_time}），应为 HH:MM，如 09:00。")
    hh, mm = cfg.report_time.split(":")
    if int(hh) > 23 or int(mm) > 59:
        raise ConfigError(f"report_time 数值越界（{cfg.report_time}）。")
    if not (0 <= cfg.score_threshold <= 100 and 0 <= cfg.realtime_threshold <= 100):
        raise ConfigError("score_threshold / realtime_threshold 必须在 0-100 之间。")
    if cfg.max_job_age_days < 1:
        raise ConfigError("max_job_age_days 必须 >= 1（岗位新鲜度上限，单位天）。")
    if cfg.realtime_threshold < cfg.score_threshold:
        raise ConfigError("realtime_threshold 应 >= score_threshold（实时推送的岗位必然进日报）。")


def require_telegram(cfg: AppConfig) -> None:
    """run / report 命令的凭据门禁：缺 token 时用中文提示并中止。"""
    if not cfg.telegram.bot_token or not cfg.telegram.chat_id:
        raise ConfigError(
            "Telegram 推送未配置：请在 config.yaml 的 telegram 段填写 bot_token 与 chat_id。\n"
            "  1) 与 @BotFather 对话创建机器人获取 token；\n"
            "  2) 给你的机器人发一条 /start，再访问\n"
            "     https://api.telegram.org/bot<你的token>/getUpdates 找到 chat_id。"
        )
    if not re.fullmatch(r"\d+:[\w-]+", cfg.telegram.bot_token):
        raise ConfigError("telegram.bot_token 格式不像合法 token（应形如 123456789:ABCdef...）。")


def require_llm_key(cfg: AppConfig) -> None:
    """run / once 命令的 LLM 门禁：key 缺失时给出可直接照做的三种补法。"""
    if not cfg.llm.resolve_api_key():
        raise ConfigError(
            f"未配置 {cfg.llm.api_key_env}（DeepSeek 的 key），LLM 打分无法工作。\n"
            f"\n"
            f"  方式一 · 最省事（推荐）：在项目文件夹里新建一个文本文件，\n"
            f"      命名为 api_key.txt，打开后只写你那一串 key（sk- 开头），保存。\n"
            f"      程序会自动读取它 —— 无论你是双击 bat，还是直接跑 python run.py。\n"
            f"\n"
            f"  方式二 · 临时（只在当前 PowerShell 窗口有效）：\n"
            f"      $env:{cfg.llm.api_key_env} = \"sk-你的key\"\n"
            f"\n"
            f"  方式三 · 永久（写入用户级环境变量，之后新开的窗口都生效）：\n"
            f"      setx {cfg.llm.api_key_env} \"sk-你的key\"\n"
            f"\n"
            f"  还没有 key？打开 https://platform.deepseek.com 注册登录，\n"
            f"  左侧点 API Keys → 创建，复制那串 sk- 开头的文字。"
        )


# ---------------------------------------------------------------------------
# 日志初始化：loguru 按天滚动，保留 30 天（架构第 8 节）
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    """全局日志初始化。必须在 cli.main() 最先调用，之后各模块直接
    from loguru import logger 即可（loguru 是全局单例，无需传递 logger 对象）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()   # 移除默认的 stderr sink，统一由我们接管输出格式
    # 控制台：给人看的，保留彩色
    logger.add(
        lambda msg: print(msg, end=""),
        format="{time:HH:mm:ss} | {level} | {name} | {message}",
        level="INFO",
    )
    # 文件：给排障用的，按天滚动、保留 30 天、UTF-8（Windows 默认 GBK 会写坏中文）
    logger.add(
        LOG_DIR / "radar_{time:YYYY-MM-DD}.log",
        format="{time:HH:mm:ss} | {level} | {name} | {message}",
        rotation="00:00",       # 每天零点切新文件
        retention="30 days",
        encoding="utf-8",
        level="DEBUG",
        enqueue=True,           # 异步写入，避免高频日志阻塞抓取主线程
    )


# 时区对象也在此导出，供 scheduler 等模块复用（单一来源）
__all__ = [
    "HK_TZ", "PROJECT_ROOT", "CONFIG_PATH", "DATA_DIR", "DB_PATH", "PROFILE_PATH",
    "BROWSER_DIR",
    "ConfigError", "AppConfig", "TelegramConfig", "LLMConfig",
    "load_config", "require_telegram", "require_llm_key", "setup_logging",
]
