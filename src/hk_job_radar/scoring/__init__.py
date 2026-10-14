"""处理层（scoring/）：关键词初筛 + LLM 相关性打分。

包结构与职责：
- keyword_filter.py：廉价的文本匹配初筛（P0-3），把明显无关的岗位挡在
  LLM 之外——省钱、省时间；
- llm_scorer.py：DeepSeek 打分（P0-4），失败降级不阻断（C4 挑战）。
"""
from hk_job_radar.scoring.keyword_filter import KeywordFilter
from hk_job_radar.scoring.llm_scorer import LLMScorer

__all__ = ["KeywordFilter", "LLMScorer"]
