"""LLM 相关性打分（P0-4）：DeepSeek 经 openai SDK 调用。

关键设计：
1. DeepSeek 完全兼容 OpenAI API，只需 base_url 指到 https://api.deepseek.com、
   model 用 deepseek-chat——学一套 SDK 到处能用；
2. 降级策略（C4 挑战）：任何失败（超时/JSON 解析失败/断网）都返回 None，
   调用方把岗位标记"关键词匹配，未打分"照常进日报——推送链路永不为
   LLM 故障买单；
3. 串行逐个打分，不加并发：单用户场景一天几十次调用，并发只会增加
   限流风险和排查难度，省不了几分钟；
4. prompt = 用户背景画像（profile.yaml 的 background 段）+ 岗位三要素，
   背景改动即热更新打分口径（P1-3），无需改代码。
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from loguru import logger

from hk_job_radar.config import LLMConfig
from hk_job_radar.models import JobPosting, ScoringResult

# 兜底画像：profile.yaml 缺失/无 background 段时使用（U4 决策的默认占位）
# 注意（合规）：学生签证在读期间只能实习/兼职，全职违法；但「毕业后入职」
# 的管培生/毕业生项目（management trainee / graduate programme）合法，
# 因为入职时已毕业并持有 IANG。因此这里把两类分开写。
_FALLBACK_PROFILE = (
    "香港城市大学数据科学硕士在读学生（约一年后毕业）。当前求职阶段主要找香港的"
    "实习（internship）与兼职（part-time）岗位——学生签证在读期间不可全职工作；"
    "同时投递「毕业后入职」的管理培训生/毕业生项目（management trainee / "
    "graduate programme），这类岗位毕业转 IANG 后合法入职。"
    "求职方向为数据科学/数据分析/数据工程类。熟悉 Python、SQL、机器学习基础。"
)

# 系统提示词：强调输出纪律（只要 JSON，不要解释），这是稳定解析的关键
_SYSTEM_PROMPT = (
    "你是一个严谨的求职岗位匹配评估助手。根据求职者的背景画像和岗位信息，"
    "评估该岗位与求职者的匹配程度。"
    "打分范围 0-100：60 分以上代表值得认真考虑，80 分以上代表高度匹配。"
    "评分依据包括：技能匹配度、经验要求与应届生身份的契合度、行业方向、"
    "地点（求职者在香港求职）。"
    "你必须只输出一个 JSON 对象，不要输出任何其他文字："
    '{"score": <0-100 的整数>, "reason": "<一句话中文理由，不超过 50 字>"}'
)


class LLMScorer:
    """DeepSeek 打分客户端。"""

    def __init__(self, config: LLMConfig, user_profile_prompt: str) -> None:
        self.config = config
        self.user_profile_prompt = (user_profile_prompt or "").strip() or _FALLBACK_PROFILE
        # 延迟创建 OpenAI 客户端：key 可能未配置，等第一次真正打分时再报降级
        self._client = None

    # ------------------------------------------------------------------
    def _get_client(self):
        """懒加载 OpenAI 客户端；key 缺失时返回 None（走降级路径）。"""
        if self._client is not None:
            return self._client
        api_key = self.config.resolve_api_key()
        if not api_key:
            logger.warning(
                f"环境变量 {self.config.api_key_env} 未设置，本轮所有岗位将降级为「未打分」。")
            return None
        try:
            from openai import OpenAI
        except ImportError as exc:
            logger.error(f"openai SDK 未安装：{exc}，岗位将降级为「未打分」。")
            return None
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.config.base_url,   # DeepSeek 兼容层的关键一行
            timeout=self.config.timeout_seconds,
        )
        return self._client

    # ------------------------------------------------------------------
    def build_prompt(self, job: JobPosting) -> str:
        """组装单岗位打分的 user prompt（控制在 ~1500 tokens 内）。"""
        return (
            f"【求职者背景画像】\n{self.user_profile_prompt}\n\n"
            f"【岗位信息】\n"
            f"职位名称：{job.title}\n"
            f"公司：{job.company}\n"
            f"地点：{job.location or '未标注'}\n"
            f"薪资：{job.salary_text or '未标注'}\n"
            f"岗位摘要：{job.description_snippet or '（无摘要，仅凭职位名称判断）'}\n\n"
            "请输出匹配分 JSON。"
        )

    # ------------------------------------------------------------------
    def score(self, job: JobPosting) -> Optional[ScoringResult]:
        """单岗位打分。返回 None 的全部情况 → 调用方降级（llm_score 置 None）。"""
        client = self._get_client()
        if client is None:
            return None
        try:
            resp = client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": self.build_prompt(job)},
                ],
                temperature=0,        # 打分要稳定可复现，不要发散
                max_tokens=120,       # 只要一小段 JSON，上限小一点省钱
            )
            return self._parse_response(resp.choices[0].message.content or "")
        except Exception as exc:
            # 统一降级入口：超时/网络/限流/解析，都不阻断流水线
            logger.warning(f"LLM 打分失败（降级为未打分）{job.job_uid}：{exc}")
            return None

    @staticmethod
    def _parse_response(text: str) -> Optional[ScoringResult]:
        """解析模型输出 → ScoringResult；解析失败视为降级（返回 None）。"""
        text = text.strip()
        # 模型偶尔会在 JSON 前后加话（"好的，以下是结果："），先剥出 {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            logger.warning(f"LLM 输出中找不到 JSON：{text[:200]!r}")
            return None
        try:
            data = json.loads(m.group(0))
            score = int(data["score"])
            reason = str(data.get("reason", "")).strip()
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"LLM 输出 JSON 解析失败（降级）：{exc}；原文：{text[:200]!r}")
            return None
        # 分数越界钳制到合法区间（模型偶尔输出 100+ 或负数）
        score = max(0, min(100, score))
        return ScoringResult(score=score, reason=reason or "（模型未给出理由）")

    # ------------------------------------------------------------------
    def score_batch(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """串行逐个打分，返回同一批 JobPosting（失败者 llm_score 保持 None）。

        为什么原地修改而不是返回新列表：岗位对象还要带着打分结果走完
        入库→推送的链路，原地修改让调用方不必做对象合并这种易错操作。
        """
        scored = degraded = 0
        for job in jobs:
            result = self.score(job)
            if result is None:
                job.llm_score = None
                job.llm_reason = "关键词匹配，未打分"   # 日报中标注的降级文案
                degraded += 1
            else:
                job.llm_score = result.score
                job.llm_reason = result.reason
                scored += 1
            time.sleep(0.5)   # 轻微限速：对 API 侧礼貌一点，也便于日志观察节奏
        logger.info(f"LLM 打分完成：{scored} 个成功，{degraded} 个降级未打分")
        return jobs
