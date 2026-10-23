"""报告导出（P5）测试：三种格式 + 看板的下载接口 + 向导里的简历路径。

这份测试盯的四件事：

1. **转义**：职位名来自第三方网站，可能带 `<script>`。导出的 HTML 是让人
   双击打开的文件，一条恶意 JD 不能变成执行代码。
2. **空库不报错**：报表是快照，0 条就写 0 条；直接抛异常会让用户以为坏了。
3. **单格式失败不牵连**：PDF 打不出来时，HTML/Excel 仍要落地。
4. **看板下载要令牌**：`/api/export` 和页面同一套鉴权，没有例外。

兼容 pytest 与 `python tests/test_export.py`。
"""
from __future__ import annotations

import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar import export as export_mod              # noqa: E402
from hk_job_radar.db import Database                       # noqa: E402
from hk_job_radar.models import JobPosting                 # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _job(n: int, *, score: int | None = 75, status: str = "new",
         title: str = "Data Analyst", company: str = "ACME",
         source: str = "jobsdb", days_ago: int = 1) -> JobPosting:
    posted = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return JobPosting(
        source=source, external_id=str(n), title=title, company=company,
        url=f"https://example.com/{n}", url_normalized=f"https://example.com/{n}",
        posted_at=posted, description_snippet="SQL and Power BI.",
        llm_score=score, llm_reason="技能匹配", status=status,
        eligibility_note="[软] 粤语要求", first_seen_at=posted,
    )


class _TempDB:
    def __enter__(self) -> Database:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._dir.name) / "jobs.db"))
        self.db.init_schema()
        return self.db

    def __exit__(self, *exc) -> None:
        self.db.close()
        self._dir.cleanup()


class _TempDir:
    def __enter__(self) -> Path:
        self._dir = tempfile.TemporaryDirectory()
        return Path(self._dir.name)

    def __exit__(self, *exc) -> None:
        self._dir.cleanup()


def _seed(db: Database) -> None:
    db.upsert_job(_job(1, title="Data Analyst", company="HSBC", score=88))
    db.upsert_job(_job(2, title="Risk Analyst", company="DBS", source="indeed", score=64))
    db.upsert_job(_job(3, title="Old Role", company="X", status="rejected", score=40))
    db.set_board_status("jobsdb:1", "applied")
    db.set_board_status("jobsdb:1", "interview")
    db.set_board_status("indeed:2", "applied")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def test_html_escapes_third_party_text():
    """职位名里的尖括号必须转义，不能变成可执行标签。"""
    with _TempDB() as db:
        db.upsert_job(_job(9, title="<script>alert(1)</script>",
                           company="A & B <b>x</b>"))
        data = export_mod.collect(db)
        html = export_mod.render_html(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "A &amp; B" in html


def test_html_renders_empty_db_without_error():
    """空库也要能导出：0 就写 0，不抛异常。"""
    with _TempDB() as db:
        html = export_mod.render_html(export_mod.collect(db))
    assert "入库岗位" in html
    assert ">0<" in html
    assert "还没有点过「已投」" in html


def test_html_has_funnel_and_source_rows():
    with _TempDB() as db:
        _seed(db)
        html = export_mod.render_html(export_mod.collect(db))
    assert "<svg" in html, "漏斗应为内联 SVG"
    assert "jobsdb" in html and "indeed" in html
    # 不引外部资源：离线也要能看
    assert "<script src" not in html
    assert "http://" not in html.replace("http://127.0.0.1", "")


def test_html_marks_filled_funnel_stage():
    """投过 2 条时，漏斗第二层不该是 0。"""
    with _TempDB() as db:
        _seed(db)
        data = export_mod.collect(db)
    assert data["insights"]["funnel"]["applied"] == 2
    assert data["insights"]["funnel"]["interview"] == 1


def test_applied_table_only_has_touched_statuses():
    with _TempDB() as db:
        _seed(db)
        data = export_mod.collect(db)
    statuses = {r["status_raw"] for r in data["applied"]}
    assert statuses <= set(export_mod.TOUCHED_STATUSES)
    assert "jobsdb:1" in {r["uid"] for r in data["applied"]}


def test_status_labels_cover_board_actions():
    """看板能点出的状态，导出里都得有中文名（否则表里冒出英文原文）。"""
    from hk_job_radar.workbench import BOARD_ACTIONS, VIEW_FILTERS

    needed = set(BOARD_ACTIONS.values())
    for statuses in VIEW_FILTERS.values():
        needed |= set(statuses or ())
    missing = {s for s in needed if s not in export_mod.STATUS_LABEL}
    assert not missing, f"这些状态没有中文名：{missing}"


def test_events_carry_job_title():
    with _TempDB() as db:
        _seed(db)
        events = export_mod.status_events(db)
    assert events, "点过按钮就该有流水"
    assert any(e["title"] == "Data Analyst" for e in events)
    assert {e["to"] for e in events} >= {"已投", "约面"}


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def test_xlsx_has_four_sheets():
    if not export_mod.xlsx_available():
        print("（跳过：这台机器没装 openpyxl）")
        return
    from io import BytesIO

    from openpyxl import load_workbook

    with _TempDB() as db:
        _seed(db)
        blob = export_mod.build_xlsx(export_mod.collect(db))
    wb = load_workbook(BytesIO(blob))
    assert wb.sheetnames == ["概览", "岗位明细", "投递记录", "状态流水"]
    detail = wb["岗位明细"]
    assert [c.value for c in detail[1]][:3] == ["分数", "职位", "公司"]
    assert detail.freeze_panes == "A2"
    assert detail.auto_filter.ref, "明细表要能筛选"
    # 链接列是超链接，不是一坨文本
    urls = [c.hyperlink.target for row in detail.iter_rows(min_row=2)
            for c in row if c.hyperlink]
    assert urls and all(u.startswith("https://example.com/") for u in urls)


def test_xlsx_reply_rate_not_fake_zero():
    """分母为 0 时写「—」，不写 0%（那是两种完全不同的处境）。"""
    if not export_mod.xlsx_available():
        print("（跳过：这台机器没装 openpyxl）")
        return
    from io import BytesIO

    from openpyxl import load_workbook

    with _TempDB() as db:
        db.upsert_job(_job(1))            # 有岗位，但一条都没投
        wb = load_workbook(BytesIO(export_mod.build_xlsx(export_mod.collect(db))))
    ws = wb["概览"]
    rates = [ws.cell(row=r, column=5).value for r in range(1, ws.max_row + 1)]
    assert "—" in rates, "没有投递时回复率应是破折号"
    assert "0%" not in rates


# ---------------------------------------------------------------------------
# 导出入口
# ---------------------------------------------------------------------------
def test_export_writes_html_and_xlsx():
    with _TempDB() as db, _TempDir() as out:
        _seed(db)
        written, problems = export_mod.export(db, out, formats=("html", "xlsx"))
        names = sorted(p.name for p in written)
        assert names[0].endswith(".html") and names[1].endswith(".xlsx")
        assert not problems
        for path in written:            # 目录在 with 结束时就被删了，必须当场检查
            assert path.exists() and path.stat().st_size > 0


def test_export_html_survives_pdf_failure(monkeypatch=None):
    """PDF 打不出来时，HTML 必须还在——不该整次白跑。"""
    with _TempDB() as db, _TempDir() as out:
        _seed(db)
        original = export_mod.build_pdf

        def _boom(*_a, **_k):
            raise export_mod.ExportError("浏览器起不来")

        export_mod.build_pdf = _boom
        try:
            written, problems = export_mod.export(db, out, formats=("html", "pdf"))
        finally:
            export_mod.build_pdf = original
    suffixes = {p.suffix for p in written}
    assert ".html" in suffixes and ".pdf" not in suffixes
    assert any("浏览器起不来" in p for p in problems)


def test_export_xlsx_missing_library_is_a_note_not_a_crash():
    with _TempDB() as db, _TempDir() as out:
        _seed(db)
        original = export_mod.xlsx_available
        export_mod.xlsx_available = lambda: False
        try:
            written, problems = export_mod.export(db, out, formats=("html", "xlsx"))
        finally:
            export_mod.xlsx_available = original
    assert [p.suffix for p in written] == [".html"]
    assert any("openpyxl" in p for p in problems)


def test_export_raises_when_nothing_written():
    with _TempDB() as db, _TempDir() as out:
        original = export_mod.xlsx_available
        export_mod.xlsx_available = lambda: False
        try:
            export_mod.export(db, out, formats=("xlsx",))
        except export_mod.ExportError as exc:
            assert "openpyxl" in str(exc)
        else:                                          # pragma: no cover
            raise AssertionError("一个都没导出来时必须抛错")
        finally:
            export_mod.xlsx_available = original


def test_default_stem_is_ascii_and_dated():
    stem = export_mod.default_stem(datetime(2026, 10, 21, 9, 0))
    assert stem == "hk-job-radar-2026-10-21"
    assert stem.isascii(), "文件名保持 ASCII，避免压缩/邮件附件里乱码"


# ---------------------------------------------------------------------------
# 看板下载接口
# ---------------------------------------------------------------------------
class _Server:
    """起一个只绑本机的服务，用 urllib 真发请求（不 mock 传输层）。"""

    def __enter__(self):
        from hk_job_radar.workbench import _BoardServer, _Handler

        self._holder = _TempDB()          # 保留夹具本体，退出时才能正常清理
        self.db = self._holder.__enter__()
        self.srv = _BoardServer(("127.0.0.1", 0), _Handler, "tok-123", self.db)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self._holder.__exit__(*exc)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str):
        return urllib.request.urlopen(self.url(path), timeout=8)


def test_export_endpoint_requires_token():
    with _Server() as srv:
        _seed(srv.db)
        try:
            srv.get("/api/export?fmt=html").read()
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:                                          # pragma: no cover
            raise AssertionError("没令牌也能下载，说明鉴权漏了")


def test_export_endpoint_sends_attachment():
    with _Server() as srv:
        _seed(srv.db)
        with srv.get("/api/export?fmt=html&t=tok-123") as resp:
            assert resp.status == 200
            disposition = resp.headers.get("Content-Disposition", "")
            body = resp.read().decode("utf-8")
    assert "attachment" in disposition
    assert disposition.endswith('.html"')
    assert "香港岗位雷达" in body


def test_export_endpoint_rejects_unknown_format():
    with _Server() as srv:
        try:
            srv.get("/api/export?fmt=doc&t=tok-123").read()
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:                                          # pragma: no cover
            raise AssertionError("未知格式应当 400")


def test_ui_has_export_links():
    """页面上真有这两个下载入口，不是只在后端留着接口。"""
    from hk_job_radar.workbench import _load_ui

    html = _load_ui("t")
    assert 'id="expHtml"' in html and 'id="expXlsx"' in html
    assert "/api/export?fmt=html" in html


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
