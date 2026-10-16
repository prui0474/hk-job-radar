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
class WecomConfig:
    """企业微信群机器人（只有一串 webhook，最简单）。"""
    webhook: str = ""


@dataclass
class WxPusherConfig:
    """WxPusher：app_token + 若干 UID（关注公众号后得到的用户标识）。"""
    app_token: str = ""
    uids: list[str] = field(default_factory=list)


@dataclass
class EmailConfig:
    """SMTP 邮件。auth_code 是邮箱后台生成的【授权码】，不是登录密码。"""
    smtp_host: str = ""
    smtp_port: int = 465
    user: str = ""
    auth_code: str = ""
    to: list[str] = field(default_factory=list)
    use_ssl: bool = True


@dataclass
class NotifyConfig:
    """推送总配置。

    channels 是「要启用哪些通道」的列表，顺序就是用户的选择顺序——
    免费版只保留第一个（见 tier.apply_free_limits），高级版可以全开。
    四个通道完全平等，系统不预设谁优先。
    """
    channels: list[str] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    wecom: WecomConfig = field(default_factory=WecomConfig)
    wxpusher: WxPusherConfig = field(default_factory=WxPusherConfig)
    email: EmailConfig = field(default_factory=EmailConfig)


# ---------------------------------------------------------------------------
# 求职阶段 → 岗位类型过滤
#
# 原版把「只能实习/兼职」写死在代码里，那是因为作者本人是在读学生、持学生
# 签证，全职违法。但本产品要面向**所有**香港求职者，所以改成用户可选：
#
#   student  → 在读学生：只抓实习 / 兼职（学生签证的合规红线）
#   graduate → 已毕业：只抓全职（含管培生 / 毕业生项目）
#   both     → 毕业前后过渡期：全都要
#
# 「毕业生项目关键词」（管培生等）在任何模式下都放行——它们虽然标注 full-time，
# 但入职时间在毕业之后，届时已持 IANG，属于合法且高价值的投递目标。
# ---------------------------------------------------------------------------
CAREER_STAGE_JOB_TYPES: dict[str, list[str]] = {
    "student": ["internship", "part time"],
    "graduate": ["full time"],
    "both": ["internship", "part time", "full time"],
}

# 岗位类型 → 归一的「类型分类」名（与 keyword_filter.classify_job_type 的返回值对应）
_FULLTIME_ALIASES = {"full time", "full-time", "fulltime", "permanent"}
_PARTTIME_ALIASES = {"part time", "part-time", "parttime"}
_INTERNSHIP_ALIASES = {"internship", "intern", "co-op", "coop"}

# 「毕业生项目」关键词的识别词（管培生 / 毕业生培训计划等）。
# 这类词无论放在 keywords 还是 graduate_keywords 里，都一律用【全职】类型去搜——
# 它们在招聘网站上标注 full-time，但入职时间在毕业之后，届时已持 IANG，合法。
# 放在 config 而不是 keyword_filter 里，是为了让搜索过滤与初筛安全网共用同一套判断。
_GRADUATE_PROGRAMME_WORDS = (
    "management trainee", "graduate trainee", "graduate programme",
    "graduate program", "graduate scheme", "management associate",
    "管培生", "見習管理", "见习管理", "管理培訓", "管理培训",
)


def _looks_like_graduate_programme(keyword: str) -> bool:
    """关键词本身是不是「毕业生项目」类（如 management trainee）。"""
    k = str(keyword or "").strip().lower()
    return any(w in k for w in _GRADUATE_PROGRAMME_WORDS)


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
        # 刻意【不预置任何默认词】：关键词是「你想找什么」，只有用户自己知道，
        # 给个默认值反而会让人以为可以不改就直接用。留空时会由 _validate()
        # 给出中文提示，引导用户去填自己的岗位名称。
        default_factory=list
    )
    keyword_synonyms: dict[str, list[str]] = field(default_factory=dict)
    # 毕业生项目关键词（毕业后入职，如银行管培生）：这些岗位在招聘网站上
    # 标注为「全职（full-time）」，但属于「毕业后入职」——毕业转 IANG 后完全
    # 合法，且投递黄金期就是毕业前一年。因此它们用「全职」类型过滤抓取，
    # 与普通数据岗（用 job_types 实习/兼职过滤）分开处理，避免被误杀。
    graduate_keywords: list[str] = field(
        # 同样不预置：用不用管培生词，取决于用户是不是「毕业前一年」的在读学生。
        default_factory=list
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
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    feedback_poll_seconds: int = 60
    report_time: str = "09:00"
    # 高级版可配多个日报时间点；免费版只认上面的 report_time（每天 1 次），
    # 具体由 tier.effective_report_times() 决定，这里只负责把配置读进来。
    report_times: list[str] = field(default_factory=list)
    # 求职阶段：student / graduate / both（决定抓哪些岗位类型，见下方 career_stage 常量）
    career_stage: str = "student"
    # 免费版限制的提示语（由 tier.apply_free_limits 填充，供 UI/CLI 展示）
    tier_notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def telegram(self) -> TelegramConfig:
        """向后兼容别名：老代码/老文档里的 cfg.telegram 仍指向 notify.telegram。"""
        return self.notify.telegram

    # ------------------------------------------------------------------
    def enabled_sources(self) -> list[str]:
        """当前启用的抓取源列表（顺序固定，便于日志对照）。"""
        return [s for s in ("jobsdb", "linkedin", "indeed") if self.sources.get(s, False)]

    def all_keywords(self) -> list[str]:
        """全部待抓取关键词 = 普通关键词 + 毕业生项目关键词（顺序：普通在前）。"""
        return self.keywords + self.graduate_keywords

    def job_types_for_keyword(self, keyword: str) -> list[str]:
        """某关键词搜索时应使用的岗位类型过滤。

        毕业生项目关键词（管培生等）→ 用「全职」过滤（它们是毕业后入职的
        full-time 岗，合法）；其余关键词 → 用 job_types（由求职阶段决定）。

        判断依据有两条，任一条命中即按毕业生项目处理：
          ① 词在 graduate_keywords 列表里；
          ② 词本身长得像毕业生项目（如 "management trainee"）——
             这样用户即使把管培生词直接写进 keywords，也能被正确识别，
             不会因为它被 internship 过滤掉而白白漏岗。
        """
        if keyword in self.graduate_keywords or _looks_like_graduate_programme(keyword):
            return ["full time"]
        return self.job_types

    def allowed_job_type_classes(self) -> set[str]:
        """当前配置允许放行的岗位类型分类（keyword_filter 的「安全网」据此判定）。

        - ""（未标注类型）：始终放行，交给 LLM 判定——宁可多看，不可漏看；
        - "graduate programme"：始终放行（毕业后入职，任何阶段都合法且有价值）；
        - 其余按 job_types 逐个映射：写了 internship 才放行实习，写了 full time
          才放行全职。这正是 career_stage 三种模式生效的地方。
        """
        allowed: set[str] = {"", "graduate programme"}
        for jt in self.job_types:
            j = str(jt).strip().lower()
            if j in _INTERNSHIP_ALIASES:
                allowed.add("internship")
            elif j in _PARTTIME_ALIASES:
                allowed.add("part time")
            elif j in _FULLTIME_ALIASES:
                allowed.add("full time")
        return allowed

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
def _normalize_career_stage(value: object) -> str:
    """把 career_stage 归一成 student / graduate / both。

    写错或没写都落到 student —— 这是最保守的默认值：只抓实习/兼职，
    对学生签证用户绝对安全（宁可少抓，不可让学生收到违法全职岗）。
    """
    raw = str(value or "").strip().lower()
    aliases = {
        "student": "student", "在读": "student", "在读学生": "student", "学生": "student",
        "graduate": "graduate", "毕业": "graduate", "已毕业": "graduate", "毕业生": "graduate",
        "both": "both", "all": "both", "两者": "both", "都要": "both", "不限": "both",
    }
    stage = aliases.get(raw, "student")
    if raw and raw not in aliases:
        logger.warning(f"career_stage 取值「{value}」无法识别，已按 student 处理"
                       f"（可选：student / graduate / both）。")
    return stage


def _as_str_list(value: object) -> list[str]:
    """把配置里的「一个字符串」或「字符串列表」统一成去空的字符串列表。

    这样 email.to 既可以写 `to: "a@b.com"`，也可以写成
    `to:\n  - "a@b.com"`，对小白更宽容。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace(",", " ").replace(";", " ").split()
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _parse_notify(raw: dict) -> NotifyConfig:
    """解析 notify 段，并兼容老配置的顶层 telegram 段。

    兼容规则（很重要，别删）：老用户的 config.yaml 里只有顶层 `telegram:`，
    没有 `notify:`。此时自动当作「只启用 Telegram」——升级后不会突然收不到消息。
    """
    notify_raw = raw.get("notify")
    if notify_raw is None:
        notify_raw = {}
    if not isinstance(notify_raw, dict):
        raise ConfigError("config.yaml 的 notify 段必须是键值对结构（对照 config.example.yaml 检查）。")

    # Telegram：优先读 notify.telegram，找不到再回落到顶层 telegram（向后兼容）
    tg_raw = notify_raw.get("telegram")
    if not isinstance(tg_raw, dict):
        tg_raw = raw.get("telegram") if isinstance(raw.get("telegram"), dict) else {}
    we_raw = notify_raw.get("wecom") if isinstance(notify_raw.get("wecom"), dict) else {}
    wx_raw = notify_raw.get("wxpusher") if isinstance(notify_raw.get("wxpusher"), dict) else {}
    em_raw = notify_raw.get("email") if isinstance(notify_raw.get("email"), dict) else {}

    nc = NotifyConfig(
        telegram=TelegramConfig(
            bot_token=str(tg_raw.get("bot_token", "")).strip(),
            chat_id=str(tg_raw.get("chat_id", "")).strip(),
        ),
        wecom=WecomConfig(webhook=str(we_raw.get("webhook", "")).strip()),
        wxpusher=WxPusherConfig(
            app_token=str(wx_raw.get("app_token", "")).strip(),
            uids=_as_str_list(wx_raw.get("uids")),
        ),
        email=EmailConfig(
            smtp_host=str(em_raw.get("smtp_host", "")).strip(),
            smtp_port=int(em_raw.get("smtp_port", 465) or 465),
            user=str(em_raw.get("user", "")).strip(),
            auth_code=str(em_raw.get("auth_code", "")).strip(),
            to=_as_str_list(em_raw.get("to")),
            use_ssl=bool(em_raw.get("use_ssl", True)),
        ),
    )

    channels = [str(c).strip().lower() for c in (notify_raw.get("channels") or []) if str(c).strip()]
    if not channels and (nc.telegram.bot_token or nc.telegram.chat_id):
        # 老配置升级路径：只有顶层 telegram 段 → 只启用 Telegram
        channels = ["telegram"]
    nc.channels = channels
    return nc


def load_config(path: Path | None = None, *, require_runnable: bool = True) -> AppConfig:
    """读取 config.yaml → AppConfig。

    文件不存在时不抛异常：返回默认配置并打 WARNING（stats 等只读命令
    仍可用）；真正需要凭据的 run/report/once 由 require_notify() 把关。

    require_runnable：是否要求配置「足以真正开始抓取」。
    - 抓取类命令（run / once / report / notify test）用默认的 True，
      缺关键词、缺岗位类型时立刻给出中文报错，避免跑到一半才炸。
    - 只读类命令（stats）传 False：它只读数据库、顺带打印每日上限，
      不该因为用户还没填关键词就打不开。
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
    # 求职阶段决定「抓哪些岗位类型」。优先级：显式写 job_types > 用 career_stage 推导。
    # 保留显式 job_types 是为了不挡住高级用户（懂的人可以直接写类型列表）。
    cfg.career_stage = _normalize_career_stage(raw.get("career_stage"))
    if raw.get("job_types") is not None:
        cfg.job_types = [str(j).strip().lower() for j in (raw.get("job_types") or [])
                         if str(j).strip()]
    else:
        cfg.job_types = list(CAREER_STAGE_JOB_TYPES[cfg.career_stage])
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

    cfg.notify = _parse_notify(raw)
    cfg.feedback_poll_seconds = int(raw.get("feedback_poll_seconds", cfg.feedback_poll_seconds))
    cfg.report_time = str(raw.get("report_time", cfg.report_time)).strip()
    cfg.report_times = [str(t).strip() for t in (raw.get("report_times") or []) if str(t).strip()]

    _validate(cfg, require_runnable=require_runnable)

    # 免费版额度裁剪（延迟导入：tier 需要 config 的 PROJECT_ROOT，模块级导入会成环）
    from hk_job_radar import tier
    cfg.tier_notes = tier.apply_free_limits(cfg)
    tier.log_limits(cfg.tier_notes)
    return cfg


def _validate(cfg: AppConfig, *, require_runnable: bool = True) -> None:
    """防御性校验：把"启动 3 小时后才炸"的配置错误提前到启动瞬间。

    require_runnable=False 时只做「格式类」校验，跳过「能不能开始抓取」类校验
    （关键词是否填了、岗位类型是否有），供 stats 这类只读命令使用。
    """
    if require_runnable and not cfg.keywords:
        raise ConfigError(
            "keywords 还是空的：请打开 config.yaml，在 keywords: 下面填上你想找的岗位名称"
            "（英文小写，一行一个，如 \"data analyst\"）。模板刻意不预置默认词——"
            "「你想找什么」只有你自己知道，照抄别人的词只会抓错岗位。"
            "填法见 config.example.yaml 里的「填词四条规则」。")
    if require_runnable and not cfg.job_types:
        raise ConfigError(
            f"job_types 不能为空：请在 config.yaml 里设置 career_stage"
            f"（student = 实习/兼职、graduate = 全职、both = 都要），"
            f"或直接手写 job_types 列表（如 internship、part time）。")
    # 全职安全提示：只在「当前是在读学生」时提醒——因为只有这时全职才违法。
    # 选了 graduate / both 的用户本来就是奔着全职去的，反复警告只会烦人。
    if cfg.career_stage == "student" and any(jt in _FULLTIME_ALIASES for jt in cfg.job_types):
        logger.warning("career_stage=student 却允许 full time：全职对学生签证在读学生违法，"
                       "请确认你已毕业并取得 IANG 签证（或把 career_stage 改成 graduate/both）。")
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
    # 推送通道名合法性：写错名字会让"配了却收不到"变成无声故障，不如启动即报错
    from hk_job_radar import tier   # 延迟导入避免循环依赖
    unknown = [c for c in (cfg.notify.channels or []) if c not in tier.VALID_CHANNELS]
    if unknown:
        raise ConfigError(
            f"notify.channels 里有无法识别的通道名：{unknown}。"
            f"可选值为 {' / '.join(tier.VALID_CHANNELS)}。")
    # 高级版的多时间点日报；格式写错时静默失效最难查，所以逐个校验
    for t in cfg.report_times:
        if not re.fullmatch(r"\d{1,2}:\d{2}", t):
            raise ConfigError(f"report_times 里的「{t}」格式错误，应为 HH:MM，如 09:00、18:30。")


def require_notify(cfg: AppConfig) -> None:
    """run / report / once 的推送门禁：至少要有一个「配置完整」的通道。

    为什么要有这道门禁：推送是这个产品唯一的输出口。通道没配好时，程序会
    默默跑完、什么都不发——用户等了半天，还以为在正常工作。所以这里提前
    拦住，并用中文告诉他"差什么、去哪里拿"。
    """
    from hk_job_radar import tier
    from hk_job_radar.notify.dispatch import build_channel

    names = list(cfg.notify.channels or [])
    if not names:
        raise ConfigError(
            "还没有启用任何推送通道：请在 config.yaml 的 notify.channels 里写一个，"
            f"可选 {' / '.join(tier.VALID_CHANNELS)}（四者完全平等，任选其一）。\n"
            + _channel_setup_hint())

    problems: list[str] = []
    for name in names:
        channel = build_channel(name, cfg)
        if channel is None:
            problems.append(f"「{name}」不是合法通道名")
            continue
        ok, message = channel.check()
        if ok:
            return          # 有一个通道可用就够了，直接放行
        problems.append(f"{tier.channel_label(name)} —— {message}")

    raise ConfigError(
        "已启用的推送通道都还没配好，消息发不出去：\n  · "
        + "\n  · ".join(problems)
        + "\n\n" + _channel_setup_hint())


def _channel_setup_hint() -> str:
    """每个通道「去哪拿配置」的中文指引（复制粘贴级）。"""
    return (
        "各通道怎么配（config.yaml 的 notify 段）：\n"
        "  企业微信 wecom：手机装「企业微信」→ 建一个只有自己的群 → 群设置里加「群机器人」\n"
        "                  → 复制它的 Webhook 地址，填进 notify.wecom.webhook\n"
        "  WxPusher：打开 wxpusher.zjiecode.com → 微信扫码登录 → 创建应用拿 appToken\n"
        "             → 在「用户管理」里复制你的 UID，填进 notify.wxpusher.app_token / uids\n"
        "  邮件 email：登录你的邮箱网页版 → 设置里开启 SMTP → 生成【授权码】（不是登录密码）\n"
        "              → 填 notify.email.smtp_host / user / auth_code / to\n"
        "  Telegram：与 @BotFather 对话创建机器人拿 token；给它发一条 /start 后访问\n"
        "            https://api.telegram.org/bot<token>/getUpdates 取 chat_id"
    )


def require_telegram(cfg: AppConfig) -> None:
    """（已弃用）旧名字，保留以免外部脚本报错；实际转调 require_notify。"""
    require_notify(cfg)


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
    "CAREER_STAGE_JOB_TYPES",
    "ConfigError", "AppConfig", "TelegramConfig", "NotifyConfig",
    "WecomConfig", "WxPusherConfig", "EmailConfig", "LLMConfig",
    "load_config", "require_notify", "require_telegram", "require_llm_key",
    "setup_logging",
]
