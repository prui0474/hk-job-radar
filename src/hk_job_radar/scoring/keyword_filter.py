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

# ---------------------------------------------------------------------------
# 销售陷阱（2026-10-25 新增）：香港招聘站上一大批「理财顾问 / 财富管理培训生」
# 岗位，标题写着 Trainee / Graduate，实际是保险或理财销售。它们是真全职，
# 只是借用了「培训生」的说法绕开求职者的实习过滤。
#
# ★ 为什么不能靠 "trainee" 判定为实习：实测本地库里按「含 trainee」放行的岗位，
#   放行的一半就是下面这些——把 trainee 当实习信号等于直接放进销售岗。
# ★ 为什么不用裸 "insurance"：保险公司确实也招数据分析师，那是正常岗位。
#   只认「销售性质」的搭配（agent / consultant / sales）与中文的那几个词。
# ---------------------------------------------------------------------------
_SALES_TRAP_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"wealth management|private wealth|wealth mgmt"),
    re.compile(r"financial planning|financial consultan|financial advisor|financial adviser"),
    re.compile(r"insurance (?:agent|consultant|advisor|adviser|sales|specialist)"),
    re.compile(r"財務規劃|财务规划|財務策劃|财务策划|理財顧問|理财顾问|財富管理|财富管理|保險顧問|保险顾问"),
]

# ---------------------------------------------------------------------------
# 职级信号（2026-10-25 新增）：只作用于**标题**，不作用于正文。
# 「Senior / Manager / Director」这些词出现在标题里，基本可以断定不是实习岗；
# 但同样的词出现在正文里（"report to the Senior Manager"）毫无判定价值，
# 所以必须限定在标题上做匹配。
# 放在实习/兼职信号之后判断：「Project Manager Intern」仍是实习。
# ---------------------------------------------------------------------------
_SENIORITY_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:senior|sr)\b"),
    re.compile(r"\b(?:lead|principal|head|director|vp|vice president)\b"),
    re.compile(r"\b(?:manager|supervisor|officer)\b"),
    re.compile(r"高級|高级|資深|资深|總監|总监|經理|经理"),
]


# ---------------------------------------------------------------------------
# 标题方向词直通（2026-10-25 新增）
#
# 动机：关键词表要求「标题里出现某个完整岗位名」，但真实标题的写法比词表自由得多。
# 实测一批 205 个真实实习/兼职岗位，只有 8 个（3.9%）能过初筛，被丢掉的有
# `AI R&D Engineering Intern`（不含 "ai engineer"）、`Investment Risk Management
# Intern`（不含任何词）、`intern - FICC/Research` —— 都是对口岗。
#
# 做法：只要**标题**里出现方向词（data / ai / research / risk …），就放行进 LLM
# 打分，让 LLM 用「求职者画像」判断相关性，而不是让字符串匹配替它做决定。
#
# ★ 为什么必须加词首边界：裸子串会让 "ai" 命中 email / retail / Air
#   Conditioning、"data" 命中 metadata，等于把噪音全放进来。
# ★ 为什么 ≤2 字母还要加词尾边界：只加词首仍会命中 "air" / "aim" / "aid"。
#   三个字母以上刻意不加，因为要能命中 "LLMs"、"LLM-powered"、"Quantitative"。
# ★ 边界用「前后不能是 [0-9a-z_]」而不是 \b：\b 把汉字也算单词字符，
#   会导致 "AI原生產品經理" 这种中文标题里的 "AI" 判定不上（见 _compile_direction_hints）。
# ★ 中文直接子串匹配（中文没有词边界概念）。
# ---------------------------------------------------------------------------
def _compile_direction_hints(hints: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """把方向词编译成 (原文, 正则) 列表，用于匹配岗位标题。

    边界怎么写（2026-11-02 改）：英文方向词用**ASCII 前后查视**而不是 `\\b`。
    原因：Python 的 `\\b` 把汉字也算「单词字符」，所以 "AI原生產品經理" 里
    "AI" 后面紧挨汉字时**不算词尾**，`\\bai\\b` 匹配不到——而这正是中文标题里
    最常见的写法（"AI工程師"、"BI分析員"）。改成「前后不能是 [0-9a-z_]」后：

        AI原生 / AI工程師   → 命中（旧版漏）
        email / air / mail  → 不命中（与旧版一致）
        meta_data           → 不命中（下划线仍算词内，与旧版一致）

    即：纯英文标题上的结果与旧版逐条相同（有测试钉住），只在「ASCII 词紧邻
    汉字」这一种中文标题形态上放开——而那正是这次要修的场景。
    """
    out: list[tuple[str, re.Pattern[str]]] = []
    for hint in hints or []:
        h = (hint or "").strip().lower()
        if not h:
            continue
        if not h.isascii():
            # 必须带 re.I：配置加载时 title_direction_hints 会被统一 .lower()，
            # 而用户能填「AI數據」「BI分析員」这种 ASCII+汉字 的混合写法。
            # 早先这里没写 re.I，混合词就永远匹配不到首字母大写的真实标题
            # （"AI數據" 配 "AI數據分析實習生" 不命中、配 "ai數據分析" 才命中），
            # 与同一份配置里 keywords 走 haystack.lower() 的口径不一致，
            # 也与文档宣称的「大小写不敏感」矛盾。纯中文加 re.I 无副作用。
            pattern = re.compile(re.escape(h), re.I)
        elif len(h) <= 2:
            pattern = re.compile(r"(?<![0-9a-z_])" + re.escape(h) + r"(?![0-9a-z_])", re.I)
        else:
            pattern = re.compile(r"(?<![0-9a-z_])" + re.escape(h), re.I)
        out.append((h, pattern))
    return out


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
        #: 「标题方向词直通」的编译结果（见 config.title_direction_hints）。
        #: 空列表 = 关闭该功能，行为与之前完全一致。
        self._direction_hints = _compile_direction_hints(
            getattr(config, "title_direction_hints", []) or []
        )

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
    @classmethod
    def classify_posting(cls, job: JobPosting) -> str:
        """判定一条岗位的类型（比 classify_job_type 多两层信息）。

        返回 'internship' | 'part time' | 'contract' | 'temporary'
        | 'graduate programme' | 'full time' | ''（空 = 不可确认）。

        与 classify_job_type(text) 的区别，也是它存在的理由：
          1. 先看**站点自己标的类型**（job.job_type）——抓取时从卡片上读下来的，
             这是最可信的证据，站点说 Part time 就是 Part time；
          2. 把标题和正文分开判断——"Senior" 出现在标题里可以断定不是实习，
             出现在正文里（"report to the Senior Manager"）则毫无意义。

        ★ 站点标签会原样返回，所以返回值可能是 extract_site_job_type 归一化后的
        'contract' / 'temporary'（JobsDB 卡片写 "Contract/Temp"）。这时类型判定
        完全交给站点，不再用标题猜——合同的结束日期只有站点知道。
        """
        site = (getattr(job, "job_type", "") or "").strip().lower()
        title = (job.title or "").lower()
        body = (job.description_snippet or "").lower()

        if site:
            # 站点标签优先——「实习」是唯一能压过它的一层。
            # 原因：JobsDB 会把「全职时长的实习」也标成 Full time（卡片上就印
            # "This is a Full time job"）。若站点标签无条件优先，修好标签提取后
            # 反而会把 Data Analyst Intern 这类岗位误杀成全职。
            # 职级词（Senior / Manager）不享受这个豁免——见下面的注释。
            if site == "full time" and any(p.search(title) for p in _INTERNSHIP_MARKERS):
                return "internship"
            return site                      # 站点标签优先，已归一化

        # 1) 标题里的实习/兼职信号最硬：标题写了 Intern 就是实习，不用再看别的
        if any(p.search(title) for p in _INTERNSHIP_MARKERS):
            return "internship"
        if any(p.search(title) for p in _PARTTIME_MARKERS):
            return "part time"
        # 2) 正文里的实习/兼职信号次之
        if any(p.search(body) for p in _INTERNSHIP_MARKERS):
            return "internship"
        if any(p.search(body) for p in _PARTTIME_MARKERS):
            return "part time"
        # 3) 销售陷阱：理财顾问 / 财富管理培训生，实为全职销售
        if any(p.search(title) for p in _SALES_TRAP_MARKERS):
            return "full time"

        haystack = f"{title} {body}"
        # 4) 毕业生项目（管培生等）
        if any(p.search(haystack) for p in _GRADUATE_PROGRAMME_MARKERS):
            return "graduate programme"
        # 5) 明确的全职标记
        if any(p.search(haystack) for p in _FULLTIME_MARKERS):
            return "full time"
        # 6) 标题里的职级词（限标题，正文里出现毫无判定价值）
        if any(p.search(title) for p in _SENIORITY_MARKERS):
            return "full time"
        return ""

    # ------------------------------------------------------------------
    def filter(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """过滤岗位列表：先做岗位类型安全网，再按关键词+同义词筛选。

        「安全网」的放行集合由 config.allowed_job_type_classes() 决定，
        也就是由 career_stage 决定——在读学生剔除全职、已毕业剔除实习兼职、
        both 全放行。这样同一套代码能服务不同身份的求职者。

        在此基础上还有两层可选收紧（都由配置控制，默认全关，不影响开源用户）：
          - exclude_job_type_classes：额外排除某些分类（如不要管培生）；
          - job_type_policy=strict + unknown_job_type_action=drop：
            判定不出类型时直接丢弃。

        判定结果会写回 job.job_type，供入库、实时推送与日报使用。
        """
        kept: list[JobPosting] = []
        type_dropped = 0
        excluded_dropped = 0
        unknown_dropped = 0
        direction_passed = 0
        allowed = self.config.allowed_job_type_classes()
        excluded = set(self.config.exclude_job_type_classes)
        drop_unknown = (self.config.job_type_policy == "strict"
                        and self.config.unknown_job_type_action == "drop")
        for job in jobs:
            kind = self.classify_posting(job)
            job.job_type = kind             # 记下来：入库/推送/日报都要用
            # 岗位类型安全网：URL 过滤是主防线，这里再兜一道。
            if kind in excluded:
                excluded_dropped += 1
                logger.debug(f"岗位类型排除「{kind}」：{job.title}（{job.url}）")
                continue
            if kind not in allowed:
                type_dropped += 1
                logger.debug(f"岗位类型安全网剔除「{kind}」：{job.title}（{job.url}）")
                continue
            if drop_unknown and not kind:
                unknown_dropped += 1
                logger.debug(f"类型不可确认（strict/drop）剔除：{job.title}（{job.url}）")
                continue
            haystack = f"{job.title} {job.description_snippet}".lower()
            matched = self._match(haystack)
            if matched:
                job.keyword_matched = matched
                kept.append(job)
                continue
            # 方向词直通：标题里出现方向词就放行，交给 LLM 判断相关性。
            # ★ 只认标题。摘要里 "data-driven" / "AI-powered" 遍地都是，
            #   放开摘要等于取消初筛。
            hint = self._match_direction(job.title)
            if hint:
                job.keyword_matched = f"方向词直通: {hint}"
                direction_passed += 1
                kept.append(job)
        keyword_dropped = (len(jobs) - len(kept) - type_dropped
                           - excluded_dropped - unknown_dropped)
        extra = ""
        if excluded_dropped:
            extra += f"，按配置排除 {excluded_dropped} 个"
        if unknown_dropped:
            extra += f"，类型不可确认丢弃 {unknown_dropped} 个"
        if direction_passed:
            extra += f"，方向词直通放行 {direction_passed} 个"
        logger.info(f"关键词初筛：{len(jobs)} → {len(kept)}"
                    f"（剔除 {type_dropped} 个不符合当前求职阶段"
                    f"（{self.config.career_stage}）的岗位{extra}，"
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

    def _match_direction(self, title: str) -> str:
        """标题里命中的第一个方向词；没配或没命中返回空串。

        只接受**标题**——调用方传入的必须是 job.title，不要传 title+snippet。
        """
        if not self._direction_hints:
            return ""
        text = title or ""
        for hint, pattern in self._direction_hints:
            if pattern.search(text):
                return hint
        return ""
