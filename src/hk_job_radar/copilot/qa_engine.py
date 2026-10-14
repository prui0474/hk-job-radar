"""开放式筛选问题的识别与作答（T05）。

求职表单里最难自动化的不是"姓名/邮箱"这类标准字段，而是五花八门的
开放式问题，例如：
  - "Why are you a good fit for this role?"
  - "Do you require visa sponsorship?"
  - "What is your expected salary?"

作答策略分三层（越往下成本越高、越需要人工把关）：
1. 规则层：命中 profile.yaml 的 screening_rules 口径 → 直接用（最可靠）；
2. LLM 层：规则没命中 → 调 DeepSeek 结合画像生成英文草稿；
3. 人工层：LLM 不可用或生成失败 → 留空并在确认关卡标为 🔶 待你手填。

无论哪一层产出的答案，都必须经过 confirm.py 的人工确认才能进入页面
（合规要求：AI 生成内容不得静默提交）。
"""
from __future__ import annotations

import re
from typing import Optional

from loguru import logger

# 问题关键词 → screening_rules 的键（规则层匹配表）
_RULE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"visa|sponsorship|work\s+author|right\s+to\s+work|eligible\s+to\s+work|"
                r"簽證|签证|工作簽", re.I), "visa_sponsorship"),
    (re.compile(r"expected\s+salary|salary\s+expectation|desired\s+salary|current\s+salary|"
                r"薪資|薪资|期望薪", re.I), "expected_salary"),
    (re.compile(r"when\s+can\s+you\s+start|availability|start\s+date|available\s+from|"
                r"到崗|到岗|可上班", re.I), "availability"),
    (re.compile(r"notice\s+period", re.I), "notice_period"),
    (re.compile(r"location|relocat|on-?site|hybrid|remote|工作地點|工作地点", re.I), "work_location"),
    (re.compile(r"language|mandarin|cantonese|english\s+proficien|語言|语言", re.I), "language_skills"),
    (re.compile(r"why.*(fit|interested|apply)|why\s+should\s+we\s+hire|cover\s+letter|"
                r"tell\s+us\s+about|自我介紹|自我介绍", re.I), "why_fit"),
]


class QAEngine:
    """筛选问题作答引擎：规则优先，LLM 兜底，人工终审。"""

    def __init__(self, llm_config, profile, enabled: bool = True) -> None:
        self.llm_config = llm_config
        self.profile = profile
        self.enabled = enabled   # 关掉则所有问题都留空给人工
        self._client = None

    # ------------------------------------------------------------------
    def answer(self, question: str) -> tuple[str, str]:
        """对一个问题给出答案，返回 (答案, 来源)。

        来源取值：
          "rule" —— 命中 profile.yaml 的答题口径（可直接采信）
          "ai"   —— LLM 生成的草稿（必须人工审阅）
          ""     —— 没答出来（确认关卡会标为待填）
        """
        question = (question or "").strip()
        if not question:
            return "", ""

        # 第一层：规则
        for pattern, key in _RULE_PATTERNS:
            if pattern.search(question):
                text = self.profile.rule(key)
                if text:
                    logger.debug(f"筛选问题命中规则：{key}")
                    return text, "rule"

        # 第二层：LLM
        if self.enabled:
            text = self._ask_llm(question)
            if text:
                return text, "ai"

        return "", ""

    # ------------------------------------------------------------------
    def _ask_llm(self, question: str) -> Optional[str]:
        """调 DeepSeek 生成英文答案草稿；任何异常都吞掉并留空（不阻断填表）。"""
        api_key = self.llm_config.resolve_api_key()
        if not api_key:
            return None
        try:
            if self._client is None:
                from openai import OpenAI
                self._client = OpenAI(api_key=api_key,
                                      base_url=self.llm_config.base_url,
                                      timeout=self.llm_config.timeout_seconds)
            prompt = (
                "You are helping a job candidate answer a job application screening question.\n"
                "Answer concisely in English, first person, 2-4 sentences, factual and specific "
                "to the candidate profile. Do not invent facts that are not in the profile.\n\n"
                f"QUESTION:\n{question}\n\n"
                "CANDIDATE PROFILE (from the candidate's own profile file):\n"
                f"{self._profile_brief()}\n\n"
                "Answer:"
            )
            resp = self._client.chat.completions.create(
                model=self.llm_config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text or None
        except Exception as exc:
            logger.warning(f"LLM 生成筛选问题答案失败（将留空由你手填）：{exc}")
            return None

    def _profile_brief(self) -> str:
        """给 LLM 的精简画像（避免把整个 profile 塞进去浪费 token）。"""
        lines = []
        if self.profile.name:
            lines.append(f"Name: {self.profile.name}")
        for edu in self.profile.education[:2]:
            lines.append(f"Education: {edu.get('degree','')} at {edu.get('school','')}"
                         f" ({edu.get('period','')})")
        for work in self.profile.work_experience[:2]:
            lines.append(f"Experience: {work.get('title','')} at {work.get('company','')}"
                         f" ({work.get('period','')})")
        for key in ("visa_sponsorship", "expected_salary", "availability",
                    "work_location", "language_skills"):
            if self.profile.rule(key):
                lines.append(f"{key}: {self.profile.rule(key)}")
        prefs = self.profile.job_preferences or {}
        if prefs.get("target_roles"):
            lines.append(f"Target roles: {', '.join(map(str, prefs['target_roles']))}")
        return "\n".join(lines) or "(no profile provided)"
