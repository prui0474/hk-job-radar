"""Telegram Bot API 直调客户端（P0-6 / U2 决策：P0 即带反馈按钮）。

为什么用 requests 直调而不是 python-telegram-bot 框架（架构第 2 节）：
单用户场景只需要 3 个 HTTP 接口（sendMessage / answerCallbackQuery /
getUpdates），约 150 行代码完全透明可控；引入 async 框架反而把简单事情
复杂化，也不利于学习者排查问题。

实现要点：
- send_message 失败重试 2 次（共 3 次尝试）后放弃本轮，下轮日报兜底
  （架构第 8 节错误处理约定）；
- poll_feedback 用 getUpdates 轮询，offset 持久化到 SQLite kv_store，
  进程重启不会重复处理已消费的更新；
- 日报分页由 report.py 按"卡片边界"完成，本类只负责逐页发送 +
  页间 sleep 1s 防限流。
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

import requests
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_fixed

from hk_job_radar.db import Database
from hk_job_radar.notify.base import NotifyChannel

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
# Telegram 单消息硬上限 4096 字符（含按钮 JSON），留少量余量
_MSG_CHAR_LIMIT = 4000
# offset 的 kv 键名（跨进程持久化在 SQLite）
_OFFSET_KEY = "tg_update_offset"


class TelegramClient(NotifyChannel):
    """Bot API 薄封装：发送、分页发送、反馈轮询。

    它是四个推送通道里**唯一支持交互按钮**的（✅/❌ 反馈闭环），
    所以 supports_keyboard / supports_feedback 为 True——调度器据此决定
    要不要启动反馈轮询任务（其它通道启动也没用，只会白跑）。
    """

    name = "telegram"
    label = "Telegram"
    render_mode = "text"
    supports_keyboard = True
    supports_feedback = True
    max_chars = _MSG_CHAR_LIMIT

    def __init__(self, bot_token: str, chat_id: str, db: Optional[Database] = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.db = db   # 用于 offset 持久化；传 None 则 offset 只存活于本进程

    # ------------------------------------------------------------------
    # 通道接口适配（其余通道共用同一组方法名，流水线不关心具体实现）
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id)

    def check(self) -> tuple[bool, str]:
        if not self.bot_token or not self.chat_id:
            return False, "未填写 notify.telegram.bot_token / chat_id"
        # token 形如 123456789:ABCdef...；格式不对时不如提前说清楚
        if not re.fullmatch(r"\d+:[\w-]+", self.bot_token):
            return False, "bot_token 格式不像合法 token（应形如 123456789:ABCdef...）"
        return True, "已配置"

    def send_text(self, text: str, keyboard: list | None = None) -> bool:
        return self.send_message(text, keyboard)

    def send_pages(self, pages: list[str],
                   keyboards: Optional[list[list[list[dict]]]] = None) -> bool:
        return self.send_report_pages(pages, keyboards)


    # ------------------------------------------------------------------
    # 底层调用
    # ------------------------------------------------------------------
    def _call(self, method: str, payload: dict, timeout: int = 30) -> dict:
        """POST 一个 Bot API 方法并校验 ok 字段（失败抛异常给上层退避）。"""
        url = _API_BASE.format(token=self.bot_token, method=method)
        resp = requests.post(url, json=payload, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{data.get('description')}")
        return data.get("result", {})

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2), reraise=True)
    def _send_message_once(self, text: str, keyboard: list | None) -> None:
        """单次发送（带重试装饰：共 3 次尝试，每次间隔 2s）。"""
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,   # 岗位链接的预览卡片会刷屏，关掉
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self._call("sendMessage", payload)

    def send_message(self, text: str, keyboard: list | None = None) -> None:
        """发送一条消息：超长自动按字符兜底分页（正常路径的分页由 report.py 做）。"""
        try:
            pages = self.paginate(text)
            for i, page in enumerate(pages):
                # keyboard 只挂在最后一页：按钮属于整条消息的"结尾交互"
                self._send_message_once(page, keyboard if i == len(pages) - 1 else None)
        except Exception as exc:
            # 共享约定：发送失败 log ERROR，不抛出打断流水线，下轮日报兜底
            logger.error(f"Telegram 发送失败（已重试 3 次，本轮放弃）：{exc}")

    def send_report_pages(self, pages: list[str],
                          keyboards: Optional[list[list[list[dict]]]] = None) -> bool:
        """逐页发送日报，页间 sleep 1s 防限流。返回是否全部发成功。"""
        ok = True
        for i, page in enumerate(pages):
            kb = keyboards[i] if keyboards and i < len(keyboards) else None
            try:
                self._send_message_once(page, kb)
            except Exception as exc:
                logger.error(f"日报第 {i + 1}/{len(pages)} 页发送失败：{exc}")
                ok = False
            time.sleep(1)   # Telegram 对高频 sendMessage 限流约 1 条/秒/会话
        return ok

    # ------------------------------------------------------------------
    # 反馈闭环（U2：P0 即带 ✅/❌ 按钮 + getUpdates 轮询）
    # ------------------------------------------------------------------
    def poll_feedback(self, on_callback: Callable[[str, str], None]) -> None:
        """非阻塞地拉一次反馈更新（由调度器按 feedback_poll_seconds 周期调用）。

        callback_data 格式 "fb:<job_uid>:<action>"，解析成功即回调
        on_callback(job_uid, action) → Pipeline 落 user_feedback 表。
        offset 持久化到 SQLite：进程重启后不会把昨天的按钮点击再处理一遍。
        """
        offset = self._load_offset()
        try:
            updates = self._call(
                "getUpdates",
                {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]},
                timeout=15,
            )
        except Exception as exc:
            logger.debug(f"getUpdates 轮询失败（下轮再试）：{exc}")
            return

        for update in updates or []:
            update_id = update.get("update_id", 0)
            offset = max(offset, update_id + 1)   # +1 = 告知服务端"这条已消费"
            query = update.get("callback_query") or {}
            data = str(query.get("data") or "")
            # 注意：job_uid 本身形如 "jobsdb:12345678"，内部含冒号，因此
            # 不能用 data.split(":")（会切成 4 段导致解析失败）——
            # 先剥 "fb:" 前缀，再从右侧切一刀分出 job_uid 与 action。
            job_uid = action = ""
            if data.startswith("fb:"):
                job_uid, _, action = data[3:].rpartition(":")
            if job_uid and action:
                try:
                    on_callback(job_uid, action)
                except Exception as exc:
                    logger.error(f"处理反馈回调失败 {job_uid}/{action}：{exc}")
                # 无论业务处理结果如何都应答按钮（否则用户端一直转小圈）
                try:
                    self._call("answerCallbackQuery", {"callback_query_id": query.get("id", "")})
                except Exception as exc:
                    logger.debug(f"answerCallbackQuery 失败（忽略）：{exc}")
        self._save_offset(offset)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def paginate(self, text: str, limit: int = _MSG_CHAR_LIMIT) -> list[str]:
        """字符级兜底分页（按换行优先切）。正常日报分页在 report.py 的
        卡片边界完成，这里只兜住"单条消息超长"的极端情况。"""
        if len(text) <= limit:
            return [text]
        pages: list[str] = []
        while text:
            cut = text.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            pages.append(text[:cut].rstrip())
            text = text[cut:].lstrip("\n")
        return pages

    def _load_offset(self) -> int:
        if self.db is None:
            return 0
        try:
            return int(self.db.get_state(_OFFSET_KEY, "0"))
        except ValueError:
            return 0

    def _save_offset(self, offset: int) -> None:
        if self.db is not None:
            self.db.set_state(_OFFSET_KEY, str(offset))
