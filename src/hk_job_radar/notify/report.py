"""日报与岗位卡片的文本生成（P0-6）。

为什么文本生成与发送分离（report.py vs telegram_client.py）：
排版是"内容问题"，发送是"通道问题"。分离后，同一份排版逻辑未来可以直接
复用到邮件通道（T04 的 emailer），而 Telegram 限流、分页、重试等通道细节
也不会污染排版代码。

分页约定（T03 要点）：Telegram 单消息上限 4096 字符，日报必须在
"岗位卡片边界"切分——硬切 4096 会把一张卡片拦腰截断，阅读体验崩坏。
这里预留余量取 3500 字符/页。
"""
from __future__ import annotations

from datetime import datetime

from hk_job_radar.models import JobPosting, now_hk

# 单页字符预算：4096 上限减去按钮 JSON、换行余量，实际按卡片边界切
_PAGE_CHAR_LIMIT = 3500
_CARD_SEP = "\n" + "━" * 18 + "\n"


def _score_badge(job: JobPosting) -> str:
    """岗位卡片的分数徽标：未打分的降级岗位用 ⚠️ 明确标出（U6 开关的展示侧）。"""
    if job.llm_score is None:
        return "⚠️ 未打分（LLM 降级）"
    star = "⭐" if job.llm_score >= 80 else "✨"
    return f"{star} {job.llm_score} 分"


def build_job_card(job: JobPosting, index: int | None = None) -> str:
    """单张岗位卡片（日报与实时推送共用同一种排版，用户认知负担最小）。

    index：日报内的序号，用于按钮与卡片的对应（实时推送不传）。
    """
    lines = [
        f"{_score_badge(job)}{f'  #{index}' if index else ''}"
        + ("  · 官网直投 🏠" if job.direct_apply else ""),
        job.title,
        " · ".join(x for x in (job.company, job.location, job.salary_text) if x),
        f"理由：{job.llm_reason}" if job.llm_reason else "",
        f"关键词：{job.keyword_matched}" if job.keyword_matched else "",
        f"🔗 {job.url}",
    ]
    return "\n".join(x for x in lines if x)


def build_daily_report(jobs: list[JobPosting], show_unscored: bool = True
                       ) -> tuple[list[str], list[list[list[dict]]]]:
    """生成日报。

    返回 (pages, keyboards) 两个平行列表：
    - pages[i]：第 i 页的文本（按卡片边界切分，单页 ≤ ~3500 字符）；
    - keyboards[i]：第 i 页对应的 inline keyboard（每岗位一行 ✅/❌ 按钮）。

    排序：分数降序、未打分垫底（若 show_unscored=False，调用方已过滤）。
    """
    if not jobs:
        return ([f"📡 香港岗位日报 · {now_hk():%m-%d}\n\n今天没有新的匹配岗位，休息一下 🍵"], [])

    matched = [j for j in jobs if j.llm_score is not None]
    unscored = [j for j in jobs if j.llm_score is None]
    direct = sum(1 for j in jobs if j.direct_apply)

    header = (
        f"📡 香港岗位日报 · {now_hk():%m-%d}\n"
        f"共 {len(jobs)} 个新岗位，{len(matched)} 个已打分"
        + (f"，{len(unscored)} 个未打分（LLM 降级）" if unscored and show_unscored else "")
        + f"，{direct} 个官网直投\n"
    )

    # 全局序号跨页连续，用户回看时报"第 7 个"没有歧义
    ordered = sorted(jobs, key=lambda j: (j.llm_score is None, -(j.llm_score or 0)))
    pages: list[str] = []
    keyboards: list[list[list[dict]]] = []
    current = header
    current_kb: list[list[dict]] = []

    for i, job in enumerate(ordered, start=1):
        card = build_job_card(job, index=i) + _CARD_SEP
        if len(current) + len(card) > _PAGE_CHAR_LIMIT and current is not header:
            pages.append(current.rstrip())
            keyboards.append(current_kb)
            current = "📡 日报（续）\n" + _CARD_SEP
            current_kb = []
        current += card
        current_kb.append(build_feedback_row(job, index=i))

    pages.append(current.rstrip())
    keyboards.append(current_kb)
    return pages, keyboards


def build_realtime_card(job: JobPosting) -> str:
    """≥80 分岗位的实时推送卡片（P1-1 的简化前置：P0 即支持高分即推）。"""
    return (
        f"🚨 高分新岗位（≥80 分，实时推送）\n\n"
        + build_job_card(job)
    )


def build_feedback_row(job: JobPosting, index: int | None = None) -> list[dict]:
    """单岗位的 ✅/❌ 按钮行（Telegram inline keyboard 的一行）。

    callback_data 格式 "fb:<job_uid>:<action>"（架构 4.6）：
    按钮点击后 Telegram 原样回传该字符串，poll_feedback 据此定位岗位与动作。
    """
    tag = f" #{index}" if index else ""
    return [
        {"text": f"✅ 感兴趣{tag}", "callback_data": f"fb:{job.job_uid}:interested"},
        {"text": f"❌ 不感兴趣{tag}", "callback_data": f"fb:{job.job_uid}:not_interested"},
    ]


def build_feedback_keyboard(job: JobPosting) -> list[list[dict]]:
    """单岗位消息的完整 keyboard（实时推送用，只有一行）。"""
    return [build_feedback_row(job)]
