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

import html
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


# ---------------------------------------------------------------------------
# HTML 排版（邮件通道用）
#
# 为什么单独做一套：文本通道只能发纯文本，而邮件可以发 HTML。同一份岗位数据，
# 用 HTML 排成「卡片」，在手机上看起来就像一个小程序界面——这是邮件通道最大
# 的价值，也是「高级版更精致」这条产品线的第一块砖。
#
# 排版约定（邮件客户端的坑）：
# - 只用**内联 style**：QQ / 网易 / Gmail 都会剥掉 <style> 标签里的规则；
# - 不用 flex/grid：Outlook 与部分手机客户端不支持，用最朴素的块级布局；
# - 宽度用 max-width 而不是 width：窄屏手机上才会自适应。
# ---------------------------------------------------------------------------
def _esc(value: object) -> str:
    """HTML 转义：岗位标题里出现 & < > 时不会把邮件排版打乱。"""
    return html.escape(str(value if value is not None else ""), quote=True)


def build_html_card(job: JobPosting) -> str:
    """单个岗位的 HTML 卡片（邮件正文的一块）。"""
    if job.llm_score is None:
        badge_bg, badge_text = "#FAC775", "未打分"
    elif job.llm_score >= 80:
        badge_bg, badge_text = "#E24B4A", f"{job.llm_score} 分"
    else:
        badge_bg, badge_text = "#378ADD", f"{job.llm_score} 分"

    meta = " · ".join(x for x in (job.company, job.location, job.salary_text) if x)
    rows = [
        f'<div style="margin:0 0 6px 0;">'
        f'<span style="display:inline-block;background:{badge_bg};color:#ffffff;'
        f'font-size:12px;font-weight:600;padding:3px 10px;border-radius:10px;">{_esc(badge_text)}</span>'
        + (f' <span style="font-size:12px;color:#1D9E75;font-weight:600;">官网直投</span>'
           if job.direct_apply else "")
        + "</div>",
        f'<div style="font-size:16px;font-weight:600;color:#1f2328;line-height:1.4;'
        f'margin:0 0 4px 0;">{_esc(job.title)}</div>',
    ]
    if meta:
        rows.append(f'<div style="font-size:13px;color:#5f6368;margin:0 0 6px 0;">{_esc(meta)}</div>')
    if job.llm_reason:
        rows.append(
            f'<div style="font-size:13px;color:#1f2328;background:#F1EFE8;'
            f'border-radius:6px;padding:8px 10px;margin:0 0 6px 0;">'
            f'<b style="color:#5f6368;font-weight:600;">匹配理由</b><br>{_esc(job.llm_reason)}</div>')
    if job.keyword_matched:
        rows.append(f'<div style="font-size:12px;color:#888780;margin:0 0 8px 0;">'
                    f'命中关键词：{_esc(job.keyword_matched)}</div>')
    rows.append(
        f'<a href="{_esc(job.url)}" style="display:inline-block;background:#185FA5;color:#ffffff;'
        f'font-size:13px;font-weight:600;text-decoration:none;padding:8px 16px;'
        f'border-radius:6px;">查看岗位 / 投递</a>')

    return ('<div style="border:1px solid #e3e3e3;border-radius:10px;padding:14px 16px;'
            'margin:0 0 12px 0;background:#ffffff;">' + "".join(rows) + "</div>")


def build_html_report(jobs: list[JobPosting], show_unscored: bool = True
                      ) -> tuple[str, str]:
    """生成邮件用的 (主题, HTML 正文)。

    排序逻辑与文本日报完全一致（分数降序、未打分垫底），保证同一批岗位在
    不同通道上看到的顺序是一样的——用户认知负担最小。
    """
    today = f"{now_hk():%m-%d}"
    subject = f"香港岗位雷达 · 岗位日报（{today}）"
    if not jobs:
        body = ('<div style="font-family:-apple-system,\'Segoe UI\',sans-serif;'
                'padding:24px;color:#1f2328;">今天没有新的匹配岗位，休息一下。</div>')
        return subject, body

    matched = [j for j in jobs if j.llm_score is not None]
    unscored = [j for j in jobs if j.llm_score is None]
    direct = sum(1 for j in jobs if j.direct_apply)
    ordered = sorted(jobs, key=lambda j: (j.llm_score is None, -(j.llm_score or 0)))

    summary = (f"共 {len(jobs)} 个新岗位 · {len(matched)} 个已打分"
               + (f" · {len(unscored)} 个未打分（AI 降级）" if unscored and show_unscored else "")
               + f" · {direct} 个官网直投")

    header = (
        '<div style="background:#0C447C;border-radius:10px;padding:18px 20px;margin:0 0 16px 0;">'
        '<div style="font-size:17px;font-weight:600;color:#ffffff;">香港岗位雷达 · 日报</div>'
        f'<div style="font-size:13px;color:#B5D4F4;margin-top:6px;">{_esc(summary)}</div>'
        "</div>")

    cards = "".join(build_html_card(j) for j in ordered)
    html_body = (
        '<div style="font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
        'background:#f5f5f5;padding:16px;">'
        f'<div style="max-width:640px;margin:0 auto;">{header}{cards}'
        '<div style="font-size:12px;color:#888780;text-align:center;padding:8px 0 16px 0;">'
        '本邮件由香港岗位雷达自动发送 · 岗位信息以招聘网站页面为准</div>'
        "</div></div>")
    return subject, html_body
