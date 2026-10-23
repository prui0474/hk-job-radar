"""标准数据结构定义（架构文档 4.2 节）。

为什么单独一个 models.py：
三站页面结构异构（C2 挑战），适配器模式要求所有站点把抓到的原始数据
"翻译"成统一的 JobPosting，后续的筛选、打分、入库、推送只认这个结构——
这样某一站页面改版只需要修补对应的适配器，不会波及其它模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

# 全项目唯一时区对象：香港时间（架构第 8 节共享约定）。
# 为什么统一到模块级常量而不是各处手写字符串：避免拼错、避免混用 UTC。
HK_TZ = ZoneInfo("Asia/Hong_Kong")


def now_hk() -> datetime:
    """返回当前香港时间（带时区信息）。所有"现在"都必须来自这里。"""
    return datetime.now(HK_TZ)


def to_iso(dt: datetime) -> str:
    """任意时间 → 香港时区 ISO 8601 字符串（数据库统一存储格式）。"""
    if dt.tzinfo is None:
        # 抓取器解析出来的 naive 时间默认当作香港本地时间处理
        dt = dt.replace(tzinfo=HK_TZ)
    return dt.astimezone(HK_TZ).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime | None:
    """ISO 8601 字符串 → datetime；解析失败返回 None（调用方自行兜底）。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class JobPosting:
    """岗位标准结构：三站适配器的统一输出、SQLite jobs 表的一行。"""

    source: str                      # "jobsdb" | "linkedin" | "indeed"
    external_id: str                 # 站内岗位 ID（或规范化 URL 哈希）
    title: str
    company: str
    url: str                         # 原始链接（保留跟踪参数，方便点开）
    url_normalized: str              # 去跟踪参数的规范化 URL（去重键之一）
    salary_text: str = ""            # 薪资原文，如 "35-45K/月"；抓不到留空
    location: str = ""               # 如 "中环" / "Hong Kong"
    posted_at: datetime | None = None    # 发布时间；解析失败为 None
    description_snippet: str = ""    # 摘要前 500 字符（喂给 LLM 的素材）
    keyword_matched: str = ""        # 初筛命中的关键词（证据，便于排查误杀）
    llm_score: int | None = None     # None = 未打分（LLM 失败降级，照常进日报）
    llm_reason: str = ""             # LLM 给出的一句话匹配理由
    direct_apply: bool = False       # 是否官网直投（Workday/Greenhouse/Lever）
    status: str = "new"              # new → reported → interested/rejected → applied
    first_seen_at: datetime = field(default_factory=now_hk)  # 首次发现时间
    # 来源已明确告知「这个岗位还在收简历」（目前只有 Workday 的 canApply 字段能给）。
    # 为 True 时允许它跳过「岗位新鲜度」那道年龄闸门——既然能确认还在招，
    # 就不该因为挂得久被当成旧岗位扔掉。
    verified_open: bool = False
    # 资格提示：岗位里明确写出、而你可能不满足的条件（「须為香港永久居民」「粵語流利」等）。
    # 空字符串 = 没发现。★ 它只用来**提示**，不参与任何过滤与打分——
    # 有些门槛是硬的（永久居民身份），有些是软的（粤语可以边学边投），
    # 能不能投只有本人判断得了，程序不替他做这个决定。
    eligibility_note: str = ""
    # 跨站重复指纹 = 归一化后的「公司名|职位名」。
    # 入库时算好存起来，日报与实时推送都靠它认出「这其实是同一个岗位」。
    # NO_KEY（"-"）= 信息不全，无法判断（见 dedup.py）。
    dedupe_key: str = ""
    # 显示用：本条与日报里第几号是同一岗位。不入库——序号每次日报都变。
    duplicate_note: str = ""

    @property
    def job_uid(self) -> str:
        """全局唯一键 = 来源 + 站内 ID（SQLite 主键、反馈按钮回传都用它）。"""
        return f"{self.source}:{self.external_id}"


@dataclass
class ScoringResult:
    """LLM 打分结果：强制 JSON 输出 {"score": 0-100, "reason": "...", "eligibility": "..."}。"""

    score: int
    reason: str
    # 岗位明确写出、而候选人可能不满足的条件；没发现就留空。
    # 只做提示，绝不影响上面的 score——理由见 JobPosting.eligibility_note 的注释。
    eligibility: str = ""


@dataclass
class ApplicationRecord:
    """投递留档（P1-8，T05 副驾写入；P0 先定义好结构占位）。"""

    job_uid: str
    company: str
    title: str
    applied_date: str                # ISO 日期字符串（香港时区）
    channel: str                     # workday / greenhouse / lever / jobsdb / ...
    resume_version: str              # 使用的简历版本文件名
    status: str = "applied"          # applied / interview / offer / rejected
