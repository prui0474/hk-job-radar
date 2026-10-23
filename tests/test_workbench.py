"""岗位工作台（P2）测试：数据库视图 + 本地 HTTP 服务的鉴权。

看板是本项目里第一个「写」接口，所以测试重点不只是功能对不对，而是
**没令牌的人进不来**：
- 不带令牌 / 令牌不对 → 403，且不做「自动跳到带令牌地址」；
- Host 头不是本机 → 403（DNS rebinding）；
- 动作名不在白名单里 → 400，不能拿任意字符串去改 status。

兼容两种运行方式：pytest 与 `python tests/test_workbench.py`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.copilot.jobsdb_apply import (             # noqa: E402
    Answer,
    AssistReport,
    Question,
)
from hk_job_radar.db import Database, composite_score     # noqa: E402
from hk_job_radar.models import JobPosting, to_iso          # noqa: E402
from hk_job_radar.workbench import (                        # noqa: E402
    ACTION_UNDO,
    BOARD_ACTIONS,
    VIEW_FILTERS,
    _BoardServer,
    _Handler,
    _job_payload,
    _load_ui,
)

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
def _job(n: int, score: int | None = 70, status: str = "new",
         title: str = "Data Analyst", company: str = "ACME",
         days_ago: int = 1, source: str = "jobsdb") -> JobPosting:
    posted = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return JobPosting(
        source=source, external_id=str(n), title=title, company=company,
        url=f"https://example.com/{n}", url_normalized=f"https://example.com/{n}",
        posted_at=posted, description_snippet="SQL and Power BI.",
        llm_score=score, llm_reason="技能匹配", status=status,
        eligibility_note="", first_seen_at=posted,
    )


class _TempDB:
    """临时库（文件放临时目录，进程退出即弃）。"""

    def __enter__(self) -> Database:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "jobs.db"))
        self.db.init_schema()
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()
        self._dir.cleanup()


class _Server:
    """起一个真实的服务线程，用真实 HTTP 请求打它（不 mock 传输层）。"""

    def __init__(self, db: Database, token: str = "test-token-123",
                 cfg=None) -> None:
        self.server = _BoardServer(("127.0.0.1", 0), _Handler, token, db, cfg)
        self.token = token
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    # -- 请求helpers ----------------------------------------------------
    def url(self, path: str, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        base = f"http://127.0.0.1:{self.port}{path}"
        return f"{base}?{query}" if query else base

    def get(self, path: str, token: str | None = None, **params):
        req = urllib.request.Request(self.url(path, **params))
        if token:
            req.add_header("X-Board-Token", token)
        return urllib.request.urlopen(req, timeout=5)

    def get_status(self, path: str, token: str | None = None, **params) -> int:
        try:
            with self.get(path, token, **params) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def post(self, path: str, payload: dict, token: str | None = None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Board-Token", token)
        return urllib.request.urlopen(req, timeout=5)

    def post_json(self, path: str, payload: dict, token: str | None = None):
        """返回 (状态码, JSON)。4xx 也返回，不抛异常——鉴权失败是正常用例。"""
        try:
            with self.post(path, payload, token) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") or "{}"
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {}


# ---------------------------------------------------------------------------
# 数据库视图
# ---------------------------------------------------------------------------
def test_pending_view_groups_new_and_reported():
    """默认视图是「待处理」：new 与 reported 都是用户还没处理过的。"""
    assert VIEW_FILTERS["pending"] == ("new", "reported")
    with _TempDB() as db:
        db.upsert_job(_job(1, status="new"))
        db.upsert_job(_job(2, status="reported"))
        db.upsert_job(_job(3, status="applied"))
        got = {j.external_id for j in db.board_jobs(statuses=VIEW_FILTERS["pending"])}
    assert got == {"1", "2"}


def test_board_jobs_sorted_by_score_then_unscored_last():
    with _TempDB() as db:
        db.upsert_job(_job(1, score=62))
        db.upsert_job(_job(2, score=88))
        db.upsert_job(_job(3, score=None))
        scores = [j.llm_score for j in db.board_jobs()]
    assert scores == [88, 62, None], "未打分的必须垫底，否则第一屏全是没分岗位"


def test_board_jobs_search_matches_title_and_company():
    with _TempDB() as db:
        db.upsert_job(_job(1, title="Data Analyst", company="ACME"))
        db.upsert_job(_job(2, title="Risk Manager", company="Hang Seng"))
        assert len(db.board_jobs(query="data")) == 1
        assert len(db.board_jobs(query="hang seng")) == 1
        assert len(db.board_jobs(query="nope")) == 0


def test_board_jobs_min_score_filter():
    with _TempDB() as db:
        db.upsert_job(_job(1, score=88))
        db.upsert_job(_job(2, score=45))
        assert [j.external_id for j in db.board_jobs(min_score=60)] == ["1"]


def test_board_counts_and_today():
    with _TempDB() as db:
        # days_ago=0：三条都是今天入库的，才该算进「今日新增」
        db.upsert_job(_job(1, status="new", days_ago=0))
        db.upsert_job(_job(2, status="applied", days_ago=0))
        db.upsert_job(_job(3, status="applied", days_ago=0))
        counts = db.board_counts()
    assert counts["new"] == 1 and counts["applied"] == 2
    assert counts["_today"] == 3, "今天入库的三条都应算进今日新增"


def test_set_board_status_updates_row_and_logs_feedback():
    with _TempDB() as db:
        db.upsert_job(_job(1))
        assert db.set_board_status("jobsdb:1", "applied") is True
        assert db.get_job("jobsdb:1").status == "applied"
        rows = db.conn.execute(
            "SELECT action FROM user_feedback WHERE job_uid = ?", ("jobsdb:1",)).fetchall()
    assert [r["action"] for r in rows] == ["board:applied"], "动作要留档，便于以后算转化率"


def test_set_board_status_unknown_uid_returns_false():
    with _TempDB() as db:
        assert db.set_board_status("jobsdb:does-not-exist", "applied") is False


def test_undo_action_returns_job_to_pending():
    """撤销＝回到上一次的状态。没历史行（老数据）时退到 `new`，
    pending 视图同时收 new 与 reported，所以一定重新出现在默认视图里。"""
    with _TempDB() as db:
        db.upsert_job(_job(1))
        db.set_board_status("jobsdb:1", "interview")
        assert db.undo_board_status("jobsdb:1") == "new"
        assert db.get_job("jobsdb:1").status == "new"
        pending = {j.job_uid for j in db.board_jobs(statuses=VIEW_FILTERS["pending"])}
    assert pending == {"jobsdb:1"}


def test_status_history_restores_exact_previous_status():
    """从 `reported` 点成 `applied`，撤销要回 `reported` 而不是一律 `new`。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, status="reported"))
        db.set_board_status("jobsdb:1", "applied")
        rows = db.conn.execute(
            "SELECT from_status, to_status FROM status_history WHERE job_uid = ?",
            ("jobsdb:1",)).fetchall()
        assert [(r["from_status"], r["to_status"]) for r in rows] == [("reported", "applied")]
        assert db.undo_board_status("jobsdb:1") == "reported"
        assert db.get_job("jobsdb:1").status == "reported"


def test_undo_twice_walks_back_one_step_at_a_time():
    """连按两次撤销是两级回退，不是来回跳（历史行用过就删）。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, status="reported"))
        db.set_board_status("jobsdb:1", "applied")
        db.set_board_status("jobsdb:1", "interview")
        assert db.undo_board_status("jobsdb:1") == "applied"
        assert db.undo_board_status("jobsdb:1") == "reported"
        # 没历史了，再撤就是兜底到 new —— 不能把状态又翻回 interview
        assert db.undo_board_status("jobsdb:1") == "new"
        assert db.get_job("jobsdb:1").status == "new"


def test_same_status_click_does_not_pollute_history():
    with _TempDB() as db:
        db.upsert_job(_job(1, status="applied"))
        assert db.set_board_status("jobsdb:1", "applied") is True
        assert db.conn.execute(
            "SELECT COUNT(*) AS n FROM status_history").fetchone()["n"] == 0


def test_undo_unknown_uid_returns_none():
    with _TempDB() as db:
        assert db.undo_board_status("jobsdb:nope") is None


def test_board_jobs_source_filter():
    with _TempDB() as db:
        db.upsert_job(_job(1, source="jobsdb"))
        db.upsert_job(_job(2, source="indeed"))
        db.upsert_job(_job(3, source="workday"))
        assert [j.source for j in db.board_jobs(source="indeed")] == ["indeed"]
        assert len(db.board_jobs(source="")) == 3, "空来源＝不筛选"
        # 来源与分数、视图叠加时是 AND 关系
        db.set_board_status("indeed:2", "applied")
        assert db.board_jobs(source="indeed", statuses=("applied",))[0].external_id == "2"


# ---------------------------------------------------------------------------
# 综合排序（分数 + 新鲜度）
# ---------------------------------------------------------------------------
def test_board_jobs_smart_sort_favors_fresh():
    """88 分挂了 19 天 vs 82 分刚发：综合排序要把新的那条提到前面。

    「高分但快过期」的岗位不该一直占着第一屏——这是这个排序存在的理由。
    """
    with _TempDB() as db:
        db.upsert_job(_job(1, score=88, days_ago=19))
        db.upsert_job(_job(2, score=82, days_ago=1))
        db.upsert_job(_job(3, score=60, days_ago=0))
        default_order = [j.external_id for j in db.board_jobs()]
        smart_order = [j.external_id for j in db.board_jobs(sort="smart")]
    assert default_order == ["1", "2", "3"], "默认口径＝纯分数降序"
    assert smart_order == ["2", "1", "3"], "综合排序要按「分数×新鲜度」"


def test_board_jobs_smart_sort_still_prefers_score_when_age_close():
    """年龄差不多时分数高的仍在前——综合不是「只按时间」。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, score=70, days_ago=2))
        db.upsert_job(_job(2, score=88, days_ago=3))
        order = [j.external_id for j in db.board_jobs(sort="smart")]
    assert order == ["2", "1"]


def test_board_jobs_smart_sort_keeps_unscored_last():
    with _TempDB() as db:
        db.upsert_job(_job(1, score=None, days_ago=0))
        db.upsert_job(_job(2, score=50, days_ago=25))
        order = [j.external_id for j in db.board_jobs(sort="smart")]
    assert order == ["2", "1"], "没评分的不该因为「今天刚抓到」就排到最前"


def test_board_jobs_unknown_sort_falls_back_to_default():
    """排序值要拼进 SQL，白名单外的一律当默认，不能留任何注入空间。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, score=88, days_ago=19))
        db.upsert_job(_job(2, score=82, days_ago=1))
        messy = db.board_jobs(sort="score DESC; DROP TABLE jobs")
        assert [j.external_id for j in messy] == ["1", "2"]
        assert db.board_jobs(sort="")[0].external_id == "1"


def test_composite_score_matches_the_documented_formula():
    """卡片上显示的综合分必须和 SQL 排序用的公式一致。

    0.4 / 30 这两个常量两边共用，所以只要这里对得上，界面上的数字
    就能解释「为什么这一条排在上面」，不会出现「显示 80 分却排在
    显示 79 分的下面」这种自相矛盾的画面。
    """
    assert composite_score(88, 0) == 88, "今天发的：原分"
    assert composite_score(88, 15) == 70.4, "15 天前：八折"
    assert composite_score(88, 30) == 52.8, "30 天：六折"
    assert composite_score(88, 90) == 52.8, "再老也不再掉，六折是下限"
    assert composite_score(88, -5) == 88, "未来时间（站点写错）按今天算"
    assert composite_score(None, 3) is None, "没打分就没有综合分"


def test_job_payload_carries_smart_score():
    """接口要把综合分发给前端——否则卡片没法解释自己的位置。"""
    job = _job(1, score=88, days_ago=10)
    payload = _job_payload(job)
    assert payload["smart"] is not None
    # 10 天 → 1 - 0.4×10/30 = 0.8667 → 76.3 分左右
    assert 75 <= payload["smart"] <= 77, payload["smart"]
    assert payload["age_days"] == 10

    unscored = _job_payload(_job(2, score=None, days_ago=10))
    assert unscored["smart"] is None, "未打分的岗位不给综合分"


def test_ui_shows_smart_score_only_in_smart_mode():
    """分数旁边那颗「综合 xx」的胶囊只在综合排序下出现。

    分数优先模式下卡片位置跟综合分无关，多印一个数字只会让人问
    「这两个分数哪个是真的」。
    """
    ui = (Path(__file__).resolve().parents[1] / "src/hk_job_radar/workbench_ui.html"
          ).read_text(encoding="utf-8")
    assert 'state.sort === "smart" && j.smart != null' in ui
    assert 'class="chip smart"' in ui
    assert ".chip.smart{" in ui


def test_age_label_says_recorded_not_posted_when_date_is_unknown():
    """不知道发布日就不能说「发布」。

    补录进来的岗位只有投递日（站点列表页不给发布时间），标成
    「23 天前发布」等于替它编了一个发布日期——用户点进原链接发现
    是两个月前发的，就不再信这个日期了。
    """
    ui = (Path(__file__).resolve().parents[1] / "src/hk_job_radar/workbench_ui.html"
          ).read_text(encoding="utf-8")
    assert 'j.age_from === "posted" ? "发布" : "收录"' in ui

    known = _job_payload(_job(1, days_ago=5))
    assert known["age_from"] == "posted"

    # 没有发布时间的岗位（补录的就是这种）要报 "seen"
    job = _job(2, days_ago=5)
    job.posted_at = None
    unknown = _job_payload(job)
    assert unknown["age_from"] == "seen"
    assert unknown["age_days"] == 5, "年龄本身照样按 first_seen_at 算"


def test_board_counts_reports_sources():
    """`_sources` 让前端只列出库里真有的站点，避免点进空的来源。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, source="jobsdb"))
        db.upsert_job(_job(2, source="jobsdb"))
        db.upsert_job(_job(3, source="indeed"))
        counts = db.board_counts()
    assert counts["_sources"] == {"jobsdb": 2, "indeed": 1}
    assert counts["_sources"] != counts.get("new"), "元数据不该混进状态计数"


# ---------------------------------------------------------------------------
# HTTP 鉴权
# ---------------------------------------------------------------------------
def test_page_requires_token():
    with _TempDB() as db, _Server(db) as srv:
        assert srv.get_status("/") == 403
        assert srv.get_status("/", token="wrong") == 403
        assert srv.get_status("/", token=srv.token) == 200


def test_api_requires_token():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        assert srv.get_status("/api/jobs") == 403
        assert srv.get_status("/api/summary") == 403
        assert srv.get_status("/api/jobs", token=srv.token) == 200


def test_forbidden_response_does_not_leak_token():
    """403 页面不许出现正确令牌——否则「拒绝」等于把钥匙递出去。"""
    with _TempDB() as db, _Server(db) as srv:
        try:
            with srv.get("/") as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
    assert srv.token not in body
    assert "控制台" in body, "要告诉用户怎么办，而不是只回一个 403"


def test_foreign_host_header_is_rejected():
    """DNS rebinding 防护：Host 头不是本机就拒绝，哪怕令牌是对的。"""
    with _TempDB() as db, _Server(db) as srv:
        req = urllib.request.Request(srv.url("/api/summary"))
        req.add_header("X-Board-Token", srv.token)
        req.add_header("Host", "evil.example.com")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
    assert code == 403


# ---------------------------------------------------------------------------
# HTTP 业务
# ---------------------------------------------------------------------------
def test_jobs_endpoint_returns_payload():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, score=88, title="Senior Data Analyst"))
        with srv.get("/api/jobs", token=srv.token, view="all") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert data["ok"] is True
    job = data["jobs"][0]
    assert job["title"] == "Senior Data Analyst"
    assert job["score"] == 88
    assert job["uid"] == "jobsdb:1"
    assert job["url"].startswith("https://")


def test_jobs_endpoint_respects_view_filter():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, status="new"))
        db.upsert_job(_job(2, status="applied"))
        with srv.get("/api/jobs", token=srv.token, view="applied") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert [j["uid"] for j in data["jobs"]] == ["jobsdb:2"]


def test_duplicate_count_marks_same_job_across_listings():
    """同一个岗位挂了两条时，卡片要标出「另有 1 条」，但不合并成一条。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, title="MT Programme", company="Hang Seng Bank"))
        db.upsert_job(_job(2, title="MT Programme", company="Hang Seng Bank Ltd"))
        db.upsert_job(_job(3, title="Data Analyst", company="ACME"))
        with srv.get("/api/jobs", token=srv.token, view="all") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    dups = {j["title"]: j["dup"] for j in data["jobs"]}
    assert dups["MT Programme"] == 1, "Ltd 后缀应被规范化掉，认成同一个岗位"
    assert dups["Data Analyst"] == 0
    assert len(data["jobs"]) == 3, "标记重复不等于删除卡片"
    assert all("key" not in j for j in data["jobs"]), "指纹不该回传给前端"


def test_jobs_endpoint_source_and_score_filters():
    """两个下拉框走的是查询参数，必须真的落到 SQL 上（前端传了后端不该无视）。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, score=88, source="jobsdb"))
        db.upsert_job(_job(2, score=88, source="indeed"))
        db.upsert_job(_job(3, score=45, source="indeed"))
        with srv.get("/api/jobs", token=srv.token, view="all", source="indeed") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert {j["uid"] for j in data["jobs"]} == {"indeed:2", "indeed:3"}
        with srv.get("/api/jobs", token=srv.token, view="all",
                     source="indeed", min_score=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert [j["uid"] for j in data["jobs"]] == ["indeed:2"]


def test_summary_exposes_sources():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, source="jobsdb"))
        db.upsert_job(_job(2, source="workday"))
        with srv.get("/api/summary", token=srv.token) as resp:
            counts = json.loads(resp.read().decode("utf-8"))["counts"]
    assert counts["_sources"] == {"jobsdb": 1, "workday": 1}


def test_post_undo_restores_previous_status():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, status="reported"))
        srv.post_json("/api/status", {"uid": "jobsdb:1", "action": "applied"}, srv.token)
        code, data = srv.post_json("/api/status",
                                   {"uid": "jobsdb:1", "action": ACTION_UNDO}, srv.token)
        assert code == 200 and data["status"] == "reported"
        assert db.get_job("jobsdb:1").status == "reported"
        with srv.get("/api/jobs", token=srv.token, view="applied") as resp:
            assert json.loads(resp.read().decode("utf-8"))["jobs"] == []


def test_post_undo_unknown_job_is_404():
    with _TempDB() as db, _Server(db) as srv:
        code, data = srv.post_json("/api/status",
                                   {"uid": "jobsdb:nope", "action": ACTION_UNDO}, srv.token)
    assert code == 404 and data["ok"] is False


def test_unknown_view_falls_back_to_all():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, status="new"))
        db.upsert_job(_job(2, status="applied"))
        with srv.get("/api/jobs", token=srv.token, view="whatever") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert len(data["jobs"]) == 2


def test_post_status_applies_action():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        code, data = srv.post_json("/api/status",
                                   {"uid": "jobsdb:1", "action": "interview"}, srv.token)
        assert code == 200 and data["ok"] is True
        assert db.get_job("jobsdb:1").status == "interview"


def test_post_status_rejects_unknown_action():
    """动作名必须来自白名单：不能让前端随便传字符串去写 status。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        code, data = srv.post_json("/api/status",
                                   {"uid": "jobsdb:1", "action": "deleted"}, srv.token)
        assert code == 400 and data["ok"] is False
        assert db.get_job("jobsdb:1").status == "new", "非法动作不能改到数据"


def test_post_status_without_token_is_rejected():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        code, _ = srv.post_json("/api/status", {"uid": "jobsdb:1", "action": "applied"})
        assert code == 403
        assert db.get_job("jobsdb:1").status == "new"


def test_post_status_missing_fields_is_bad_request():
    with _TempDB() as db, _Server(db) as srv:
        assert srv.post_json("/api/status", {"uid": "", "action": "applied"}, srv.token)[0] == 400


def test_four_buttons_map_to_expected_statuses():
    """四个按钮的落库值，改动时要先想清楚历史数据会不会错位。
    撤销不在这张表里：它靠状态历史推出来，不是一个固定状态。"""
    assert BOARD_ACTIONS == {
        "applied": "applied",
        "interview": "interview",
        "closed": "closed",
        "rejected": "rejected",
    }
    assert ACTION_UNDO not in BOARD_ACTIONS


# ---------------------------------------------------------------------------
# 进度面板（P3）：漏斗 / 趋势 / 分组
# ---------------------------------------------------------------------------
def test_insights_funnel_counts_union_of_status_and_history():
    """现在状态是 `interview` 的岗位显然投过，漏斗的「已投」不能漏掉它。"""
    with _TempDB() as db:
        for i in range(1, 6):
            db.upsert_job(_job(i))
        db.set_board_status("jobsdb:1", "applied")
        db.set_board_status("jobsdb:2", "interview")      # 投→约面，状态里没有 applied 了
        db.set_board_status("jobsdb:3", "rejected")       # 不感兴趣不算投递
        f = db.board_insights()["funnel"]
    assert f["pool"] == 5
    assert f["applied"] == 2, "applied + interview 都应算进已投"
    assert f["interview"] == 1
    assert f["rejected"] == 1


def test_insights_daily_series_has_one_row_per_day():
    with _TempDB() as db:
        db.upsert_job(_job(1, days_ago=0))
        db.upsert_job(_job(2, days_ago=3))
        data = db.board_insights(days=7)
    assert len(data["daily"]) == 7, "没有数据的日子也要占位，否则柱状图会错位"
    assert data["daily"][-1]["new"] == 1
    assert data["daily"][-4]["new"] == 1
    assert data["totals"]["new_7d"] == 2


def test_insights_source_reply_rate_is_none_without_applications():
    """一条都没投时回复率必须是 None（前端显示「—」），不能除出一个假 0%。"""
    with _TempDB() as db:
        db.upsert_job(_job(1, source="jobsdb"))
        db.upsert_job(_job(2, source="indeed"))
        rows = {r["source"]: r for r in db.board_insights()["sources"]}
    assert rows["jobsdb"]["applied"] == 0
    assert rows["jobsdb"]["reply_rate"] is None


def test_insights_clamps_days():
    with _TempDB() as db:
        assert len(db.board_insights(days=0)["daily"]) == 1
        assert len(db.board_insights(days=999)["daily"]) == 120


def test_insights_endpoint_requires_token():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        assert srv.get_status("/api/insights") == 403
        assert srv.get_status("/api/insights", token=srv.token) == 200


def test_insights_endpoint_returns_funnel_and_daily():
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        db.set_board_status("jobsdb:1", "applied")
        with srv.get("/api/insights", token=srv.token, days=14) as resp:
            data = json.loads(resp.read().decode("utf-8"))["insights"]
    assert data["funnel"]["applied"] == 1
    assert len(data["daily"]) == 14
    assert data["totals"]["applied_7d"] == 1


def test_ui_page_injects_token_and_has_four_buttons():
    html = _load_ui("abc-token")
    assert "abc-token" in html
    assert "__BOARD_TOKEN__" not in html
    for label in ("已投", "约面", "结束", "不感兴趣"):
        assert label in html, f"页面缺少按钮：{label}"


def test_ui_has_undo_button_and_filter_controls():
    """撤销按钮与两个下拉框是页面上真实存在的元素，不是只写在后端。"""
    html = _load_ui("t")
    assert 'data-act="undo"' in html and "撤销" in html
    assert 'id="src"' in html, "缺少来源下拉框"
    assert 'id="score"' in html, "缺少分数下拉框"
    # 撤销只挂在「点过按钮」的卡片上：待处理的卡片不该出现它
    assert "UNTOUCHED" in html


def test_ui_has_insights_panel():
    """P3 的进度面板：图表容器存在，且不引用任何外部脚本（离线要能看）。"""
    html = _load_ui("t")
    for el in ('id="insights"', 'id="funnel"', 'id="daily"', 'id="bySource"'):
        assert el in html, f"缺少 {el}"
    assert "<script src" not in html, "不许引外部脚本/CDN"
    assert "http://" not in html.replace("http://127.0.0.1", ""), "不许引外部资源"


# ---------------------------------------------------------------------------
# 容易被误读的地方（真实用户报过）
# ---------------------------------------------------------------------------
def test_ui_separates_status_badge_from_action_buttons():
    """确认状态徽章和操作按钮在页面上是两回事。

    真实困惑：待处理列表里所有卡片底部第一个按钮是蓝底「已投」，看着就像
    「这条已经是已投了」。现在状态徽章每张卡片都显示，按钮那排前面加了
    「标记为」，两者不会再被看成同一个东西。
    """
    html = _load_ui("t")
    assert "标记为" in html, "操作按钮前要有「标记为」，否则按钮读起来像状态"
    assert "statusChipOf" in html
    # 「待处理」也必须出徽章：只在非待处理时显示，正是这次误读的根源
    assert "ST_CHIP" in html and "plain" in html, "状态徽章缺了中性配色"


def test_stale_detector_flags_newer_file():
    """改完代码但没重启 → 必须报出文件名，否则用户会以为新功能坏了。

    页面每次刷新都重读磁盘，Python 代码却停在启动那一刻；这是真实撞过的坑
    （新加的分数筛选被旧进程无视）。
    """
    import os
    import time

    from hk_job_radar import workbench as wb

    ui = wb.UI_PATH
    assert wb._outdated_source() == "", "刚导入的进程不该被判成旧版"
    original = ui.stat().st_mtime
    try:
        # 设成「绝对未来」而不是 original+60：文件可能是前几天写的，
        # 加 60 秒仍然早于本进程的导入时刻，测试就会假通过/假失败。
        future = time.time() + 60
        os.utime(ui, (future, future))       # 假装文件刚被改过
        assert wb._outdated_source() == "workbench_ui.html"
    finally:
        os.utime(ui, (original, original))   # 一定还原，别留未来的 mtime
    assert wb._outdated_source() == "", "还原 mtime 后要立刻恢复"


def test_summary_exposes_stale_flag():
    """前端靠 `/api/summary` 的 `stale` 字段决定要不要弹黄条。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        with srv.get("/api/summary", token=srv.token) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert data["stale"] == "", "正常启动时不该提示重启"
    assert "counts" in data, "原有字段不能被顶掉"


def test_jobs_endpoint_smart_sort():
    """排序下拉走 `sort` 查询参数，必须真的落到 SQL 上。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1, score=88, days_ago=19))
        db.upsert_job(_job(2, score=82, days_ago=1))
        with srv.get("/api/jobs", token=srv.token, view="all",
                     sort="smart") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert [j["uid"] for j in data["jobs"]] == ["jobsdb:2", "jobsdb:1"]


def test_ui_has_sort_control():
    """排序下拉是页面上真实存在的元素，且会把选择带到请求里。"""
    html = _load_ui("t")
    assert 'id="sort"' in html
    for label in ("分数优先", "综合排序"):
        assert label in html, f"缺少排序选项：{label}"
    assert 'params.set("sort"' in html


def test_ui_has_sync_button():
    """「同步已投」是页面上的按钮，不是只写在文档里。"""
    html = _load_ui("t")
    assert 'id="syncBtn"' in html and "同步已投" in html
    assert 'id="syncNote"' in html, "同步结果要有个地方显示，不能只弹 toast"


def test_sync_applied_without_config_is_rejected():
    """没加载配置时不能去起浏览器——直接说清楚，别让用户等一分钟看到报错。"""
    with _TempDB() as db, _Server(db) as srv:      # 不传 cfg
        code, data = srv.post_json("/api/sync-applied", {}, srv.token)
    assert code == 400
    assert "配置" in data["error"]


# ---------------------------------------------------------------------------
# 投递副驾（P3）
# ---------------------------------------------------------------------------
class _FakeWorker:
    """替身：不碰浏览器，只把「填了什么」原样报回去。

    真正点页面的事由 `tests/test_jobsdb_apply.py` 和一次人工跑
    `run.py apply` 保证；这里验证的是接口这一层。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, job, form_answers, *, dry_run=False, timeout_s=180.0):
        self.calls.append((job.job_uid, dict(form_answers), dry_run))
        report = AssistReport(onsite=True)
        report.answers.append(Answer(
            Question("How many years' experience?", "select",
                     ("Less than 1 year",), ""),
            "Less than 1 year", why="统一口径"))
        return report


def test_apply_without_config_is_rejected():
    """没加载配置就没法读资料库口径，不能空着口径去猜。"""
    with _TempDB() as db, _Server(db) as srv:
        db.upsert_job(_job(1))
        code, data = srv.post_json("/api/apply", {"uid": "jobsdb:1"}, srv.token)
    assert code == 400
    assert "配置" in data["error"]


def test_apply_refuses_to_fill_broken_source():
    """★ 非 JobsDB 的岗位不给填。

    跳去雇主 ATS 的申请页结构完全不同，硬填等于把不相干的内容塞进别人的
    表单——比不填糟得多。这里必须明确拒绝，并指向「打开原链接」。
    """
    with _TempDB() as db, _Server(db, cfg=object()) as srv:
        db.upsert_job(_job(1, source="linkedin"))
        code, data = srv.post_json("/api/apply", {"uid": "linkedin:1"}, srv.token)
    assert code == 400
    assert "原链接" in data["error"]


def test_apply_returns_report_and_never_touches_status():
    """填完只在页面上报结果——**不替用户标已投**。

    用户可能核对后发现不合适、决定不投；替他标上等于伪造投递记录。
    """
    with _TempDB() as db, _Server(db, cfg=object()) as srv:
        db.upsert_job(_job(1))
        fake = _FakeWorker()
        srv.server._apply_worker = fake
        before = db.get_job("jobsdb:1").status
        code, data = srv.post_json("/api/apply", {"uid": "jobsdb:1"}, srv.token)
        after = db.get_job("jobsdb:1").status
    assert code == 200 and data["ok"]
    assert data["filled"] == 1 and data["changed"] == 0
    assert "提交" in data["summary"], "报告必须把提交这一步交还给人"
    assert before == after == "new"
    assert fake.calls and fake.calls[0][0] == "jobsdb:1"


def test_ui_has_apply_button_but_only_for_jobsdb():
    """「投递」按钮存在，且用来源做了门槛——别的站不显示它。"""
    html = _load_ui("t")
    assert 'data-do="apply"' in html
    assert 'j.source === "jobsdb"' in html
    assert 'class="assist"' in html, "填完之后要有个地方显示「填了什么」"


def test_apply_worker_passes_errors_back_to_the_caller():
    """后台线程里出的错必须回传给 HTTP 线程，否则页面会一直转圈。"""
    from hk_job_radar.workbench import _ApplyWorker

    worker = _ApplyWorker(cfg=None)
    worker._fill = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("卡在验证页"))
    try:
        worker.submit(object(), {})
    except RuntimeError as exc:
        assert "验证页" in str(exc)
    else:
        raise AssertionError("填表出错时必须抛出来，不能静默返回")


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
