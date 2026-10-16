"""推送通道的统一接口（base）。

为什么要抽象出这一层：
推送以前只有 Telegram 一种、写死在流水线里。一旦要加企业微信 / 邮件 / WxPusher，
如果每个都直接往 pipeline 里塞 if-else，主流程很快会变成一锅粥；而且以后腾讯
改政策、第三方跑路，都要动主流程——风险极高。

抽象成接口之后：**每个通道一个文件，实现同一组方法**；流水线只认接口，
不认具体通道。要新增/替换通道，只加一个文件 + 在注册表里登记一行。

关键概念（写给不熟悉的人）：
「接口」在这里就是一个「约定俗成的样板」——它规定了每个通道必须提供哪几个
方法（能发消息、能发日报、能不能点按钮）。只要照着这个样板写，流水线就能
把它当成 Telegram 一样使用，不用知道它背后到底是发邮件还是发微信。
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class NotifyChannel:
    """推送通道基类：所有通道实现同一组方法。

    子类至少要覆盖 send_text()。其余方法有默认实现，按需覆盖即可。
    """

    #: 机器名（与 config.yaml 里 notify.channels 的取值一致）
    name: str = "base"
    #: 给用户看的中文名
    label: str = "基础通道"
    #: 渲染方式："text" = 收纯文本分页；"html" = 收整封 HTML（邮件用）
    render_mode: str = "text"
    #: 是否支持可点击的 ✅/❌ 按钮（目前只有 Telegram 支持）
    supports_keyboard: bool = False
    #: 是否支持读取用户点击（反馈闭环；目前只有 Telegram 支持）
    supports_feedback: bool = False
    #: 单条消息的安全长度（字符数，子类按各家限制覆盖）
    max_chars: int = 3500

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        """发一条消息。返回 True = 至少发出去了。

        keyboard 是 Telegram 专用的「按钮」结构；不支持按钮的通道直接忽略它，
        不要报错（用户换通道不该因此收不到消息）。
        """
        raise NotImplementedError

    def send_pages(self, pages: list[str],
                   keyboards: Optional[list[list[list[dict]]]] = None) -> bool:
        """逐页发送（日报用）。默认实现：挨个调 send_text，页间小睡防限流。"""
        ok = True
        for i, page in enumerate(pages):
            kb = keyboards[i] if keyboards and i < len(keyboards) else None
            try:
                self.send_text(page, kb)
            except Exception:       # 单页失败不影响其余页
                ok = False
            if i < len(pages) - 1:
                time.sleep(1)
        return ok

    def send_html(self, subject: str, html: str) -> bool:
        """发送 HTML 内容（仅 render_mode == "html" 的通道需要实现，如邮件）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 反馈闭环（可选能力）
    # ------------------------------------------------------------------
    def poll_feedback(self, on_callback: Callable[[str, str], None]) -> None:
        """拉取用户点击回调。默认什么都不做（大多数通道没有交互能力）。"""
        return None

    # ------------------------------------------------------------------
    # 自检（供 `诊断` 命令使用）
    # ------------------------------------------------------------------
    def check(self) -> tuple[bool, str]:
        """返回 (是否可用, 中文说明)。默认：配置齐全即视为可用。"""
        return True, "已配置"


class ChannelConfigError(Exception):
    """通道配置缺失：携带中文提示，由 require_notify 汇总后给用户看。"""


# ---------------------------------------------------------------------------
# 分页小工具（各通道的上限不同，但切分策略一致：优先在换行处切）
#
# ⚠️ 必须同时处理「一整行就超限」的情况：岗位的「匹配理由」可能是一大段没有
#    换行的中文，如果只按行切、遇到超长单行不处理，那这一页就会超出通道上限被
#    直接拒收——用户什么都收不到，而且日志里只有一句"发送失败"，极难排查。
# ---------------------------------------------------------------------------
def split_text(text: str, limit_chars: int) -> list[str]:
    """按【字符数】切分，优先在换行处断句，保证每段 ≤ limit_chars。

    为什么优先在换行处切：日报是按「岗位卡片」排版的，一句话被拦腰截断
    （比如公司和薪资分到两页）阅读体验很差。行边界切分能保住排版。
    """
    if limit_chars <= 0 or len(text) <= limit_chars:
        return [text]
    pages: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit_chars:
            current = candidate
            continue
        if current:
            pages.append(current)
            current = ""
        # 单行本身超出上限 → 只能按字符硬切（保证一定不超限）
        while len(line) > limit_chars:
            pages.append(line[:limit_chars])
            line = line[limit_chars:]
        current = line
    if current:
        pages.append(current)
    return pages


def _cut_by_bytes(text: str, limit_bytes: int) -> tuple[str, str]:
    """把字符串切成「不超过 limit_bytes 的最长前缀」+ 剩余部分。

    用二分查找定位切点，保证**不会把一个多字节字符切成两半**
    （切坏了会变成乱码，企业微信那边也是直接拒收）。
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= limit_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], text[lo:]


def split_text_bytes(text: str, limit_bytes: int) -> list[str]:
    """按【UTF-8 字节数】切分（企业微信的限制是按字节算的，中文 1 个字 = 3 字节）。

    这是最容易踩的坑：按「字数」算觉得没超，按「字节」算早就超了，
    结果企业微信直接拒收、用户什么都收不到。
    """
    if limit_bytes <= 0 or len(text.encode("utf-8")) <= limit_bytes:
        return [text]
    pages: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) <= limit_bytes:
            current = candidate
            continue
        if current:
            pages.append(current)
            current = ""
        while len(line.encode("utf-8")) > limit_bytes:
            head, line = _cut_by_bytes(line, limit_bytes)
            if not head:            # 防御：单个字符就超限的极端情况，直接整段输出
                break
            pages.append(head)
        current = line
    if current:
        pages.append(current)
    return pages
