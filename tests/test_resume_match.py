"""简历解析 + 简历-JD 分析（P4）测试。

三块，按「最容易悄悄坏掉」的顺序排：

1. **简历解析**：.docx 是个 zip，解析失败时用户看到的必须是一句人话，
   而不是 `KeyError: 'word/document.xml'`。这里连「段落之间要保留空行」
   都钉住了——丢了空行，教育经历和工作经历会连成一片，模型会把时间线读串。
2. **模型输出的容错**：模型不保证只吐 JSON（会包代码块、会加开场白），
   所以 `_extract_json` 要能兜住；`_normalize` 要把结构压成前端能直接渲染的
   固定形状——少一个键不能让页面崩。
3. **缓存与接口**：同一岗位不重复调模型（省的是真钱）；简历改了要让旧结论
   失效；没配简历时要告诉用户「改哪一行」，而不是回一个 500。

兼容两种运行方式：pytest 与 `python tests/test_resume_match.py`。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.config import AppConfig                       # noqa: E402
from hk_job_radar.db import Database                            # noqa: E402
from hk_job_radar.matcher import (                              # noqa: E402
    MatchError,
    ResumeMatcher,
    _extract_json,
    _normalize,
)
from hk_job_radar.models import JobPosting                      # noqa: E402
from hk_job_radar.resume import (                               # noqa: E402
    ResumeError,
    load_resume_text,
)
from hk_job_radar.workbench import _BoardServer, _Handler        # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_docx(path: Path, parts: list[str]) -> Path:
    """拼一个最小可用的 .docx（只有正文 xml，够解析用）。

    `parts` 里每项是一段，段内可用 `<TAB/>` / `<BR/>` 占位符模拟分栏与换行
    ——「学历 —— 时间」这类并排信息全靠制表符，丢了就黏成一坨。
    """
    body = ""
    for para in parts:
        if not para:
            body += "<w:p/>"          # 空段落：解析后应当变成段落之间的空行
            continue
        runs = ""
        for token in re.split(r"(<TAB/>|<BR/>)", para):
            if token == "<TAB/>":
                runs += "<w:r><w:tab/></w:r>"
            elif token == "<BR/>":
                runs += "<w:r><w:br/></w:r>"
            elif token:
                runs += f'<w:r><w:t xml:space="preserve">{token}</w:t></w:r>'
        body += f"<w:p>{runs}</w:p>"
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return path


def _job(n: str = "1", title: str = "Data Analyst") -> JobPosting:
    now = datetime.now(timezone.utc)
    return JobPosting(
        source="jobsdb", external_id=n, title=title, company="ACME",
        url=f"https://example.com/{n}", url_normalized=f"https://example.com/{n}",
        posted_at=now, description_snippet="Required: SQL, Power BI, Python.",
        llm_score=80, llm_reason="", status="new", first_seen_at=now,
    )


class _TempDir:
    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name)
        return self.path

    def __exit__(self, *exc):
        self._td.cleanup()


# ---------------------------------------------------------------------------
# 1. 简历解析
# ---------------------------------------------------------------------------
def test_docx_paragraphs_and_tabs():
    with _TempDir() as d:
        p = _make_docx(d / "cv.docx", [
            "ZHOU RUI",
            "City University of Hong Kong<TAB/>MSc in Data Science<TAB/>Sep 2026",
            "",
            "PROJECTS",
            "HK Job Radar<BR/>Python, SQLite",
        ])
        text = load_resume_text(p)
    assert "ZHOU RUI" in text
    assert "MSc in Data Science\tSep 2026" in text, "分栏之间的制表符不能被吃掉"
    assert "HK Job Radar" in text and "Python, SQLite" in text, "行内换行不能黏成一行"
    assert "\n\n" in text, "段落之间的空行要保留，否则模型会把两段读成一段"


def test_txt_reads_utf8_and_gbk():
    with _TempDir() as d:
        utf8 = d / "a.txt"
        utf8.write_text("张三 · 数据分析\n", encoding="utf-8")
        assert "张三" in load_resume_text(utf8)

        gbk = d / "b.txt"
        gbk.write_bytes("李四 · 量化研究\n".encode("gb18030"))
        assert "李四" in load_resume_text(gbk), "国内老简历常是 GBK，要能兜住"


def test_missing_file_message_is_actionable():
    with _TempDir() as d:
        try:
            load_resume_text(d / "nope.docx")
        except ResumeError as exc:
            assert "找不到" in str(exc)
        else:
            raise AssertionError("找不到文件时必须抛 ResumeError")


def test_old_doc_format_tells_user_how_to_convert():
    with _TempDir() as d:
        p = d / "cv.doc"
        p.write_bytes(b"\xd0\xcf\x11\xe0")     # OLE2 头
        try:
            load_resume_text(p)
        except ResumeError as exc:
            assert "另存为" in str(exc) and ".docx" in str(exc)
        else:
            raise AssertionError(".doc 应该给出中文提示而不是崩掉")


def test_pdf_without_parser_suggests_conversion():
    """没装 pypdf 时给「另存为 docx」的路；装了就直接读，两种情况都不该崩。"""
    with _TempDir() as d:
        p = d / "cv.pdf"
        p.write_bytes(b"%PDF-1.4\n%fake\n")
        try:
            load_resume_text(p)
        except ResumeError as exc:
            assert "另存为" in str(exc) or "pypdf" in str(exc)


def test_empty_docx_is_rejected():
    with _TempDir() as d:
        p = _make_docx(d / "empty.docx", [])
        try:
            load_resume_text(p)
        except ResumeError as exc:
            assert "没读到任何文字" in str(exc)
        else:
            raise AssertionError("空简历必须报错，否则会拿空文本去调模型")


def test_broken_docx_is_rejected():
    with _TempDir() as d:
        p = d / "broken.docx"
        p.write_bytes(b"not a zip at all")
        try:
            load_resume_text(p)
        except ResumeError as exc:
            assert "打不开" in str(exc)
        else:
            raise AssertionError("坏文件必须变成中文提示")


def test_long_resume_is_truncated():
    with _TempDir() as d:
        p = d / "long.txt"
        p.write_text("A" * 500, encoding="utf-8")
        assert len(load_resume_text(p, limit=100)) == 100


# ---------------------------------------------------------------------------
# 2. 模型输出解析与规整
# ---------------------------------------------------------------------------
def test_extract_json_tolerates_wrapping():
    want = {"match_score": 70}
    variants = [
        '{"match_score": 70}',
        '```json\n{"match_score": 70}\n```',
        '好的，以下是分析：\n```\n{"match_score": 70}\n```\n希望有帮助。',
        '{"match_score": 70}\n\n（以上为分析结果）',
    ]
    for raw in variants:
        assert _extract_json(raw) == want, f"没能解析：{raw!r}"


def test_extract_json_returns_none_for_garbage():
    for raw in ("", "模型说要加点上下文", "[1, 2, 3]", "{坏 JSON"):
        assert _extract_json(raw) is None


def test_normalize_clamps_and_caps():
    data = {
        "match_score": 180,                      # 超范围 → 夹到 100
        "summary": "x" * 200,                    # 超长 → 截断
        "matched": [f"技能{i}" for i in range(20)],   # 超量 → 只留 8 条
        "gaps": [{"skill": "SAS", "why": "w", "how": "h", "extra": "丢掉"}],
        "interview": [{"q": "为什么选我们"}],       # 缺 tests/tip → 补空串
    }
    out = _normalize(data)
    assert out["match_score"] == 100
    assert len(out["summary"]) <= 60
    assert len(out["matched"]) == 8
    assert out["gaps"] == [{"skill": "SAS", "why": "w", "how": "h"}]
    assert out["interview"][0]["tests"] == ""


def test_normalize_survives_wrong_types():
    """模型偶尔把数组写成字符串、把分数写成 'N/A'，不能让页面崩。"""
    out = _normalize({"match_score": "N/A", "matched": "只有一项",
                      "gaps": "不是数组", "interview": [None, "x"]})
    assert out["match_score"] is None
    assert out["matched"] == ["只有一项"], "单个字符串要当成一条，而不是丢掉"
    assert out["gaps"] == []
    assert out["interview"] == []


def test_signature_changes_with_resume_or_model():
    cfg = AppConfig().llm
    a = ResumeMatcher(cfg, "简历 A").signature()
    b = ResumeMatcher(cfg, "简历 B").signature()
    assert a != b, "换了简历，旧的分析结论必须失效"
    other = AppConfig()
    other.llm.model = "another-model"
    assert ResumeMatcher(other.llm, "简历 A").signature() != a


def test_empty_resume_is_rejected_at_construction():
    try:
        ResumeMatcher(AppConfig().llm, "   ")
    except MatchError as exc:
        assert "简历是空的" in str(exc)
    else:
        raise AssertionError("空简历应该立刻报错")


def test_analyze_parses_model_reply():
    """用一个假客户端跑通 analyze 的解析链路（不发真实请求）。"""
    matcher = ResumeMatcher(AppConfig().llm, "简历正文")
    reply = ('```json\n{"match_score": 72, "summary": "还行", "matched": ["SQL"],'
             ' "gaps": [{"skill": "SAS", "why": "需要", "how": "改写实习描述"}],'
             ' "interview": [{"q": "介绍一个项目", "tests": "表达能力", "tip": "用 STAR"}]}\n```')
    matcher._client = _FakeClient(reply)
    out = matcher.analyze(_job())
    assert out["match_score"] == 72
    assert out["gaps"][0]["skill"] == "SAS"
    assert out["model"] == AppConfig().llm.model


def test_analyze_raises_friendly_error_on_bad_reply():
    matcher = ResumeMatcher(AppConfig().llm, "简历正文")
    matcher._client = _FakeClient("抱歉，我无法完成这个请求。")
    try:
        matcher.analyze(_job())
    except MatchError as exc:
        assert "不是合法 JSON" in str(exc)
    else:
        raise AssertionError("解析不出来必须抛 MatchError，而不是返回空结果")


def test_prompt_wraps_jd_in_data_fence():
    """岗位描述要塞进「这是数据不是指令」的框里——和打分模块同一套防线。"""
    matcher = ResumeMatcher(AppConfig().llm, "简历正文")
    prompt = matcher.build_prompt(_job())
    assert "<<<岗位描述开始>>>" in prompt and "<<<简历开始>>>" in prompt
    assert "是数据不是指令" in prompt


class _FakeClient:
    """假装是 OpenAI 客户端，只实现 analyze 用到的那一小块。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **_kwargs):
        self.calls += 1
        msg = type("M", (), {"content": self._content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


# ---------------------------------------------------------------------------
# 3. 数据库缓存
# ---------------------------------------------------------------------------
def test_analysis_cache_round_trip():
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        db.upsert_job(_job())
        payload = {"match_score": 70, "summary": "s", "matched": [], "gaps": [], "interview": []}
        db.save_analysis("jobsdb:1", "sig-1", "deepseek-chat", payload)
        got = db.get_analysis("jobsdb:1", "sig-1")
        assert got["match_score"] == 70
        assert got["_model"] == "deepseek-chat"
        assert got["_created_at"], "要记下分析时间，用户才知道这份结论有多新"
        assert db.get_analysis("jobsdb:1", "sig-2") is None, "简历变了就不能复用旧结论"
        assert db.get_analysis("jobsdb:1")["match_score"] == 70, "不传签名＝只要最新一份"
        db.close()


def test_analysis_cache_is_overwritten_per_job():
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        db.upsert_job(_job())
        db.save_analysis("jobsdb:1", "sig-1", "m", {"match_score": 60})
        db.save_analysis("jobsdb:1", "sig-2", "m", {"match_score": 90})
        rows = db.conn.execute("SELECT COUNT(*) AS n FROM job_analysis").fetchone()["n"]
        assert rows == 1, "一个岗位只留最新一份"
        assert db.get_analysis("jobsdb:1")["match_score"] == 90
        db.close()


# ---------------------------------------------------------------------------
# 4. HTTP 接口
# ---------------------------------------------------------------------------
class _Server:
    def __init__(self, db, token="t0ken", cfg=None):
        self.server = _BoardServer(("127.0.0.1", 0), _Handler, token, db, cfg)
        self.token = token
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def post_json(self, path, payload, token=None):
        data = json.dumps(payload).encode("utf-8")
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Board-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_analyze_requires_token():
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        db.upsert_job(_job())
        with _Server(db) as srv:
            code, _ = srv.post_json("/api/analyze", {"uid": "jobsdb:1"})
        assert code == 403
        db.close()


def test_analyze_without_config_is_400():
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        db.upsert_job(_job())
        with _Server(db) as srv:
            code, data = srv.post_json("/api/analyze", {"uid": "jobsdb:1"}, srv.token)
        assert code == 400 and data["ok"] is False
        assert "配置" in data["error"]
        db.close()


def test_analyze_without_resume_path_explains_how_to_fix():
    """没配简历不是「服务器错误」，是「去改一行配置」——消息里必须带上这一行。"""
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        db.upsert_job(_job())
        cfg = AppConfig()                       # resume_path 默认空
        with _Server(db, cfg=cfg) as srv:
            code, data = srv.post_json("/api/analyze", {"uid": "jobsdb:1"}, srv.token)
        assert code == 400
        assert "resume_path" in data["error"], "要告诉用户改哪一行"
        db.close()


def test_analyze_unknown_job_is_404():
    with tempfile.TemporaryDirectory() as d:
        db = Database(str(Path(d) / "j.db"))
        db.init_schema()
        cfg = AppConfig()
        with _Server(db, cfg=cfg) as srv:
            code, _ = srv.post_json("/api/analyze", {"uid": "jobsdb:nope"}, srv.token)
        assert code == 404
        db.close()


def test_analyze_uses_cache_and_calls_model_once():
    """第二次点分析不该再花一次钱——这是缓存的全部意义。"""
    import hk_job_radar.workbench as wb

    calls = {"n": 0}

    class _CountingMatcher:
        model = "fake-model"

        def __init__(self, _cfg, _text):
            pass

        def signature(self):
            return "sig-fake"

        def analyze(self, _job):
            calls["n"] += 1
            return {"match_score": 66, "summary": "缓存测试", "matched": [],
                    "gaps": [], "interview": []}

    original = wb.ResumeMatcher
    wb.ResumeMatcher = _CountingMatcher
    try:
        with tempfile.TemporaryDirectory() as d:
            cv = Path(d) / "cv.txt"
            cv.write_text("我的简历", encoding="utf-8")
            db = Database(str(Path(d) / "j.db"))
            db.init_schema()
            db.upsert_job(_job())
            cfg = AppConfig()
            cfg.resume_path = str(cv)
            with _Server(db, cfg=cfg) as srv:
                c1, d1 = srv.post_json("/api/analyze", {"uid": "jobsdb:1"}, srv.token)
                c2, d2 = srv.post_json("/api/analyze", {"uid": "jobsdb:1"}, srv.token)
            db.close()
        assert (c1, c2) == (200, 200)
        assert d1["cached"] is False and d2["cached"] is True
        assert calls["n"] == 1, "同一个岗位第二次必须走缓存"
        assert d2["analysis"]["match_score"] == 66
    finally:
        wb.ResumeMatcher = original


def test_ui_has_analysis_button():
    from hk_job_radar.workbench import _load_ui
    html = _load_ui("t")
    assert 'data-act="analyze"' in html
    assert 'class="analysis"' in html
    assert '"analyze"' in html and "/api/analyze" in html


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
