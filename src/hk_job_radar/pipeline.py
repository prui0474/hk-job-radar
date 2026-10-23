"""监控流水线编排（Pipeline）——整个系统的业务心脏（架构 5.1 时序图）。

为什么所有编排放一个类里：抓取、初筛、打分、入库、推送每一步都可独立
替换（适配器模式 + 简单组合），但"步骤之间的顺序与数据流"必须有且只有
一处定义——pipeline.py 就是这一处。用户学习时读懂这一个文件，就读懂了
整个系统的运行逻辑。

每轮监控周期（run_monitoring_cycle）：
  各启用站点 → 每日上限检查 → 关键词轮转取本轮子集 → 抓取(退避) → 关键词初筛
  → 新岗位入库去重 → LLM 打分(失败降级) → ≥80 分即时推送 → 周期摘要入 run_logs
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

import yaml
from loguru import logger

from hk_job_radar.config import AppConfig, PROFILE_PATH
from hk_job_radar.db import Database
from hk_job_radar.models import HK_TZ, JobPosting, now_hk
from hk_job_radar.notify.dispatch import Notifier
from hk_job_radar.scoring.keyword_filter import KeywordFilter
from hk_job_radar.scoring.llm_scorer import LLMScorer
from hk_job_radar.scrapers import get_scraper
from hk_job_radar.scrapers.browser import BrowserManager


def load_user_profile_prompt() -> str:
    """读取 profile.yaml 的 background 段作为打分画像（U4：先占位，可热更新）。

    文件缺失/格式错误都不致命：退回 LLMScorer 内置的兜底画像，
    保证断链路时打分仍可用（宁可打分粗一点，不可打分停摆）。
    """
    try:
        data = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}
        text = (data.get("background") or "").strip() if isinstance(data, dict) else ""
        return text
    except FileNotFoundError:
        logger.warning(f"未找到 {PROFILE_PATH}，LLM 打分将使用内置占位画像。")
        return ""
    except yaml.YAMLError as exc:
        logger.warning(f"profile.yaml 解析失败（使用占位画像）：{exc}")
        return ""


class Pipeline:
    """监控流水线：唯一持有全部执行组件的编排者。"""

    def __init__(self, config: AppConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.keyword_filter = KeywordFilter(config)
        self.notifier = Notifier.from_config(config, db)
        # LLM 延迟创建：创建依赖外部凭据，等真正用到再报错/降级
        self._llm: LLMScorer | None = None
        # 浏览器是独占资源：同一个 user_data_dir 不能被两个 Chromium 同时打开
        # （第二个会报 "profile in use"）。调度器的监控任务与日报任务是不同线程，
        # 整点撞在一起时就会打架，所以这里串行化——日报要复核详情页也得排队。
        self._browser_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 懒加载组件
    # ------------------------------------------------------------------
    @property
    def llm(self) -> LLMScorer:
        if self._llm is None:
            self._llm = LLMScorer(self.config.llm, load_user_profile_prompt())
        return self._llm

    # ------------------------------------------------------------------
    # 主链路：监控周期
    # ------------------------------------------------------------------
    def run_monitoring_cycle(self) -> dict[str, Any]:
        """一轮完整监控（调度器每 poll_interval_minutes 触发一次）。

        任何单站失败都被限制在本轮本站内部——绝不允许一个站点的故障
        拖垮其他站点或让调度进程退出（架构第 8 节铁律）。
        """
        logger.info("cycle start")
        summary: dict[str, Any] = {"started_at": now_hk().isoformat(timespec="seconds"),
                                   "sources": {}, "pushed_realtime": 0}
        now = now_hk()
        # 时间窗分两种情况：
        # 1) 首次运行（kv 无 backfill_done 标记）：放宽到 max_job_age_days 天，一次性
        #    把"当前在招的相关岗位"抓回来，避免用户首跑只看到最近几十分钟的岗位
        #    （冷启动问题）。回填完成后写标记，后续周期回到增量窗。
        # 2) 常规增量：按"上次跑完到现在隔了多久"往前推（见 _incremental_window_minutes）。
        #    ⚠️ 这一条对"每天只开机一小会儿"的用法是命门：如果固定只看过去 40 分钟，
        #    那么关机 23 小时后开机，这一天里发布的岗位绝大部分都在窗外、会被漏掉。
        # 岗位新鲜度上限（max_job_age_days）在 _run_single_source 里再统一过滤一次，
        # 作为最终保证——无论回填还是增量，入库的岗位都不超过该天数。
        backfill_done = self.db.get_state("backfill_done") == "1"
        gap_minutes = self._minutes_since_last_cycle(now)
        if backfill_done:
            window_minutes = self._incremental_window_minutes(now, gap_minutes)
            cutoff = now - timedelta(minutes=window_minutes)
        else:
            cutoff = now - timedelta(days=self.config.max_job_age_days)
            logger.info(f"首次运行：进入回填模式（抓取最近 {self.config.max_job_age_days} 天在招岗位）")
        browser = BrowserManager(self.config)
        try:
            with self._browser_lock:
                for source in self.config.enabled_sources():
                    summary["sources"][source] = self._run_single_source(source, browser, cutoff)
        finally:
            browser.close()   # 每轮周期结束必关浏览器，防进程泄漏
        # 汇总各站点的即时推送数（此前这个值一直是 0，`once` 打印的实时推送数
        # 因此恒为 0——顺手修掉，让统计口径与真实行为一致）
        summary["pushed_realtime"] = sum(
            int(stat.get("realtime_pushed", 0)) for stat in summary["sources"].values())
        if not backfill_done:
            # 无论本轮抓到多少岗位，都算回填完成——避免抓取失败导致永远停留在
            # 回填模式（那样每天都会重复刷 7 天旧岗位、重复花 LLM 的钱）。
            self.db.set_state("backfill_done", "1")
            logger.info("回填完成，后续周期恢复增量抓取")
        # 记录本轮结束时间：下一轮据此决定"往前看多久"（跨关机时段的关键依据）。
        # 放在最后写：若本轮中途崩了，时间戳不前进 → 下一轮窗口更宽，宁可多抓不漏。
        self.db.set_state("last_cycle_at", now_hk().isoformat(timespec="seconds"))
        # 日报兜底：电脑在日报时间点时是关着的 → 那条定时日报永远发不出去。
        # 这里在本轮数据齐了之后补发一次（只在"跨了一次长时间关机"或"已过日报时间"时触发）。
        self.maybe_send_catchup_report(gap_minutes)
        self.db.log_event("system", "cycle_ok", summary)
        logger.info(f"cycle done：{json.dumps(summary, ensure_ascii=False, default=str)}")
        return summary

    # ------------------------------------------------------------------
    def _minutes_since_last_cycle(self, now: datetime) -> float:
        """距上次成功跑完一轮过了多少分钟（无记录时返回"新鲜度上限"那么久）。

        这个值是增量时间窗的依据：常驻运行时它约等于轮询间隔；用户关机一晚再
        开机时它是十几个小时，窗口就自动放宽到十几个小时，从而补回关机期间发布
        的岗位。
        """
        raw = self.db.get_state("last_cycle_at")
        fallback = float(self.config.max_job_age_days * 1440)
        if not raw:
            return fallback
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:      # 状态被人手改坏时不致命：按最宽窗口处理
            return fallback
        if last.tzinfo is None:
            last = last.replace(tzinfo=HK_TZ)
        return max(0.0, (now - last).total_seconds() / 60)

    def _incremental_window_minutes(self, now: datetime, gap_minutes: float) -> int:
        """常规增量抓取要往前看多少分钟。

        = max(2 个轮询周期, 上次跑完至今 + 5 分钟缓冲)，再以岗位新鲜度上限封顶。
        常驻运行时退化为原来的"多看 2 个轮询周期"（行为不变）；只在开机不规律时
        自动放宽，覆盖关机期间发布的岗位。
        """
        base = self.config.poll_interval_minutes * 2
        window = max(base, gap_minutes + 5)
        window = min(window, self.config.max_job_age_days * 1440)
        if window > base:
            logger.info(f"距上次运行 {gap_minutes:.0f} 分钟 → 本轮抓取时间窗放宽到 "
                        f"{window:.0f} 分钟（覆盖关机期间发布的岗位）")
        return int(window)

    def _filter_by_age(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """岗位新鲜度过滤：只保留最近 max_job_age_days 天发布的岗位。

        posted_at 为 None（时间解析失败）的岗位保留——宁可多看，不可漏看；
        verified_open=True 的保留——那是来源明确告知「还在收简历」的岗位
        （目前来自 Workday 的 canApply），能确认在招就不该按年龄砍掉。
        能解析出时间的其余岗位，过老的一律剔除，避免旧岗占用日报与 LLM 配额。
        """
        age_cutoff = now_hk() - timedelta(days=self.config.max_job_age_days)
        kept = [j for j in jobs
                if j.posted_at is None or j.verified_open or j.posted_at >= age_cutoff]
        if len(kept) < len(jobs):
            logger.info(f"新鲜度过滤：{len(jobs)} → {len(kept)}"
                        f"（剔除 {len(jobs) - len(kept)} 个超过 {self.config.max_job_age_days} 天的岗位）")
        return kept

    def _keyword_window(self, source: str) -> list[str]:
        """本轮该站点要抓的关键词子集（关键词轮转，见 config.keyword_rotation）。

        为什么放在 Pipeline 而不是适配器：轮转偏移必须落库（跨轮次、跨进程重启
        连续推进），而 db 只有 Pipeline 稳定持有；适配器只负责"把我给你的这批
        关键词抓完"。关闭轮转时原样返回全部关键词（旧行为）。
        """
        all_keywords = self.config.all_keywords()
        per_cycle = self.config.keywords_per_cycle_for_source(source)
        if per_cycle <= 0 or per_cycle >= len(all_keywords):
            return all_keywords
        state_key = f"keyword_offset_{source}"
        try:
            offset = int(self.db.get_state(state_key, "0") or "0")
        except ValueError:      # 状态被人手改坏时不致命：从头开始轮转
            offset = 0
        offset %= len(all_keywords)
        window = [all_keywords[(offset + i) % len(all_keywords)]
                  for i in range(per_cycle)]
        next_offset = (offset + per_cycle) % len(all_keywords)
        self.db.set_state(state_key, str(next_offset))
        logger.info(f"[{source}] 关键词轮转：本轮抓 {per_cycle}/{len(all_keywords)} 个"
                    f"（offset {offset}→{next_offset}，"
                    f"下一个从「{all_keywords[next_offset]}」开始）")
        return window

    def _run_single_source(self, source: str, browser: BrowserManager,
                           cutoff) -> dict[str, int]:
        """单站点的完整子流程，返回该站的小结（供周期摘要与 stats 用）。"""
        stat = {"fetched": 0, "kept": 0, "new": 0, "scored": 0, "degraded": 0}

        # 每日上限检查（时序图：P→D today_counts）——已达上限直接跳过该站
        limit = self.config.daily_limits.get(source, 0)
        used = self.db.today_counts(source)
        if limit and used >= limit:
            logger.warning(f"[{source}] 今日已抓 {used} 页 ≥ 上限 {limit}，本轮跳过。")
            self.db.log_event(source, "limit_hit", {"used": used, "limit": limit})
            return stat

        # 关键词轮转：本轮只抓一部分关键词，把每日页数配额摊到全天
        scraper = get_scraper(source, self.config, browser, db=self.db,
                              keywords=self._keyword_window(source))
        jobs = scraper.fetch_new_jobs(cutoff)
        # 新鲜度上限：只保留最近 max_job_age_days 天发布的岗位（posted_at 缺失的保留）
        jobs = self._filter_by_age(jobs)
        stat["fetched"] = len(jobs)

        kept = self.keyword_filter.filter(jobs)
        stat["kept"] = len(kept)

        # 去重入库：只有"新岗位"才值得花 LLM 的钱
        new_jobs: list[JobPosting] = []
        for job in kept:
            if self.db.upsert_job(job):
                new_jobs.append(job)
        stat["new"] = len(new_jobs)
        if not new_jobs:
            return stat

        # 打分（含降级），然后回写数据库
        scored_jobs = self.llm.score_batch(new_jobs)
        for job in scored_jobs:
            self.db.update_score(job)
            if job.llm_score is None:
                stat["degraded"] += 1
            else:
                stat["scored"] += 1

        # ≥80 分不等日报，发现即推（P1-1 前置，U5 默认开启）
        realtime_pushed = 0
        for job in scored_jobs:
            if job.llm_score is not None and job.llm_score >= self.config.realtime_threshold:
                self.check_high_score_push(job)
                realtime_pushed += 1
        stat["realtime_pushed"] = realtime_pushed
        return stat

    # ------------------------------------------------------------------
    # 高分即时推送
    # ------------------------------------------------------------------
    def check_high_score_push(self, job: JobPosting) -> None:
        """高分岗位即时推送并标记已报（防止日报再推一遍）。"""
        # 同一个岗位常同时挂在几个站上，十分钟内先后被抓到很常见。
        # 已经推过其中一条就不再推第二条——实时推送是「马上看」的场景，
        # 重复即噪音。（日报不同：那一批你都能看见，只做「另见 #N」标注。）
        duplicate = self.db.find_pushed_duplicate(job)
        if duplicate is not None:
            logger.info(
                f"实时推送跳过：{job.title}（{job.source}）与已推送的 "
                f"{duplicate.source} 那条是同一岗位")
            self.db.mark_reported([job.job_uid])
            return
        logger.info(f"高分岗位即时推送：{job.job_uid}（{job.llm_score} 分）")
        self.notifier.send_realtime(job)
        self.db.mark_reported([job.job_uid])

    # ------------------------------------------------------------------
    # 日报（每日 09:00 或 `hk_radar report now` 手动触发）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 日报取数与推送前复核
    # ------------------------------------------------------------------
    def _age_limits(self) -> dict:
        """岗位年龄的两条线：能复核的站放宽带，不能复核的站卡紧线。

        为什么要分两条：年龄和「还能不能投」根本不是一回事。实测 10 条 14 天前的
        JobsDB 岗位里 9 条仍在招——按年龄一刀切等于白扔机会；反过来一条 2 天前就
        撤下的广告，多留一天都是垃圾。所以「能查的交给查，查不了的才靠年龄兜」。

        返回空字典 = 只有一条线（全部按 report_max_age_days 卡）。这不仅是「没开复核」
        时的退化路径，也是必须的：不复核又放宽年龄，等于把一批早就招满的岗位
        原样推给用户。
        """
        if not self.config.liveness_check:
            return {}
        from hk_job_radar import liveness      # 延迟导入：不开复核的用户不加载这段
        return {
            "recheckable_sources": liveness.RECHECKABLE_SOURCES,
            "recheck_age_days": liveness.MAX_RECHECK_AGE_DAYS,
        }

    def _report_candidates(self) -> list[JobPosting]:
        """取本轮真正要推的岗位。

        为什么不是「直接取 reported=0」：那条 SQL 只看「推过没」和「分数够不够」，
        完全不看岗位是不是还开着。电脑关几天再开，队列里会攒下一批早就招满的岗位，
        一开日报就全推给用户——用户点进去看到「此職位已過期」。

        三道闸门，只砍「确认失效」的：
          1. 时效归档：只归档那些**注定推不出去**的——超过硬天花板（能复核的站 30 天、
             不能复核的站 report_max_age_days）；
          2. 时效过滤：SQL 层用同一套双轨年龄线再挡一次；
          3. 存活复核：对能开详情页的站（JobsDB / Indeed）逐个看一眼，
             看到「不再刊登 / 已过期 / 不再接受申请」才剔除，打不开一律放行。
        """
        days = self.config.report_max_age_days
        limits = self._age_limits()
        archived = self.db.archive_stale_jobs(days, **limits)
        if archived:
            logger.info(f"日报：{archived} 个岗位超过硬天花板仍未推送，已归档。")
            self.db.log_event("system", "stale_archived",
                              {"count": archived, "days": days, "limits": limits})

        jobs = self.db.get_unreported_jobs(
            self.config.score_threshold, self.config.show_unscored,
            max_age_days=days, **limits)
        if not jobs:
            return []
        return self._drop_closed_jobs(jobs)

    def _drop_closed_jobs(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """推送前把「已经关了」的岗位剔掉（只查 Indeed / JobsDB）。

        任何失败都必须放行：复核是为了少推垃圾，不是为了让推送失败。
        所以浏览器起不来、页面打不开、过盾卡住，全部按「岗位还有效」处理。
        """
        if not self.config.liveness_check:
            return jobs
        from hk_job_radar import liveness      # 延迟导入：不开详情的用户不加载这段代码

        try:
            if not self._browser_lock.acquire(timeout=180):
                logger.warning("推送前复核：浏览器被监控周期占用超过 180 秒，本轮跳过复核。")
                return jobs
            browser = None
            try:
                browser = BrowserManager(self.config)
                result = liveness.check_jobs(jobs, browser)
            finally:
                if browser is not None:
                    browser.close()
                self._browser_lock.release()
        except Exception as exc:
            logger.warning(f"推送前复核失败（本轮按全部有效处理）：{exc}")
            return jobs

        if result.closed:
            self.db.mark_closed([j.job_uid for j in result.closed])
            logger.info(f"推送前复核：{len(result.closed)} 个岗位已过期或不再接受申请，已从本轮排除。")
            self.db.log_event("system", "closed_dropped",
                              {"count": len(result.closed),
                               "uids": [j.job_uid for j in result.closed][:50]})
        closed_uids = {j.job_uid for j in result.closed}
        return [j for j in jobs if j.job_uid not in closed_uids]

    def run_daily_report(self, force: bool = False) -> bool:
        """汇总推送日报：取未报岗位 → 排版分页 → 发送 → 标记已报。

        返回值：是否真的推送了一份（False = 今日已推过 / 没有待推岗位）。

        force=True 用于用户手动 `report now`（"我就是要现在来一份"）；
        定时任务与开机补发都走 force=False，靠 report_sent_date 保证一天只推一次。
        """
        today = now_hk().strftime("%Y-%m-%d")
        if not force and self.db.get_state("report_sent_date") == today:
            logger.info("日报：今日已推送过，跳过（新岗位会累计到明天那份）。")
            return False
        try:
            jobs = self._report_candidates()
            if not jobs:
                # 注意：这里【不】写 report_sent_date —— 今天还没推过任何东西，
                # 后面一旦有岗位就可以补发（否则今天这份日报就彻底没了）。
                logger.info("日报：当前没有待推送的新岗位。")
                return False
            results = self.notifier.send_report(jobs, self.config.show_unscored)
            if not results:
                logger.error("日报：没有可用的推送通道，未发送（请检查 config.yaml 的 notify 段）。")
                return False
            if not any(results.values()):
                # 有通道但全部失败：不标记已报，留到下一轮重试（宁重复，不丢失）
                logger.error(f"日报：所有通道都发送失败 {results}，本轮不标记，下轮重试。")
                return False
            self.db.mark_reported([j.job_uid for j in jobs])
            self.db.set_state("report_sent_date", today)
            self.db.log_event("system", "daily_report_sent",
                              {"jobs": len(jobs), "channels": results})
            logger.info(f"日报已推送：{len(jobs)} 个岗位 → "
                        f"{'、'.join(k for k, v in results.items() if v)}。")
            return True
        except Exception as exc:
            # 日报失败不允许带崩调度器；未 mark_reported 的岗位下轮兜底
            logger.error(f"日报推送失败：{exc}")
            self.db.log_event("system", "daily_report_failed", {"error": str(exc)[:500]})
            return False

    def maybe_send_catchup_report(self, gap_minutes: float) -> None:
        """开机补发日报：电脑在日报时间点时是关着的，那条定时日报就永远发不出去。

        满足下面任一条件就补一份（每天最多一次，靠 report_sent_date 兜住）：
          A) 已经过了日报时间（report_time）——正常定时任务该发而没发成；
          B) 本轮是"跨了一次长时间关机后的第一轮"（间隔 > 2 小时）——用户刚开机，
             没必要让他干等到某个整点才看到结果。
        常驻运行（间隔 ≈ 轮询间隔）时两个条件都不成立，行为与以前完全一致。
        """
        if self.config.max_job_age_days < 1:      # 防御：配置异常时不做补发
            return
        today = now_hk().strftime("%Y-%m-%d")
        if self.db.get_state("report_sent_date") == today:
            return
        now = now_hk()
        try:
            hh, mm = (int(x) for x in self.config.report_time.split(":"))
        except ValueError:
            hh, mm = 9, 0
        past_report_time = (now.hour, now.minute) >= (hh, mm)
        fresh_session = gap_minutes > 120
        if not (past_report_time or fresh_session):
            return          # 还没到日报时间，交给定时任务
        # 这里只做便宜的「有没有东西可推」判断（归档 + 计数），不打开详情页复核——
        # 真正的复核在 run_daily_report 里做一次就够了，做两遍等于白花一倍时间。
        limits = self._age_limits()
        self.db.archive_stale_jobs(self.config.report_max_age_days, **limits)
        pending = self.db.get_unreported_jobs(
            self.config.score_threshold, self.config.show_unscored,
            max_age_days=self.config.report_max_age_days, **limits)
        if not pending:
            return          # 暂时没岗位可推，等下一轮再看看（不标记已发）
        reason = "已过日报时间" if past_report_time else "跨了长时间关机后刚开机"
        logger.info(f"开机补发日报（{reason}）：{len(pending)} 个待推岗位，立即推送。")
        self.run_daily_report()

    # ------------------------------------------------------------------
    # 反馈闭环（U2：按钮 → user_feedback 表）
    # ------------------------------------------------------------------
    def handle_feedback(self, job_uid: str, action: str) -> None:
        """Telegram 按钮回调落库 + 更新岗位状态。"""
        status_map = {"interested": "interested", "not_interested": "rejected",
                      "clicked": "clicked", "expired": "expired"}
        status = status_map.get(action)
        if status is None:
            logger.warning(f"未知反馈动作（忽略）：{action}")
            return
        self.db.save_feedback(job_uid, action)
        self.db.update_status(job_uid, status)
        logger.info(f"用户反馈已记录：{job_uid} → {action}")

    def poll_feedback_once(self) -> None:
        """调度器周期调用：拉一次用户点击反馈。

        只有支持交互的通道才会真正做事（目前仅 Telegram）——分发器内部已过滤，
        所以即便当前用的是邮件通道，这个调用也是安全的空操作。
        """
        try:
            self.notifier.poll_feedback(self.handle_feedback)
        except Exception as exc:
            logger.debug(f"反馈轮询异常（下轮重试）：{exc}")
