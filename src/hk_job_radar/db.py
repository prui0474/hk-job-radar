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
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from hk_job_radar.dedup import NO_KEY, dedupe_key_of
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
    eligibility_note    TEXT DEFAULT '',
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

-- 状态流转留痕：每点一次工作台按钮就记一行「从哪到哪」，撤销靠它回退。
-- 为什么不用 applications 表：那张表是「投递档案」（一次投递一行、带简历版本），
-- 而这里要的是**任意状态变化的时间线**——约面、结束、撤销都要记。
-- 有了它，趋势图才知道「哪天投的」，撤销才知道「回到哪」。
CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid     TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 简历-JD 分析缓存：同一岗位只算一次，省 token。
-- signature = 模型名 + 简历全文的指纹。简历改了、或换了模型，指纹就变，
-- 旧结论自动失效——绝不能让人拿着上一版简历的分析去投。
CREATE TABLE IF NOT EXISTS job_analysis (
    job_uid     TEXT PRIMARY KEY,
    signature   TEXT NOT NULL,
    model       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_status_history_job ON status_history(job_uid);
CREATE INDEX IF NOT EXISTS idx_status_history_created ON status_history(created_at);
"""

#: 跨站指纹索引单独建：老库在补列之前不存在这一列，放进 _SCHEMA 会让
#: 「表已存在但列还没补」的库在建索引时报 no such column。所以先补列、再建索引。
_INDEX_DEDUPE = "CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);"


#: 工作台排序方式。`default` 沿用日报口径；`smart` 是综合排序（分数 + 新鲜度）。
#: 白名单之外的值一律按 default 处理——排序表达式要拼进 SQL，不能让外部字符串
#: 直接落进语句里。
BOARD_SORTS = ("default", "smart")

#: 综合排序的新鲜度衰减：挂满 `_FRESH_DAYS` 天的岗位，综合分＝分数 × 0.6。
#: 为什么是六折：招聘广告从发布到截止常有两三周，压太狠会把仍在招的好岗位
#: 一起埋掉。六折刚好让「1 天前的 82 分」压过「19 天前的 88 分」（这两种是
#: 实测库里最常见的组合），又不至于让 88 分的高分岗彻底沉底。
FRESH_DECAY = 0.4
FRESH_DAYS = 30


def _order_clause(sort: str) -> str:
    """工作台的排序子句。

    `smart` 的口径：`综合分 = 分数 × (1 - 0.4 × min(年龄, 30) / 30)`。
    - 今天发的：原分；
    - 15 天前：八折；
    - 30 天及更早：六折（下限，不再继续掉）。
    未打分的岗位仍然垫底，否则「还没评分的」会浮到最前面。

    年龄来自 `posted_at`（岗位自己写的发布日），没有就退回 `first_seen_at`
    （雷达第一次看到它）。日期解析不出来时按最老处理——宁可把一条脏数据排到
    后面，也不要让它因为「算不出年龄」而被当成今天刚发的顶上去。
    """
    if sort == "smart":
        # 最外层的 COALESCE 是兜底：两个时间列都是 NULL 时，减法得 NULL，
        # 而 SQLite 的 max(NULL, 0) 仍返回 NULL（不是 0）——整条乘积跟着变 NULL，
        # 排序时会被当成「最小」沉到最底下，与上一条「按最老处理」的口径相反。
        # 所以先把减法兜成 FRESH_DAYS，再做截断。
        age = (f"MIN(MAX(COALESCE(julianday('now') - julianday("
               f"COALESCE(posted_at, first_seen_at)), {FRESH_DAYS}), 0), {FRESH_DAYS})")
        return (" ORDER BY (llm_score IS NULL) ASC,"
                f" (COALESCE(llm_score, 0) * (1.0 - {FRESH_DECAY} * {age}"
                f" / {FRESH_DAYS}.0)) DESC,"
                " llm_score DESC, COALESCE(posted_at, first_seen_at) DESC")
    return (" ORDER BY (llm_score IS NULL) ASC, llm_score DESC,"
            " COALESCE(posted_at, first_seen_at) DESC")


def composite_score(score: int | None, age_days: float) -> float | None:
    """综合分（与 `_order_clause("smart")` 的 SQL 完全同口径）。

    这里和 SQL 各写一遍算法是刻意的：排序必须在数据库里做（否则要先全量读出来
    再排，`LIMIT` 就没意义了），而卡片上要显示的数字又必须在 Python 里算。
    两者共用上面的 `FRESH_DECAY` / `FRESH_DAYS` 两个常量——只要改常量就同步，
    不会出现「SQL 按 A 排、界面按 B 显示」的分叉。

    `score is None`（还没被模型打过分）返回 None：这类岗位在 SQL 里被单独
    `ORDER BY ... ASC` 垫底，没有「综合分」可言。
    """
    if score is None:
        return None
    age = min(max(float(age_days), 0.0), float(FRESH_DAYS))
    return score * (1.0 - FRESH_DECAY * age / FRESH_DAYS)


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
            self._ensure_columns()
            self.conn.execute(_INDEX_DEDUPE)   # 必须在补列之后，否则旧库报 no such column
            self.conn.commit()
            self.backfill_dedupe_keys()
        except sqlite3.Error as exc:
            # 建表失败属于致命错误（库文件损坏/磁盘满），这里直接抛出让上层终止
            logger.error(f"数据库初始化失败：{exc}")
            raise

    #: 后来新增的列：(列名, 建列声明)。
    #: CREATE TABLE IF NOT EXISTS 对**已存在的表**不会补列，所以老用户的库升级后
    #: 会缺这些字段，读的时候直接报 "no such column"。这里逐列检查、缺了就补。
    #: 只允许加「有默认值、可空」的列——老行补不回来历史数据，只能给默认值。
    _ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
        ("eligibility_note", "TEXT DEFAULT ''"),
        # 跨站重复指纹。老库补列后所有行都是空串，由
        # backfill_dedupe_keys() 一次性算好；算不出来的写 NO_KEY 哨兵值。
        ("dedupe_key", "TEXT DEFAULT ''"),
    )

    def _ensure_columns(self) -> None:
        """给老库补上后加的列（幂等；已有就跳过）。"""
        existing = {row["name"] for row in
                    self.conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, ddl in self._ADDED_COLUMNS:
            if name in existing:
                continue
            try:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
                logger.info(f"数据库升级：jobs 表补上 {name} 列")
            except sqlite3.Error as exc:
                # 补列失败不该让整个程序起不来：缺字段的地方都做了「取不到就当空」处理
                logger.warning(f"数据库升级失败（补 {name} 列）：{exc}")

    def backfill_dedupe_keys(self) -> int:
        """给还没有指纹的老岗位补算一笔，返回补了几条。

        只在补列后的第一次启动真正干活（之后查不到空值就立刻返回）。
        算不出指纹的写 NO_KEY，避免每次启动都重算同一批算不出来的行。
        """
        rows = self.conn.execute(
            "SELECT job_uid, company, title FROM jobs "
            "WHERE dedupe_key IS NULL OR dedupe_key = ''"
        ).fetchall()
        if not rows:
            return 0
        try:
            with self.conn:
                self.conn.executemany(
                    "UPDATE jobs SET dedupe_key = ? WHERE job_uid = ?",
                    [(dedupe_key_of(row["company"], row["title"]), row["job_uid"])
                     for row in rows],
                )
        except sqlite3.Error as exc:
            logger.warning(f"补算跨站指纹失败（下次启动会重试）：{exc}")
            return 0
        logger.info(f"数据库升级：为 {len(rows)} 个历史岗位补算了跨站重复指纹")
        return len(rows)

    def find_pushed_duplicate(self, job: JobPosting) -> JobPosting | None:
        """找出「已经推送过的、同一岗位的另一条记录」；没有则返回 None。

        实时推送专用：同一个岗位十分钟前刚从 JobsDB 推给你，LinkedIn 又抓到了，
        再推一次是纯骚扰。日报不走这里——日报里的重复只做标记，因为那份日报
        里的两条你都能看见，自己判断更快（见 dedup.py 的模块说明）。
        """
        key = job.dedupe_key or dedupe_key_of(job.company, job.title)
        if key == NO_KEY:
            return None
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ? AND reported = 1 "
            "AND job_uid != ? ORDER BY first_seen_at LIMIT 1",
            (key, job.job_uid),
        ).fetchone()
        return self._row_to_job(row) if row else None

    # ------------------------------------------------------------------
    # 岗位 CRUD
    # ------------------------------------------------------------------
    def upsert_job(self, job: JobPosting) -> bool:
        """插入岗位，返回"是否为新岗位"。

        去重双保险：主键 job_uid 冲突或 url_normalized 唯一索引冲突都视为
        "已存在"→ 返回 False，调用方跳过打分/推送，注意力不被重复消耗。
        """
        try:
            # 指纹在入库这一刻算好并写回对象：后续的日报标注、实时推送判重
            # 都直接读这个字段，不必重复计算（口径也只有一个）。
            if not job.dedupe_key:
                job.dedupe_key = dedupe_key_of(job.company, job.title)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO jobs (job_uid, source, external_id, title, company,
                           url, url_normalized, salary_text, location, posted_at,
                           description_snippet, keyword_matched, llm_score, llm_reason,
                           direct_apply, status, first_seen_at, reported, eligibility_note,
                           dedupe_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                    (
                        job.job_uid, job.source, job.external_id, job.title, job.company,
                        job.url, job.url_normalized, job.salary_text, job.location,
                        to_iso(job.posted_at) if job.posted_at else None,
                        job.description_snippet, job.keyword_matched,
                        job.llm_score, job.llm_reason,
                        1 if job.direct_apply else 0, job.status, to_iso(job.first_seen_at),
                        job.eligibility_note, job.dedupe_key,
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
        """LLM 打分完成后回写分数、理由与资格提示（新岗位是先入库、后打分）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET llm_score = ?, llm_reason = ?, eligibility_note = ? "
                    "WHERE job_uid = ?",
                    (job.llm_score, job.llm_reason, job.eligibility_note, job.job_uid),
                )
        except sqlite3.Error as exc:
            logger.error(f"回写打分失败 {job.job_uid}：{exc}")

    def get_job(self, job_uid: str) -> JobPosting | None:
        """按主键取单个岗位（cli apply / 反馈处理时用）。"""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_uid = ?", (job_uid,)
        ).fetchone()
        return self._row_to_job(row) if row else None

    # 岗位「年龄」的统一口径：优先用发布日（posted_at），抓不到发布日就用入库时间。
    # 为什么不用 first_seen_at 优先：Indeed 的列表页根本不展示发布时间，
    # 那类岗位只能退化成「第一次被我们看到的时间」，而 LinkedIn / JobsDB 有真实发布日，
    # 用发布日才能识别出「抓的时候就已经放了好几天的旧岗位」。
    #
    # 用 datetime() 包一层的原因：库里存的是带时区的 ISO 字符串（+08:00），
    # 直接做字符串比较只在时区完全一致时才正确；datetime() 会先统一折算成 UTC。
    # 解析失败（返回 NULL）的脏数据一律视为「新鲜」放行——宁可多推一个，不可错杀。
    _AGE_SQL = "datetime(COALESCE(posted_at, first_seen_at))"

    def get_unreported_jobs(self, score_threshold: int, show_unscored: bool,
                            max_age_days: int | None = None,
                            recheckable_sources: tuple[str, ...] = (),
                            recheck_age_days: int | None = None) -> list[JobPosting]:
        """取"待进日报"的岗位：reported=0 且（达标 或（未打分且允许展示））。

        排序：分数降序、未打分的垫底——用户从上往下看，最先看到的永远最值得看。

        年龄是**双轨**的，因为「老」不等于「失效」：
          - recheckable_sources（能复核的站，见 liveness.RECHECKABLE_SOURCES）：
            只卡 recheck_age_days 这道硬天花板，中间那段交给推送前复核逐条判断。
            实测 10 条 14 天前的 JobsDB 岗位里 9 条仍在招，一刀切会白扔好机会。
          - 其余（LinkedIn 这类不能复核的站）：卡 max_age_days，只能靠年龄兜。

        不传 recheckable_sources / recheck_age_days 时退化为「全部按 max_age_days 卡」，
        也就是旧行为（`liveness_check` 关掉时就走这条路）。
        """
        sql = f"""
            SELECT * FROM jobs
            WHERE reported = 0
              AND (llm_score >= ?
                   OR (llm_score IS NULL AND ?))
        """
        params: list = [score_threshold, 1 if show_unscored else 0]
        if max_age_days is not None:
            soft_cutoff = to_iso(now_hk() - timedelta(days=max_age_days))
            if recheckable_sources and recheck_age_days is not None:
                marks = ",".join("?" * len(recheckable_sources))
                sql += (f"  AND ((source IN ({marks})"
                        f" AND ({self._AGE_SQL} IS NULL"
                        f" OR {self._AGE_SQL} >= datetime(?)))"
                        f" OR (source NOT IN ({marks})"
                        f" AND ({self._AGE_SQL} IS NULL"
                        f" OR {self._AGE_SQL} >= datetime(?))))\n")
                params.extend(list(recheckable_sources))
                params.append(to_iso(now_hk() - timedelta(days=recheck_age_days)))
                params.extend(list(recheckable_sources))
                params.append(soft_cutoff)
            else:
                sql += (f"  AND ({self._AGE_SQL} IS NULL"
                        f" OR {self._AGE_SQL} >= datetime(?))\n")
                params.append(soft_cutoff)
        sql += " ORDER BY (llm_score IS NULL) ASC, llm_score DESC, first_seen_at ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def archive_stale_jobs(self, max_age_days: int,
                           recheckable_sources: tuple[str, ...] = (),
                           recheck_age_days: int | None = None) -> int:
        """把「超过时效线还没推出去」的岗位归档，返回归档条数。

        归档 = reported 置 1（不再进日报）+ status 置 'stale'。
        为什么不直接删：用户可能想回看「当时有哪些机会」，删了就没法复盘；
        而且统计口径（抓了多少、筛掉多少）也依赖这些记录。

        年龄口径与 get_unreported_jobs 保持一致（双轨，见那边的说明）：
        能复核的站用 recheck_age_days 这道宽线，不能复核的用 max_age_days。

        注意：这里不分分数高低，一律归档。低于门槛的岗位本来就永远推不出去，
        留着只会让 `stats` 里的「待推」数字虚高、掩盖真实待推量。
        """
        soft_cutoff = to_iso(now_hk() - timedelta(days=max_age_days))
        if recheckable_sources and recheck_age_days is not None:
            marks = ",".join("?" * len(recheckable_sources))
            where = (f"(source IN ({marks}) AND {self._AGE_SQL} < datetime(?))"
                     f" OR (source NOT IN ({marks})"
                     f" AND {self._AGE_SQL} < datetime(?))")
            params: list = list(recheckable_sources)
            params.append(to_iso(now_hk() - timedelta(days=recheck_age_days)))
            params.extend(list(recheckable_sources))
            params.append(soft_cutoff)
        else:
            where = f"{self._AGE_SQL} < datetime(?)"
            params = [soft_cutoff]
        try:
            with self.conn:
                cur = self.conn.execute(
                    f"""UPDATE jobs SET reported = 1, status = 'stale'
                        WHERE reported = 0 AND status = 'new'
                          AND {self._AGE_SQL} IS NOT NULL
                          AND ({where})""",
                    params,
                )
                return max(0, cur.rowcount)
        except sqlite3.Error as exc:
            logger.error(f"归档超时岗位失败：{exc}")
            return 0

    def mark_closed(self, job_uids: list[str]) -> None:
        """把「已过期 / 不再接受申请」的岗位标记掉（推送前复核发现的那批）。"""
        if not job_uids:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    "UPDATE jobs SET reported = 1, status = 'expired' WHERE job_uid = ?",
                    [(uid,) for uid in job_uids],
                )
        except sqlite3.Error as exc:
            logger.error(f"标记已过期失败：{exc}")

    def suppress_from_report(self, job_uid: str) -> None:
        """标记「不必再进日报」，但**不动 status**。

        为什么不能直接用 `mark_reported`：它会把 status 改成 'reported'。
        补录已投记录时这条岗位的状态是 'applied'——用 mark_reported 会把
        「已投」冲成「已推送」，进度页的漏斗立刻少一条。

        为什么必须调用它：`get_unreported_jobs` 只看 `reported = 0`，**不看
        status**。补录进来的岗位没有 JD、没有分数，如果留着 reported=0，
        下一份日报会冒出一堆「未打分、而且你早就投过」的岗位，把真正
        待处理的机会挤下去。
        """
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET reported = 1 WHERE job_uid = ?", (job_uid,))
        except sqlite3.Error as exc:
            logger.error(f"标记免推送失败 {job_uid}：{exc}")

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
    # 工作台（P2 网页看板）
    # ------------------------------------------------------------------
    def board_jobs(self, statuses: tuple[str, ...] | None = None,
                   limit: int = 200, min_score: int = 0,
                   query: str = "", source: str = "",
                   sort: str = "default") -> list[JobPosting]:
        """取工作台要展示的岗位。

        与 `get_unreported_jobs` 的区别：那个是「还没推给用户」的队列，
        这里是**用户手动筛选**的视图——已投、已约面、已结束的岗位都要能回看，
        所以不按 reported 过滤。

        排序有两种（`sort` 参数）：
        - `default`：沿用日报口径，分数降序、未打分垫底、同分按发布日降序。
          想知道「最高分的岗位有哪些」时用它。
        - `smart`：**综合排序**，把新鲜度折进分数里。高分但已经挂了二十天的
          岗位会往后让，刚发的中高分岗位浮上来——找工作真正该先看的是后者。
        """
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if min_score > 0:
            sql += " AND COALESCE(llm_score, 0) >= ?"
            params.append(min_score)
        if query:
            sql += " AND (title LIKE ? OR company LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += _order_clause(sort)
        sql += " LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.error(f"读取工作台岗位失败：{exc}")
            return []
        return [self._row_to_job(r) for r in rows]

    def board_counts(self) -> dict[str, Any]:
        """各状态的岗位数（看板顶部的筛选标签用）。

        另外单独给出两项以下划线开头的元数据（前端据此渲染下拉框与标题栏，
        不会被当成状态标签）：
        - `_today`：今日新增条数。用户打开看板最先想知道的是「今天有什么新的」，
          而不是「一共存了多少条」。
        - `_sources`：来源 → 条数。用途是**只把库里真有的来源摆进下拉框**，
          而不是写死 jobsdb/indeed/linkedin/workday 四个——用户没配 LinkedIn
          时看到它只会困惑「为什么点了没东西」。
        """
        counts: dict[str, Any] = {}
        try:
            for row in self.conn.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
                counts[str(row["status"] or "new")] = int(row["n"])
            today = now_hk().strftime("%Y-%m-%d")
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE substr(first_seen_at,1,10) = ?",
                (today,)).fetchone()
            counts["_today"] = int(row["n"]) if row else 0
            counts["_sources"] = {
                str(r["source"]): int(r["n"])
                for r in self.conn.execute(
                    "SELECT source, COUNT(*) AS n FROM jobs"
                    " GROUP BY source ORDER BY n DESC")
                if r["source"]
            }
        except sqlite3.Error as exc:
            logger.error(f"统计工作台状态失败：{exc}")
        return counts

    def set_board_status(self, job_uid: str, status: str, note: str = "",
                         when: datetime | None = None) -> bool:
        """工作台按钮 → 改状态。返回是否真的改到了行（False＝这个 uid 不存在）。

        三处留痕：
        - `jobs.status`：当前状态，日报/统计都看它；
        - `status_history`：从哪到哪、什么时候。撤销与趋势图都靠它；
        - `user_feedback`（action = `board:<status>`）：保留更早一版就有的信号，
          不改掉以免历史数据断层。
        状态没变时不写历史：重复点同一个按钮不该在时间线上多一个点。

        `when` 是「事后补录」用的：从招聘网站同步已投记录时，真实投递日期可能
        在几十天前，一律用当前时间会把「近 7 天投递」全算到今天，进度页的图
        就骗人了。默认 None＝现在。
        """
        try:
            row = self.conn.execute(
                "SELECT status FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
            if row is None:
                return False
            old = str(row["status"] or "new")
            if old == status:
                return True
            now = to_iso(when or now_hk())
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET status = ? WHERE job_uid = ?", (status, job_uid))
                self.conn.execute(
                    "INSERT INTO status_history (job_uid, from_status, to_status,"
                    " note, created_at) VALUES (?,?,?,?,?)",
                    (job_uid, old, status, note[:200], now))
                self.conn.execute(
                    "INSERT INTO user_feedback (job_uid, action, created_at) VALUES (?,?,?)",
                    (job_uid, f"board:{status}", now),
                )
            return True
        except sqlite3.Error as exc:
            logger.error(f"工作台改状态失败 {job_uid}→{status}：{exc}")
            return False

    def undo_board_status(self, job_uid: str) -> str | None:
        """撤销这个岗位在工作台上最后做的一次状态变更，返回恢复后的状态。

        - 有历史行：恢复成那行的 `from_status`，然后**删掉这行**。
          删而不是标记，是为了让「连按两次撤销」变成两级回退而不是来回跳
          （标记式会让第二次撤销把状态又翻回去，用户会以为按钮坏了）。
        - 没有历史行（升级前点过按钮、或状态是抓取/Telegram 写的）：
          回退到 `new`。默认视图同时收 `new` 与 `reported`，所以它一定会
          重新出现在用户眼前——这正是他点撤销想要的结果。
        - uid 不存在：返回 None。
        """
        try:
            row = self.conn.execute(
                "SELECT id, from_status FROM status_history"
                " WHERE job_uid = ? ORDER BY id DESC LIMIT 1", (job_uid,)).fetchone()
            if row is not None:
                restored = str(row["from_status"] or "new")
                with self.conn:
                    self.conn.execute(
                        "UPDATE jobs SET status = ? WHERE job_uid = ?", (restored, job_uid))
                    self.conn.execute(
                        "DELETE FROM status_history WHERE id = ?", (int(row["id"]),))
                    self.conn.execute(
                        "INSERT INTO user_feedback (job_uid, action, created_at)"
                        " VALUES (?,?,?)", (job_uid, "board:undo", to_iso(now_hk())))
                return restored
            exists = self.conn.execute(
                "SELECT 1 FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
            if exists is None:
                return None
            with self.conn:
                self.conn.execute(
                    "UPDATE jobs SET status = 'new' WHERE job_uid = ?", (job_uid,))
                self.conn.execute(
                    "INSERT INTO user_feedback (job_uid, action, created_at) VALUES (?,?,?)",
                    (job_uid, "board:undo", to_iso(now_hk())))
            return "new"
        except sqlite3.Error as exc:
            logger.error(f"工作台撤销失败 {job_uid}：{exc}")
            return None

    def status_history_rows(self, limit: int = 1000) -> list[dict[str, Any]]:
        """最近的状态变更（新的在前），供导出「状态流水」用。

        撤销会删掉对应历史行，所以这里看到的就是**还没被撤销的**动作——
        与看板上的「我的进度」口径一致，不会出现「报表里有、进度页里没有」。
        """
        try:
            rows = self.conn.execute(
                "SELECT job_uid, from_status, to_status, note, created_at"
                " FROM status_history ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 5000)),)).fetchall()
        except sqlite3.Error as exc:
            logger.error(f"读取状态流水失败：{exc}")
            return []
        return [{
            "job_uid": str(r["job_uid"]),
            "from_status": str(r["from_status"] or ""),
            "to_status": str(r["to_status"] or ""),
            "note": str(r["note"] or ""),
            "created_at": str(r["created_at"] or ""),
        } for r in rows]

    # ------------------------------------------------------------------
    # 工作台数据页（P3 漏斗 / 趋势 / 分组对比）
    # ------------------------------------------------------------------
    def board_insights(self, days: int = 30) -> dict[str, Any]:
        """漏斗 + 近 N 天趋势 + 按来源分组。

        「投过没有」必须把 `status_history` 与当前状态合起来看：现在状态是
        `interview` 的岗位显然投过，但它当前状态里已经没有 `applied` 字样了；
        反过来只在历史里出现过的（撤销后）也不该继续算进漏斗。
        所以口径是：**当前状态属于投递后阶段，或历史里有对应记录**。
        """
        days = max(1, min(days, 120))
        today = now_hk().date()
        start = (today - timedelta(days=days - 1)).isoformat()
        out: dict[str, Any] = {"days": days, "funnel": {}, "daily": [],
                               "sources": [], "totals": {}}
        try:
            total = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])

            def count_any(statuses: tuple[str, ...]) -> int:
                marks = ",".join("?" * len(statuses))
                sql = (f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({marks})"
                       " OR job_uid IN (SELECT job_uid FROM status_history"
                       f" WHERE to_status IN ({marks}))")
                params = list(statuses) + list(statuses)
                return int(self.conn.execute(sql, params).fetchone()["n"])

            applied = count_any(("applied", "interview"))
            interview = count_any(("interview",))
            rejected = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = 'rejected'").fetchone()["n"])
            out["funnel"] = {
                "pool": total,          # 入库
                "applied": applied,     # 已投（含已约面）
                "interview": interview,  # 约面
                "rejected": rejected,   # 不感兴趣（不算「被拒」，是我们主动放弃）
            }

            # 近 N 天：入库靠 first_seen_at，投递/约面靠历史时间线
            new_by_day = {r["d"]: int(r["n"]) for r in self.conn.execute(
                "SELECT substr(first_seen_at,1,10) AS d, COUNT(*) AS n FROM jobs"
                " WHERE substr(first_seen_at,1,10) >= ? GROUP BY d", (start,))}
            act_by_day: dict[str, dict[str, int]] = {}
            for r in self.conn.execute(
                    "SELECT substr(created_at,1,10) AS d, to_status, COUNT(*) AS n"
                    " FROM status_history WHERE substr(created_at,1,10) >= ?"
                    " GROUP BY d, to_status", (start,)):
                act_by_day.setdefault(str(r["d"]), {})[str(r["to_status"])] = int(r["n"])
            daily = []
            for i in range(days):
                day = (today - timedelta(days=days - 1 - i)).isoformat()
                acts = act_by_day.get(day, {})
                daily.append({
                    "d": day,
                    "new": new_by_day.get(day, 0),
                    "applied": acts.get("applied", 0),
                    "interview": acts.get("interview", 0),
                })
            out["daily"] = daily

            rows = self.conn.execute(
                "SELECT source, COUNT(*) AS pool FROM jobs GROUP BY source"
                " ORDER BY pool DESC").fetchall()
            for r in rows:
                src = str(r["source"])
                applied_s = int(self.conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE source = ? AND (status IN"
                    " ('applied','interview') OR job_uid IN (SELECT job_uid FROM"
                    " status_history WHERE to_status = 'applied'))", (src,)).fetchone()["n"])
                interview_s = int(self.conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE source = ? AND (status ="
                    " 'interview' OR job_uid IN (SELECT job_uid FROM status_history"
                    " WHERE to_status = 'interview'))", (src,)).fetchone()["n"])
                out["sources"].append({
                    "source": src, "pool": int(r["pool"]),
                    "applied": applied_s, "interview": interview_s,
                    # 分母为 0 时留空，前端显示「—」，不要除出一个假 0%
                    "reply_rate": (round(interview_s / applied_s * 100, 1)
                                   if applied_s else None),
                })
            out["totals"] = {
                "applied_7d": sum(d["applied"] for d in daily[-7:]),
                "applied_30d": sum(d["applied"] for d in daily),
                "new_7d": sum(d["new"] for d in daily[-7:]),
            }
        except sqlite3.Error as exc:
            logger.error(f"统计工作台数据页失败：{exc}")
        return out

    # ------------------------------------------------------------------
    # 简历-JD 分析缓存（P4）
    # ------------------------------------------------------------------
    def save_analysis(self, job_uid: str, signature: str, model: str,
                      payload: dict[str, Any]) -> None:
        """写/覆盖一条分析结果。同一岗位只留最新一份（主键 job_uid）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO job_analysis (job_uid, signature, model, created_at, payload)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(job_uid) DO UPDATE SET signature=excluded.signature,"
                    " model=excluded.model, created_at=excluded.created_at,"
                    " payload=excluded.payload",
                    (job_uid, signature, model, to_iso(now_hk()),
                     json.dumps(payload, ensure_ascii=False)),
                )
        except sqlite3.Error as exc:
            logger.error(f"保存岗位分析失败 {job_uid}：{exc}")

    def get_analysis(self, job_uid: str, signature: str | None = None) -> dict[str, Any] | None:
        """读缓存。传了 `signature` 但库里不一致时返回 None（＝要重算）。

        为什么会不一致：用户改了简历，或换了模型。这时候旧结论不但没用，
        还会误导——照着旧简历的缺口清单去补，补的是上一版。
        """
        try:
            row = self.conn.execute(
                "SELECT signature, model, created_at, payload FROM job_analysis"
                " WHERE job_uid = ?", (job_uid,)).fetchone()
        except sqlite3.Error as exc:
            logger.error(f"读取岗位分析失败 {job_uid}：{exc}")
            return None
        if row is None:
            return None
        if signature is not None and str(row["signature"]) != signature:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        # 下划线开头是与 board_counts 一致的「元数据」约定，前端不会当业务字段渲染
        payload["_created_at"] = str(row["created_at"] or "")
        payload["_model"] = str(row["model"] or "")
        return payload

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
            # 防御：补列失败的老库（只读挂载等）这里取不到该列，按「无提示」处理
            eligibility_note=(row["eligibility_note"] or "")
            if "eligibility_note" in row.keys() else "",
            # 同样防御：取不到就现算一次，保证日报的去重标注不会因为
            # 一个没补上的列而整体失效
            dedupe_key=((row["dedupe_key"] or "") if "dedupe_key" in row.keys() else "")
            or dedupe_key_of(row["company"], row["title"]),
        )

    def close(self) -> None:
        self.conn.close()
