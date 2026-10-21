"""关键词初筛（P0-3）：标题/摘要命中关键词或同义词才放行。

设计动机（为什么需要这一层）：
LLM 打分按次计费且串行耗时，搜索"数据"关键词会带回大量销售、运营、
客服岗——初筛用近乎零成本的字符串匹配把这些噪音挡掉，让 LLM 只看
值得看的岗位。宁可窄一点漏掉边缘岗（LLM 打分兜底召回），不可宽到
日报全是噪音。
"""
from __future__ import annotations

import re

from loguru import logger

from hk_job_radar.config import AppConfig
from hk_job_radar.models import JobPosting

# 内置默认同义词表：默认【空】。
# 为什么默认空：同义词高度依赖个人求职方向（"product manager ↔ product owner"
# 只对找产品岗的人有意义），把它写进代码等于把某个人的求职方向硬编码进产品。
# 因此统一由 config.yaml 的 keyword_synonyms 提供；这里保留空表只是为了保留
# "内置兜底 + 配置覆盖"的合并逻辑（见 load_synonyms）。
#
# 填同义词时的铁律：只放「岗位名称」的变体，不放技能名词——匹配范围包含岗位描述，
# 放 "python" / "power bi" 之类的技能词等于取消初筛。
_DEFAULT_SYNONYMS: dict[str, list[str]] = {}

# ---------------------------------------------------------------------------
# 岗位类型硬过滤标记（合规红线：学生签证在读期间只能实习/兼职，全职违法）。
#
# 判定优先级：命中「实习」或「兼职」→ 保留；命中「毕业生项目」→ 保留
# （毕业后入职的全职岗，如银行管培生，合法）；否则命中「全职」→ 剔除；
# 都不命中 → 保留（URL 过滤是主防线，这里是兜底，宁可保留模糊项交 LLM
# 判定，也不误删）。
#
# 为什么英文用 \b 词边界：裸子串 "intern" 会误伤 internal/international、
# "coop" 会误伤 cooperative/cooperation。用词边界后 "Full-time Intern" 依然
# 命中 intern（保留，实习合法），而 "Internal Auditor" 不会被误判成实习。
# ---------------------------------------------------------------------------
_INTERNSHIP_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"\binterns?\b"),          # intern / interns
    re.compile(r"\binternships?\b"),      # internship / internships
    re.compile(r"\bco-?op\b"),            # co-op / coop
    re.compile(r"實習|实习"),
]

_PARTTIME_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"part[- ]time"),          # part-time / part time
    re.compile(r"parttime"),
    re.compile(r"兼職|兼职"),
]

# 毕业生项目（毕业后入职的全职岗）：银行管培生 / 毕业生培训计划等。
# 它们在招聘网站上标注为 full-time，但入职时间在毕业之后（届时已持 IANG），
# 因此不违反学生签证在读期间的「不可全职」限制，应放行而非剔除。
_GRADUATE_PROGRAMME_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"management trainee"),
    re.compile(r"graduate trainee"),
    re.compile(r"graduate programme"),
    re.compile(r"graduate program"),
    re.compile(r"management associate"),
    re.compile(r"管培生|見習管理|见习管理|管理培訓|管理培训"),
]

_FULLTIME_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"full[- ]time"),          # full-time / full time
    re.compile(r"fulltime"),
    re.compile(r"permanent"),             # 香港语境 permanent 通常=全职长工
    re.compile(r"全職|全职"),
]


class KeywordFilter:
    """关键词 + 同义词初筛器。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.synonyms = self.load_synonyms()
        #: 短词（≤4 个纯字母/数字）不能当裸子串匹配。
        #: 实测踩过：关键词 "llm" 匹配到了 "fu**llm**ent"（fulfillment），
        #: 于是一条「Senior Manager, Sales Support」被判成 AI 岗推进日报。
        #: 这类词只有 3 个字母，嵌在别的单词里几乎是必然，必须加词首边界。
        #: 用「词首」而不是「两端」边界：`\bllm` 仍能命中 "LLMs"、"LLM-powered"，
        #: 而 "fulfillment" 里那个 llm 前面不是词边界，会被挡掉。
        self._short_keywords = {
            kw for kw in config.all_keywords()
            if len(kw) <= 4 and kw.isascii() and kw.isalnum()
        }
        self._short_patterns = {
            kw: re.compile(r"\b" + re.escape(kw), re.I) for kw in self._short_keywords
        }

    # ------------------------------------------------------------------
    def load_synonyms(self) -> dict[str, list[str]]:
        """加载同义词映射：配置文件条目覆盖内置默认（同一主关键词时），
        内置表中配置没有覆盖的主关键词保留默认同义词。"""
        merged = {k: list(v) for k, v in _DEFAULT_SYNONYMS.items()}
        for main_kw, variants in self.config.keyword_synonyms.items():
            merged[main_kw] = list(variants)
        # 过滤掉不属于当前关键词列表的主键（配置里关键词删了，同义词跟着失效）
        merged = {k: v for k, v in merged.items() if k in self.config.keywords}
        return merged

    # ------------------------------------------------------------------
    def filter(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """过滤岗位列表：先做岗位类型安全网，再按关键词+同义词筛选。

        「安全网」的放行集合由 config.allowed_job_type_classes() 决定，
        也就是由 career_stage 决定——在读学生剔除全职、已毕业剔除实习兼职、
        both 全放行。这样同一套代码能服务不同身份的求职者。

        命中关键词者在返回时带上 keyword_matched（放行证据）；被类型安全网
        剔除的岗位会单独计数并记日志。
        """
        kept: list[JobPosting] = []
        type_dropped = 0
        allowed = self.config.allowed_job_type_classes()
        for job in jobs:
            haystack = f"{job.title} {job.description_snippet}".lower()
            # 岗位类型安全网：URL 过滤是主防线，这里再兜一道。
            kind = self.classify_job_type(haystack)
            if kind not in allowed:
                type_dropped += 1
                logger.debug(f"岗位类型安全网剔除「{kind}」：{job.title}（{job.url}）")
                continue
            matched = self._match(haystack)
            if matched:
                job.keyword_matched = matched
                kept.append(job)
        keyword_dropped = len(jobs) - len(kept) - type_dropped
        logger.info(f"关键词初筛：{len(jobs)} → {len(kept)}"
                    f"（剔除 {type_dropped} 个不符合当前求职阶段"
                    f"（{self.config.career_stage}）的岗位，"
                    f"丢弃 {keyword_dropped} 个不相关岗位）")
        return kept

    # ------------------------------------------------------------------
    @staticmethod
    def classify_job_type(text: str) -> str:
        """对 title+snippet 的小写文本做岗位类型判定。

        返回 'internship' | 'part time' | 'graduate programme' | 'full time'
        | ''（空=未命中任何类型）。
        优先级：实习/兼职 > 毕业生项目 > 全职——
        "Full-time Intern" 仍是实习（合法）、"Permanent Part-time" 仍是兼职、
        "Management Trainee"（即使标注 full-time）仍是毕业后入职的毕业生项目，
        都不能被「全职」标记误杀。
        """
        text = (text or "").lower()
        if any(p.search(text) for p in _INTERNSHIP_MARKERS):
            return "internship"
        if any(p.search(text) for p in _PARTTIME_MARKERS):
            return "part time"
        if any(p.search(text) for p in _GRADUATE_PROGRAMME_MARKERS):
            return "graduate programme"
        if any(p.search(text) for p in _FULLTIME_MARKERS):
            return "full time"
        return ""

    def _contains(self, haystack: str, keyword: str) -> bool:
        """haystack 里有没有这个关键词（短词走词首边界，其余直接子串）。"""
        pattern = self._short_patterns.get(keyword)
        if pattern is not None:
            return bool(pattern.search(haystack))
        return keyword in haystack

    def _match(self, haystack: str) -> str:
        """返回命中的主关键词（主词优先于同义词返回）；未命中返回空串。

        同时匹配普通关键词（config.keywords）与毕业生项目关键词
        （config.graduate_keywords），后者是毕业后入职的全职岗，属合法投递范围。
        """
        for keyword in self.config.keywords:
            if self._contains(haystack, keyword):
                return keyword
        for keyword in self.config.graduate_keywords:
            if self._contains(haystack, keyword):
                return keyword
        for main_kw, variants in self.synonyms.items():
            for variant in variants:
                if variant and self._contains(haystack, variant):
                    return f"{main_kw}（同义词: {variant}）"
        return ""
