"""SQLite 数据层：建表、CRUD、去重、统计（架构文档 4.3 节）。

为什么手写 SQL 而不用 ORM：单用户 + 单文件 SQLite 场景，SQL 语句本身就是
最好的文档，且利于学习者直接对照表结构理解数据流。

关键约定（第 8 节）：
- WAL 模式：Windows 下并发读写更稳（调度器写、stats 命令读互不阻塞）；
- 双保险去重：job_uid 主键 + url_normalized 唯一索引；
- 所有写操作统一 try/except + 事务，单条失败只记日志、不中断流水线；
- 时间一律存香港时区 ISO 8601 字符串。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from loguru import logger

from hk_job_radar.models import (
    ApplicationRecord,
    JobPosting,
    now_hk,
    parse_iso,
    to_iso,
)

# 建表 SQL（IF NOT EXISTS：进程每次启动都会调 init_schema，必须可重入）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_uid             TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    url                 TEXT NOT NULL,
    url_normalized      TEXT NOT NULL,
    salary_text         TEXT DEFAULT '',
    location            TEXT DEFAULT '',
    posted_at           TEXT,
    description_snippet TEXT DEFAULT '',
    keyword_matched     TEXT DEFAULT '',
    llm_score           INTEGER,
    llm_reason          TEXT DEFAULT '',
    direct_apply        INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'new',
    first_seen_at       TEXT NOT NULL,
    reported            INTEGER DEFAULT 0,
    UNIQUE(source, external_id),
    UNIQUE(url_normalized)
);

CREATE TABLE IF NOT EXISTS user_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid     TEXT NOT NULL REFERENCES jobs(job_uid),
    action      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid         TEXT REFERENCES jobs(job_uid),
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    applied_date    TEXT NOT NULL,
    channel         TEXT NOT NULL,
    resume_version  TEXT NOT NULL,
    status          TEXT DEFAULT 'applied'
);

CREATE TABLE IF NOT EXISTS run_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- kv 表（架构文档之外的轻量扩展）：存放 Telegram getUpdates 的 offset 等
-- 需要跨进程持久化的小状态。放 SQLite 而不是本地文件，是为了和业务数据
-- 共享同一套事务与备份路径。
CREATE TABLE IF NOT EXISTS kv_store (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_reported ON jobs(reported);
CREATE INDEX IF NOT EXISTS idx_run_logs_created ON run_logs(created_at);
"""


class Database:
    """jobs / user_feedback / applications / run_logs / kv_store 的全部读写。"""

    def __init__(self, db_path: str) -> None:
        # check_same_thread=False：APScheduler 可能从别的线程触发任务
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        """建表 + 打开 WAL。可重入，每次进程启动调用一次。"""
        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        except sqlite3.Error as exc:
            # 建表失败属于致命错误（库文件损坏/磁盘满），这里直接抛出让上层终止
            logger.error(f"数据库初始化失败：{exc}")
            raise

    # ------------------------------------------------------------------
    # 岗位 CRUD
    # ------------------------------------------------------------------
    def upsert_job(self, job: JobPosting) -> bool:
        """插入岗位，返回"是否为新岗位"。

        去重双保险：主键 job_uid 冲突或 url_normalized 唯一索引冲突都视为
        "已存在"→ 返回 False，调用方跳过打分/推送，注意力不被重复消耗。
        """
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO jobs (job_uid, source, external_id, title, company,
                           url, url_normalized, salary_text, location, posted_at,
                           description_snippet, keyword_matched, llm_score, llm_reason,
                           direct_apply, status, first_seen_at, reported)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        job.job_uid, job.source, job.external_id, job.title, job.company,
                        job.url, job.url_normalized, job.salary_text, job.location,
                        to_iso(job.posted_at) if job.posted_at else None,
                        job.description_snippet, job.keyword_matched,
                        job.llm_score, job.llm_reason,
                        1 if job.direct_apply else 0, job.status, to_iso(job.first_seen_at),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            logger.debug(f"重复岗位已跳过：{job.job_uid}")
            return False
        except sqlite3.Error as exc:
            # 共享约定：单条写失败只记日志，绝不打断整轮监控
            logger.error(f"入库失败 {job.job_uid}：{exc}")
            return False

    def update_score(self, job: JobPosting) -> None:
        """LLM 打分完成后回写分数与理由（新岗位是先入库、后打分）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET llm_score = ?, llm_reason = ? WHERE job_uid = ?",
                    (job.llm_score, job.llm_reason, job.job_uid),
                )
        except sqlite3.Error as exc:
            logger.error(f"回写打分失败 {job.job_uid}：{exc}")

    def get_job(self, job_uid: str) -> JobPosting | None:
        """按主键取单个岗位（cli apply / 反馈处理时用）。"""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_uid = ?", (job_uid,)
        ).fetchone()
        return self._row_to_job(row) if row else None

    def get_unreported_jobs(self, score_threshold: int, show_unscored: bool) -> list[JobPosting]:
        """取"待进日报"的岗位：reported=0 且（达标 或（未打分且允许展示））。

        排序：分数降序、未打分的垫底——用户从上往下看，最先看到的永远最值得看。
        """
        sql = """
            SELECT * FROM jobs
            WHERE reported = 0
              AND (llm_score >= ?
                   OR (llm_score IS NULL AND ?))
            ORDER BY (llm_score IS NULL) ASC, llm_score DESC, first_seen_at ASC
        """
        rows = self.conn.execute(sql, (score_threshold, 1 if show_unscored else 0)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def mark_reported(self, job_uids: list[str]) -> None:
        """标记已推送，防止下一轮重复推送（P0-5/US-4 的最后一环）。"""
        if not job_uids:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    "UPDATE jobs SET reported = 1, status = 'reported' "
                    "WHERE job_uid = ? AND status = 'new'",
                    [(uid,) for uid in job_uids],
                )
        except sqlite3.Error as exc:
            logger.error(f"标记已推送失败：{exc}")

    def update_status(self, job_uid: str, status: str) -> None:
        """更新岗位状态（反馈 interested/rejected、副驾 applied 等）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET status = ? WHERE job_uid = ?", (status, job_uid)
                )
        except sqlite3.Error as exc:
            logger.error(f"更新状态失败 {job_uid}：{exc}")

    # ------------------------------------------------------------------
    # 反馈 / 投递留档
    # ------------------------------------------------------------------
    def save_feedback(self, job_uid: str, action: str) -> None:
        """记录 Telegram 按钮反馈（interested / not_interested / clicked）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO user_feedback (job_uid, action, created_at) VALUES (?,?,?)",
                    (job_uid, action, to_iso(now_hk())),
                )
        except sqlite3.Error as exc:
            logger.error(f"保存反馈失败 {job_uid}/{action}：{exc}")

    def save_application(self, record: ApplicationRecord) -> None:
        """投递留档（T05 副驾确认提交后调用）。"""
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO applications
                       (job_uid, company, title, applied_date, channel, resume_version, status)
                       VALUES (?,?,?,?,?,?,?)""",
                    (record.job_uid, record.company, record.title, record.applied_date,
                     record.channel, record.resume_version, record.status),
                )
        except sqlite3.Error as exc:
            logger.error(f"投递留档失败 {record.job_uid}：{exc}")

    # ------------------------------------------------------------------
    # 运行日志与统计
    # ------------------------------------------------------------------
    def log_event(self, source: str, event: str, detail: dict[str, Any] | None = None) -> None:
        """写一条运行事件（cycle_ok / page_fetched / limit_hit / scrape_failed...）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO run_logs (source, event, detail, created_at) VALUES (?,?,?,?)",
                    (source, event, json.dumps(detail or {}, ensure_ascii=False), to_iso(now_hk())),
                )
        except sqlite3.Error as exc:
            logger.error(f"写运行日志失败 {source}/{event}：{exc}")

    def today_counts(self, source: str) -> int:
        """某站今日已抓取页数（香港自然日）——每日上限的计数依据。

        计数方式：统计 run_logs 中该站今天的 page_fetched 事件条数。
        """
        today = now_hk().strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM run_logs "
            "WHERE source = ? AND event = 'page_fetched' AND created_at LIKE ?",
            (source, f"{today}%"),
        ).fetchone()
        return int(row["n"]) if row else 0

    def get_stats(self) -> dict[str, Any]:
        """cli stats 命令的数据来源：今日抓取量 / 新岗位 / 推送 / 反馈一览。"""
        today = now_hk().strftime("%Y-%m-%d")
        out: dict[str, Any] = {"date": today, "pages_by_source": {}, "jobs": {}}
        for row in self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM run_logs "
            "WHERE event = 'page_fetched' AND created_at LIKE ? GROUP BY source",
            (f"{today}%",),
        ):
            out["pages_by_source"][row["source"]] = row["n"]

        def _count(sql: str, params: tuple = ()) -> int:
            r = self.conn.execute(sql, params).fetchone()
            return int(r[0]) if r else 0

        out["jobs"]["new_today"] = _count(
            "SELECT COUNT(*) FROM jobs WHERE first_seen_at LIKE ?", (f"{today}%",))
        out["jobs"]["unreported"] = _count("SELECT COUNT(*) FROM jobs WHERE reported = 0")
        out["jobs"]["reported_today"] = _count(
            "SELECT COUNT(*) FROM jobs WHERE reported = 1 AND first_seen_at LIKE ?",
            (f"{today}%",))
        out["jobs"]["unscored_total"] = _count(
            "SELECT COUNT(*) FROM jobs WHERE llm_score IS NULL")
        out["jobs"]["high_score_total"] = _count(
            "SELECT COUNT(*) FROM jobs WHERE llm_score >= 80")
        out["feedback_today"] = _count(
            "SELECT COUNT(*) FROM user_feedback WHERE created_at LIKE ?", (f"{today}%",))
        out["events_today"] = {}
        for row in self.conn.execute(
            "SELECT event, COUNT(*) AS n FROM run_logs WHERE created_at LIKE ? "
            "GROUP BY event", (f"{today}%",),
        ):
            out["events_today"][row["event"]] = row["n"]
        return out

    # ------------------------------------------------------------------
    # kv 小状态（Telegram offset 等）
    # ------------------------------------------------------------------
    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM kv_store WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO kv_store (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        except sqlite3.Error as exc:
            logger.error(f"写 kv 失败 {key}：{exc}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobPosting:
        """数据库行 → JobPosting（get_unreported_jobs / get_job 共用）。"""
        return JobPosting(
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            company=row["company"],
            url=row["url"],
            url_normalized=row["url_normalized"],
            salary_text=row["salary_text"] or "",
            location=row["location"] or "",
            posted_at=parse_iso(row["posted_at"]) if row["posted_at"] else None,
            description_snippet=row["description_snippet"] or "",
            keyword_matched=row["keyword_matched"] or "",
            llm_score=row["llm_score"],
            llm_reason=row["llm_reason"] or "",
            direct_apply=bool(row["direct_apply"]),
            status=row["status"] or "new",
            first_seen_at=parse_iso(row["first_seen_at"]) or now_hk(),
        )

    def close(self) -> None:
        self.conn.close()
