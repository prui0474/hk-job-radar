"""APScheduler 调度器（T03）：轮询 + 日报 + 反馈轮询 三个定时任务。

防呆三件套（架构第 8 节，缺一不可）：
- misfire_grace_time=300：任务错过触发点（如电脑睡眠）后 5 分钟内仍可补跑；
- max_instances=1：上一轮没跑完绝不叠加启动新一轮（防浏览器进程堆积）；
- coalesce=True：积压多次触发时合并为一次。
另一个铁律：任何任务异常都不允许让调度进程退出——统一由 _safe 包装兜底。
"""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from hk_job_radar.config import AppConfig
from hk_job_radar.models import HK_TZ
from hk_job_radar.pipeline import Pipeline

# 任务默认参数：所有 add_job 共用（写成 job_defaults 防止某个 add_job 忘配）
_JOB_DEFAULTS = {
    "misfire_grace_time": 300,
    "max_instances": 1,
    "coalesce": True,
}


def report_times_of(config: AppConfig) -> list[str]:
    """实际生效的每日日报时间点。

    配了 `report_times`（可以写好几个，如 09:00 和 18:30）就按它来；
    没配就退回单个 `report_time`。手动 `report now` 不受这里影响。
    """
    times = [str(t).strip() for t in (getattr(config, "report_times", []) or [])
             if str(t).strip()]
    return times or [config.report_time]


def _safe(fn, name: str):
    """把任务函数包成"永不抛出"的版本——调度器只管触发，异常全部就地消化。"""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.error(f"调度任务 [{name}] 异常（已吞掉，进程继续运行）：{exc}")
            return None
    wrapper.__name__ = name   # APScheduler 要求 job 有合法函数名
    return wrapper


class Scheduler:
    """BlockingScheduler 封装：启动后阻塞当前进程（`hk_radar run` 的常驻形态）。"""

    def __init__(self, config: AppConfig, pipeline: Pipeline) -> None:
        self.config = config
        self.pipeline = pipeline

    def start(self) -> None:
        """装配定时任务并阻塞启动。Ctrl+C 可安全退出。"""
        sched = BlockingScheduler(timezone=HK_TZ, job_defaults=_JOB_DEFAULTS)
        self.setup_monitoring_job(sched)
        self.setup_report_job(sched)
        self.setup_feedback_job(sched)

        times = "、".join(report_times_of(self.config))
        channels = "、".join(self.pipeline.notifier.labels) or "（未配置）"
        logger.info(
            f"调度器启动：监控每 {self.config.poll_interval_minutes} 分钟 · "
            f"日报 {times} · 推送通道 {channels} · "
            f"反馈轮询每 {self.config.feedback_poll_seconds}s。Ctrl+C 退出。")
        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号，调度器已停止。")

    # ------------------------------------------------------------------
    def setup_monitoring_job(self, sched: BlockingScheduler) -> None:
        """监控轮询任务。next_run_time=now：启动后立即跑第一轮，
        不用干等一个完整间隔才见效果（对新用户的第一印象很重要）。"""
        sched.add_job(
            _safe(self.pipeline.run_monitoring_cycle, "monitoring"),
            trigger="interval",
            minutes=self.config.poll_interval_minutes,
            id="monitoring",
            next_run_time=datetime.now(HK_TZ),
        )

    def setup_report_job(self, sched: BlockingScheduler) -> None:
        """每日日报任务（香港时间 cron）。

        时间点来自 report_times（可多个）；没配就用 report_time 那一个。
        """
        for idx, time_str in enumerate(report_times_of(self.config)):
            hour, minute = (int(x) for x in time_str.split(":"))
            sched.add_job(
                _safe(self.pipeline.run_daily_report, f"daily_report_{time_str}"),
                trigger="cron",
                hour=hour, minute=minute,
                id=f"daily_report_{idx}",
            )

    def setup_feedback_job(self, sched: BlockingScheduler) -> None:
        """反馈按钮轮询任务（U2：getUpdates + offset 持久化）。

        只有支持交互的通道才需要它（目前仅 Telegram）。用邮件/企微时启动这个
        任务纯属白跑——每小时几十次无用的网络请求，没必要。
        """
        if not self.pipeline.notifier.has_feedback_channel:
            logger.info("当前推送通道不支持点击反馈，跳过反馈轮询任务。")
            return
        sched.add_job(
            _safe(self.pipeline.poll_feedback_once, "feedback_poll"),
            trigger="interval",
            seconds=self.config.feedback_poll_seconds,
            id="feedback_poll",
        )
