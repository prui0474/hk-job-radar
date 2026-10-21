"""网页配置向导（setup）单元测试：只测纯逻辑，不启服务、不联网、不碰真实配置。

为什么这些测试重要：向导手里握着用户的 config.yaml，而且它会「写文件」。
写坏一次，用户之前花时间填的关键词、画像、凭据就没了。所以这里重点盯四件事：

1. 校验分层对不对 —— 「必须修」和「建议改」不能混（混了就等于程序在刁难人）；
2. 写回文本是否逐字段一致 —— 靠 verify_config_text 兜底，但兜底本身要测；
3. 不丢东西 —— 用户自己写的注释、超出免费额度的关键词，都必须原样留着；
4. 出错时不写盘 —— 这是最要紧的一条，配置坏了还能重填，被覆盖了就没了。

兼容两种运行方式：
- pytest tests/test_setup_wizard.py
- python tests/test_setup_wizard.py
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hk_job_radar import setup_wizard as w

_REAL_ROOT = Path(__file__).resolve().parents[1]
_REAL_TEMPLATE = _REAL_ROOT / "config.example.yaml"
_REAL_PROFILE_TEMPLATE = _REAL_ROOT / "data" / "profiles" / "profile.example.yaml"


# ---------------------------------------------------------------------------
# 测试脚手架：在临时目录里造一个「假项目」，把模块里的路径指过去
# ---------------------------------------------------------------------------
class Sandbox:
    """临时项目目录 + 路径重定向。

    直接改模块级常量（CONFIG_PATH 等）而不引入依赖注入，是因为这些常量在
    生产代码里本来就该是模块级常量；测试只需用完还原即可。
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wizard_test_"))
        (self.root / "data" / "profiles").mkdir(parents=True, exist_ok=True)
        self.template = self.root / "config.example.yaml"
        if _REAL_TEMPLATE.exists():
            shutil.copy2(_REAL_TEMPLATE, self.template)
        self.profile_template = self.root / "data" / "profiles" / "profile.example.yaml"
        if _REAL_PROFILE_TEMPLATE.exists():
            shutil.copy2(_REAL_PROFILE_TEMPLATE, self.profile_template)
        self.config = self.root / "config.yaml"
        self.profile = self.root / "data" / "profiles" / "profile.yaml"
        self.api_key = self.root / "api_key.txt"
        self._saved: dict[str, object] = {}

    def __enter__(self) -> "Sandbox":
        for name, value in (
            ("CONFIG_PATH", self.config),
            ("TEMPLATE_PATH", self.template),
            ("PROFILE_PATH", self.profile),
            ("PROFILE_TEMPLATE_PATH", self.profile_template),
            ("API_KEY_PATH", self.api_key),
        ):
            self._saved[name] = getattr(w, name)
            setattr(w, name, value)
        return self

    def __exit__(self, *exc: object) -> None:
        for name, value in self._saved.items():
            setattr(w, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def write_config(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8", newline="\n")

    def md5(self) -> str:
        return hashlib.md5(self.config.read_bytes()).hexdigest()


def _payload(**over: object) -> dict[str, object]:
    """一份能通过校验的最小表单（各测试按需覆盖个别字段）。"""
    base: dict[str, object] = {
        "keywords": "data analyst\nbusiness analyst",
        "graduate_keywords": "",
        "career_stage": "student",
        "sources": {"jobsdb": True, "linkedin": False, "indeed": False,
                    "workday": True},
        "channel": "telegram",
        "telegram_bot_token": "123456789:AAExampleTokenForTesting1234567890",
        "telegram_chat_id": "1234567891",
        "report_time": "09:00",
        "score_threshold": 60,
        "realtime_threshold": 80,
        "profile_background": "香港城市大学数据科学硕士在读，2027 年毕业。" * 3,
        "api_key": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 一、表单归一化
# ---------------------------------------------------------------------------
def test_form_normalizes_keywords():
    """大小写、重复、粘贴带进来的引号和行首短横线，都要收拾干净。"""
    form = w.form_from_payload(_payload(
        keywords='  Data Analyst  \n- "business analyst"\ndata analyst\n\nDATA SCIENTIST'))
    assert form.keywords == ["data analyst", "business analyst", "data scientist"]


def test_form_rejects_unknown_career_stage():
    """乱填的求职阶段落到最保守的 student（学生签证下最安全）。"""
    assert w.form_from_payload(_payload(career_stage="随便")).career_stage == "student"


def test_form_accepts_list_of_uids():
    form = w.form_from_payload(_payload(channel="wxpusher", wxpusher_uids="UID_a, UID_b"))
    assert form.wxpusher_uids == ["uid_a", "uid_b"]


# ---------------------------------------------------------------------------
# 二、校验分层
# ---------------------------------------------------------------------------
def test_validate_blocks_empty_keywords():
    errors, _ = w.validate_form(w.form_from_payload(_payload(keywords="")))
    assert any(e["field"] == "keywords" for e in errors)


def test_validate_blocks_missing_channel():
    errors, _ = w.validate_form(w.form_from_payload(_payload(channel="")))
    assert any(e["field"] == "channel" for e in errors)


def test_validate_blocks_channel_missing_field():
    """选了 Telegram 却只填 token、不填 chat_id —— 消息发不出去，必须拦住。"""
    errors, _ = w.validate_form(w.form_from_payload(_payload(telegram_chat_id="")))
    assert any(e["field"] == "telegram_chat_id" for e in errors)


def test_validate_blocks_realtime_below_score():
    errors, _ = w.validate_form(
        w.form_from_payload(_payload(score_threshold=80, realtime_threshold=60)))
    assert any(e["field"] == "realtime_threshold" for e in errors)


def test_validate_blocks_no_source_enabled():
    errors, _ = w.validate_form(w.form_from_payload(_payload(
        sources={"jobsdb": False, "linkedin": False, "indeed": False})))
    assert any(e["field"] == "sources" for e in errors)


def test_validate_over_quota_is_note_not_error():
    """超过免费版关键词额度只提示、不拦 —— 程序运行时会自动取前几个。"""
    many = "\n".join(f"job title {i}" for i in range(11))
    errors, notes = w.validate_form(w.form_from_payload(_payload(keywords=many)))
    assert not errors
    assert any(n["field"] == "keywords" for n in notes)


def test_validate_short_profile_is_note_not_error():
    errors, notes = w.validate_form(w.form_from_payload(_payload(profile_background="短")))
    assert not errors
    assert any(n["field"] == "profile_background" for n in notes)


def test_validate_flags_malformed_telegram_token():
    errors, _ = w.validate_form(w.form_from_payload(_payload(telegram_bot_token="abc")))
    assert any(e["field"] == "telegram_bot_token" for e in errors)


# ---------------------------------------------------------------------------
# 三、渲染：字段写对 + 不丢东西
# ---------------------------------------------------------------------------
def test_render_roundtrip_matches_form():
    form = w.form_from_payload(_payload(career_stage="both", report_time="07:30",
                                        score_threshold=65, realtime_threshold=85))
    text = w.render_config_text(form)
    assert w.verify_config_text(text, form) == []


def test_render_keeps_every_source_switch():
    """回归用例：`_set_sources` 是**整块重写** sources 段。

    它曾把站点名单写死在函数里，于是新增一个站点后，用户一进向导点保存，
    那个站点的开关就被整块重写**静默删掉**——配置没报错，但从那天起不再抓了。
    这里逐个站点验证「勾上的开关必须还在」。
    """
    for name in w.WIZARD_SOURCES:
        sources = {n: n == name for n in w.WIZARD_SOURCES}
        form = w.form_from_payload(_payload(sources=sources))
        text = w.render_config_text(form)
        raw = yaml.safe_load(text)
        assert raw["sources"] == sources, f"{name} 的开关保存后丢了：{raw['sources']}"
        assert w.verify_config_text(text, form) == []


def test_render_keeps_template_comments():
    """写回后的文件仍带着讲解注释 —— 用户以后手改时还看得懂。"""
    with Sandbox() as box:
        form = w.form_from_payload(_payload())
        text = w.render_config_text(form)
        assert "四个通道" in text
        assert text.count("#") > 100          # 注释量级没掉
        assert box.template.read_text(encoding="utf-8").count("#") <= text.count("#")


def test_render_does_not_duplicate_keywords():
    """回归用例：注释夹在列表中间时，旧实现会「新列表 + 旧列表」叠成两份。"""
    with Sandbox() as box:
        box.write_config(
            "sources:\n  jobsdb: true\n"
            "keywords:\n  # 说明一行\n  - \"a job\"\n  # 又一行说明\n  - \"b job\"\n"
            "graduate_keywords: []\n"
            "career_stage: student\n"
            "score_threshold: 60\nrealtime_threshold: 80\n"
            'report_time: "09:00"\n'
            'notify:\n  channels:\n    - "wecom"\n'
            '  wecom:\n    webhook: ""\n'
            '  email:\n    smtp_host: ""\n    smtp_port: 465\n    user: ""\n'
            '    auth_code: ""\n    to: ""\n    use_ssl: true\n'
            '  telegram:\n    bot_token: ""\n    chat_id: ""\n'
            '  wxpusher:\n    app_token: ""\n    uids: []\n'
        )
        form = w.form_from_payload(_payload(keywords="a job\nb job"))
        text = w.render_config_text(form)
        import yaml
        raw = yaml.safe_load(text)
        assert raw["keywords"] == ["a job", "b job"]
        # 注释必须还在原位
        assert "# 说明一行" in text
        assert "# 又一行说明" in text


def test_render_keeps_per_item_comments_when_count_unchanged():
    """数量没变时逐项原位更新，每条词后面的说明都留着。"""
    with Sandbox() as box:
        box.write_config(
            "sources:\n  jobsdb: true\n"
            "keywords:\n"
            '  - "data analyst"      # 核心词\n'
            '  - "quant"              # 金融方向\n'
            "graduate_keywords: []\n"
            "career_stage: student\n"
            "score_threshold: 60\nrealtime_threshold: 80\n"
            'report_time: "09:00"\n'
            'notify:\n  channels:\n    - "wecom"\n'
            '  wecom:\n    webhook: ""\n'
            '  email:\n    smtp_host: ""\n    smtp_port: 465\n    user: ""\n'
            '    auth_code: ""\n    to: ""\n    use_ssl: true\n'
            '  telegram:\n    bot_token: ""\n    chat_id: ""\n'
            '  wxpusher:\n    app_token: ""\n    uids: []\n'
        )
        form = w.form_from_payload(_payload(keywords="data analyst\nquant"))
        text = w.render_config_text(form)
        assert "# 核心词" in text and "# 金融方向" in text


def test_render_migrates_legacy_top_level_telegram():
    """旧版配置只有顶层 telegram 段 → 自动升成 notify 段，token 不能丢。"""
    with Sandbox() as box:
        box.write_config(
            "sources:\n  jobsdb: true\n"
            "keywords:\n  - \"data analyst\"\n"
            "career_stage: student\n"
            "score_threshold: 60\nrealtime_threshold: 80\n"
            "# ---- Telegram 推送 ----\n"
            "telegram:\n"
            '  bot_token: "999999:LEGACYTOKENVALUE"\n'
            '  chat_id: "88888888"\n'
            "feedback_poll_seconds: 60\n"
            'report_time: "09:00"\n'
        )
        form = w.prepare_form(_payload(telegram_bot_token="", telegram_chat_id=""))
        text = w.render_config_text(form)
        import yaml
        raw = yaml.safe_load(text)
        assert raw["notify"]["channels"] == ["telegram"]
        assert raw["notify"]["telegram"]["bot_token"] == "999999:LEGACYTOKENVALUE"
        assert raw["notify"]["telegram"]["chat_id"] == "88888888"
        assert "\ntelegram:\n" not in text          # 旧段已被替换，不是两份并存


def test_render_falls_back_when_config_unparseable():
    """config.yaml 被用户改坏了，就用模板做底板 —— 页面仍然能保存成功。"""
    with Sandbox() as box:
        box.write_config("keywords: [这是: 坏掉的: yaml\n")
        form = w.form_from_payload(_payload())
        text = w.render_config_text(form)
        assert w.verify_config_text(text, form) == []


def test_verify_detects_mismatch():
    """verify_config_text 必须真的能发现写错，否则「绝不写坏配置」是空话。"""
    form = w.form_from_payload(_payload())
    text = w.render_config_text(form).replace('"09:00"', '"23:59"')
    problems = w.verify_config_text(text, form)
    assert any("日报时间" in p for p in problems)


# ---------------------------------------------------------------------------
# 四、保存：备份、失败不写盘、凭据沿用
# ---------------------------------------------------------------------------
def test_render_writes_browser_proxy():
    """三种代理取值都要能写进 browser: 段，并被逐字段校验认可。"""
    import yaml

    for value in ("", "system", "http://127.0.0.1:7890"):
        with Sandbox() as box:
            box.write_config(box.template.read_text(encoding="utf-8"))
            form = w.form_from_payload(_payload(browser_proxy=value))
            text = w.render_config_text(form)
            assert w.verify_config_text(text, form) == []
            assert yaml.safe_load(text)["browser"]["proxy"] == value


def test_proxy_lands_inside_browser_section_when_missing():
    """老配置的 browser 段里没有 proxy 时，要插在段内，不能补到文件末尾。

    补到末尾的 `  proxy:` 会挂到最后一个顶层段底下 —— YAML 语义整个变了，
    而且不会报错，只是代理悄悄不生效。这正是 _set_value 的 append_if_missing
    不能用于缩进字段的原因。
    """
    import yaml

    with Sandbox() as box:
        template = box.template.read_text(encoding="utf-8")
        stripped = template.replace('  proxy: ""', "", 1)
        assert "  proxy:" not in stripped       # 确认这一行真的被去掉了
        box.write_config(stripped)
        form = w.form_from_payload(_payload(browser_proxy="system"))
        text = w.render_config_text(form)
        assert w.verify_config_text(text, form) == []
        assert yaml.safe_load(text)["browser"]["proxy"] == "system"


def test_validate_rejects_proxy_without_port():
    form = w.form_from_payload(_payload(browser_proxy="127.0.0.1"))
    errors, _ = w.validate_form(form)
    assert any(e["field"] == "browser_proxy" for e in errors)

    # 完整的带端口地址不该被拦
    filled = w.form_from_payload(_payload(browser_proxy="http://127.0.0.1:7890"))
    errors2, _ = w.validate_form(filled)
    assert not any(e["field"] == "browser_proxy" for e in errors2)


def test_apply_form_creates_backup():
    with Sandbox() as box:
        box.write_config(box.template.read_text(encoding="utf-8"))
        result = w.apply_form(w.form_from_payload(_payload()))
        assert result["ok"], result
        backups = sorted(box.root.glob("config.yaml.bak-*"))
        assert len(backups) == 1
        assert result["backup"] == backups[0].name


def test_apply_form_does_not_touch_file_on_error():
    """校验失败时，原文件一个字节都不能变。"""
    with Sandbox() as box:
        original = box.template.read_text(encoding="utf-8")
        box.write_config(original)
        before = box.md5()
        result = w.apply_form(w.form_from_payload(_payload(keywords="")))
        assert result["ok"] is False
        assert box.md5() == before
        assert not list(box.root.glob("config.yaml.bak-*"))


def test_apply_form_writes_profile_and_api_key():
    with Sandbox() as box:
        box.write_config(box.template.read_text(encoding="utf-8"))
        result = w.apply_form(w.form_from_payload(_payload(api_key="sk-unit-test-key")))
        assert result["ok"], result
        assert box.api_key.exists()
        assert box.api_key.read_text(encoding="utf-8").strip() == "sk-unit-test-key"
        assert box.profile.exists()
        import yaml
        raw = yaml.safe_load(box.profile.read_text(encoding="utf-8"))
        assert "数据科学" in raw["background"]
        # API key 不能混进 config.yaml
        assert "sk-unit-test-key" not in box.config.read_text(encoding="utf-8")


def test_apply_form_merges_blank_secret_with_existing():
    """凭据留空 = 不改动。页面不回显 token，所以这条必须生效。"""
    with Sandbox() as box:
        box.write_config(box.template.read_text(encoding="utf-8"))
        first = w.apply_form(w.form_from_payload(_payload()))
        assert first["ok"], first
        # 第二次：只改日报时间，token 字段留空
        payload = _payload(telegram_bot_token="", telegram_chat_id="", report_time="08:15")
        second = w.apply_form(w.prepare_form(payload))
        assert second["ok"], second
        import yaml
        raw = yaml.safe_load(box.config.read_text(encoding="utf-8"))
        assert raw["notify"]["telegram"]["bot_token"].endswith("7890")
        assert raw["report_time"] == "08:15"


# ---------------------------------------------------------------------------
# 五、页面预填：不能因为免费版额度就把用户的关键词"看少"
# ---------------------------------------------------------------------------
def test_current_state_prefills_all_keywords_beyond_quota():
    """19 个关键词的旧配置，页面要显示 19 个。

    如果按 load_config 的结果预填（免费版只留 7 个），用户改个日报时间一保存，
    另外 12 个词就被永久删掉了 —— 这是真实的数据丢失。
    """
    with Sandbox() as box:
        many = "\n".join(f'  - "title {i}"' for i in range(12))
        box.write_config(
            "sources:\n  jobsdb: true\n"
            f"keywords:\n{many}\n"
            "career_stage: student\n"
            "score_threshold: 60\nrealtime_threshold: 80\n"
            'report_time: "09:00"\n'
            'notify:\n  channels:\n    - "wecom"\n'
            '  wecom:\n    webhook: ""\n'
            '  email:\n    smtp_host: ""\n    smtp_port: 465\n    user: ""\n'
            '    auth_code: ""\n    to: ""\n    use_ssl: true\n'
            '  telegram:\n    bot_token: ""\n    chat_id: ""\n'
            '  wxpusher:\n    app_token: ""\n    uids: []\n'
        )
        state = w.current_state()
        assert len(state["keywords"]) == 12


def test_current_state_prefills_sample_keywords_when_empty():
    """关键词一个都没有时，页面预填一组示例词并标记出来。

    空手面对空文本框是新手卡住的地方，给一组能直接改的起点成本很低。
    注意这只发生在页面上：config.example.yaml 与 AppConfig 仍然保持空，
    这样「模板被复制」和「字段缺失」两条路径的语义都不会被搅乱。
    """
    with Sandbox() as box:
        box.write_config("career_stage: student\nkeywords: []\n")
        state = w.current_state()
        assert state["keywords"] == list(w.SAMPLE_KEYWORDS)
        assert state["keywords_are_samples"] is True


def test_current_state_marks_secrets_without_leaking():
    with Sandbox() as box:
        box.write_config(box.template.read_text(encoding="utf-8"))
        w.apply_form(w.form_from_payload(_payload()))
        state = w.current_state()
        assert state["telegram_token_filled"] is True
        assert "telegram_bot_token" not in state          # 不回传明文
        assert state["keywords_are_samples"] is False     # 用户自己的词，不标记为示例


# ---------------------------------------------------------------------------
# 六、页面与命令注册
# ---------------------------------------------------------------------------
def test_ui_file_has_token_placeholder():
    assert w.UI_PATH.exists()
    html = w.UI_PATH.read_text(encoding="utf-8")
    assert "__WIZARD_TOKEN__" in html
    assert "X-Wizard-Token" in html


def test_cli_registers_setup_command():
    from hk_job_radar.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["setup", "--no-browser"])
    assert args.func.__name__ == "cmd_setup"
    assert args.no_browser is True


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
