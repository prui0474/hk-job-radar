"""本地网页配置向导：在浏览器里点选填配置，不用手改 YAML。

为什么需要它
------------
本项目面向「计算机基础一般、想快点开始投简历」的用户。手改 config.yaml 有
三个高发故障点：① 在几百行注释里找不到该填的位置；② 缩进多一格少一格，
YAML 直接解析失败；③ 用记事本另存把编码搞成 GBK，中文全变乱码。
这三件事都跟「求职」无关，却能让一个人半小时内放弃。

设计原则
--------
1. **零新增依赖**：只用标准库 http.server。用户装依赖那一步已经够重了，
   不能为了「省一次手改」再让他装个 Web 框架。

2. **只监听本机**：绑定 127.0.0.1，不从 0.0.0.0 监听。同一 WiFi 下的其他
   电脑根本连不上这个端口 —— 配置里有推送凭据，绝不能让别人能打开。

3. **出错绝不写盘**：先备份 → 生成文本 → 程序自己重新解析一遍并逐字段比对
   → 全对才覆盖。任何一步不对就返回错误，用户原有的 config.yaml 一个字节
   都不会动。宁可让人多点一次保存，也不能让人丢掉已经填好的配置。

4. **保留原有注释**：新版 config.yaml 不是「重新生成一份光秃秃的」，而是
   在现有文件（或 config.example.yaml）上做定点替换。用户改完配置后打开
   文件，那些讲解「为什么这么填」的中文注释还在原地。

5. **CSRF 防护**：启动时生成一次性令牌，拼在地址里。浏览器里另一个网页
   无法拿到这个令牌，也就无法偷偷往 http://127.0.0.1:8765 发 POST 改你的
   配置（本地服务同样可能被跨站请求打，这是常被忽略的一类问题）。

用法
----
    python run.py setup          # 或双击 setup.bat / setup.command
    python run.py setup --no-browser
    python run.py setup --port 8800
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yaml
from loguru import logger

from hk_job_radar import tier
from hk_job_radar.config import (
    CONFIG_PATH,
    PROJECT_ROOT,
    PROFILE_PATH,
    ConfigError,
    load_config,
)

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
#: 页面文件与 wizard 放在同一个包里，随源码一起分发（本仓库不打包成 wheel，
#: 直接跑源码，所以不需要额外的 package_data 配置）。
UI_PATH = Path(__file__).with_name("setup_ui.html")

#: 注释最完整的那份模板。用户把 config.yaml 改坏了、或者还没有 config.yaml 时，
#: 用它当底板，这样修出来的文件依然带着全部讲解。
TEMPLATE_PATH = PROJECT_ROOT / "config.example.yaml"

PROFILE_TEMPLATE_PATH = PROJECT_ROOT / "data" / "profiles" / "profile.example.yaml"
API_KEY_PATH = PROJECT_ROOT / "api_key.txt"

#: 端口扫描范围。8765 被别的软件占了就顺延，用户不需要懂端口是什么。
PORT_START = 8765
PORT_END = 8775

#: POST 请求体上限。正常表单只有几 KB，256 KB 足够，同时能挡住「往本地服务
#: 灌垃圾数据」这类误伤。
_MAX_BODY_BYTES = 256 * 1024

#: 画像 background 的「认真填了吗」门槛，与 doctor 的判定保持一致
_PROFILE_MIN_CHARS = 80

#: 关键词为空时，在**页面上**预填的示例岗位名。
#:
#: 刻意只做在向导这一层，不动 config.example.yaml，也不动 AppConfig 的默认值：
#:   - 模板给默认词 → 直接复制模板的用户会以为这些词是自己写的；
#:   - AppConfig 给默认词 → 「字段缺失」和「用户主动清空」再也区分不出来，
#:     用户清空后会被悄悄填回去，这是更隐蔽的错。
#: 放在页面上则刚好：词摆在眼前、标着「示例」、鼠标一点就能改，
#: 而空手面对一个空文本框才是真正卡住的地方。
SAMPLE_KEYWORDS = (
    "data analyst",
    "business analyst",
    "data scientist",
    "financial analyst",
    "risk analyst",
)

#: 请求头 / 查询串里携带令牌的字段名
TOKEN_HEADER = "X-Wizard-Token"
TOKEN_QUERY = "t"

#: 各通道「哪些字段是必填」的对照表：字段名 → 给用户看的说明
_CHANNEL_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "wecom": (("wecom_webhook", "群机器人 Webhook 地址"),),
    "email": (
        ("email_user", "发件邮箱"),
        ("email_auth_code", "邮箱授权码"),
        ("email_to", "收件邮箱"),
    ),
    "telegram": (
        ("telegram_bot_token", "机器人 token"),
        ("telegram_chat_id", "chat_id"),
    ),
    "wxpusher": (
        ("wxpusher_app_token", "appToken"),
        ("wxpusher_uids", "UID"),
    ),
}

_CHANNEL_NAMES = {"wecom": "企业微信", "email": "邮件", "telegram": "Telegram",
                  "wxpusher": "WxPusher"}


class _TemplateError(Exception):
    """配置模板里找不到预期的那一行。

    正常情况不会发生（模板与代码一起分发，测试会盯着）。真发生了说明模板
    被人改过，此时中止保存比「写出半对半错的配置」安全得多。
    """


# ---------------------------------------------------------------------------
# 表单数据模型
# ---------------------------------------------------------------------------
#: 向导里可勾选的抓取站点。★ 这是唯一名单：新增站点只改这一处，
#: 解析 / 写回 / 探路模板 / 预览状态四处都会跟着变。
#:
#: 为什么必须是唯一名单：`_set_sources` 是「整块重写 sources 段」，
#: 名单写死在四个地方时，只要漏掉写回那一处，用户一点保存，
#: 新增站点的开关就会被整块重写**静默删掉**——配置文件看着没报错，
#: 但从那天起那个源再也没抓过。这类「保存即丢失」最难排查。
WIZARD_SOURCES: tuple[str, ...] = ("jobsdb", "linkedin", "indeed", "workday")

#: 没有旧配置时，哪些站点默认开着。
#: Workday 默认开：它是最稳的一条链路（纯 HTTP、无验证码、还能直接告诉
#: 我们岗位是否还在招），关掉它没有任何好处。
WIZARD_SOURCES_DEFAULT_ON = ("jobsdb", "workday")


@dataclass
class WizardForm:
    """向导收集到的全部输入（字段名与前端 JSON 一一对应，方便互相追溯）。"""

    keywords: list[str] = field(default_factory=list)
    graduate_keywords: list[str] = field(default_factory=list)
    career_stage: str = "student"
    sources: dict[str, bool] = field(
        default_factory=lambda: {n: n in WIZARD_SOURCES_DEFAULT_ON
                                 for n in WIZARD_SOURCES}
    )

    channel: str = ""
    wecom_webhook: str = ""
    email_smtp_host: str = "smtp.qq.com"
    email_smtp_port: int = 465
    email_user: str = ""
    email_auth_code: str = ""
    email_to: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    wxpusher_app_token: str = ""
    wxpusher_uids: list[str] = field(default_factory=list)

    report_time: str = "09:00"
    score_threshold: int = 60
    realtime_threshold: int = 80

    #: 浏览器代理："" 直连（默认）/"system" 跟随系统/具体地址
    browser_proxy: str = ""

    profile_background: str = ""
    api_key: str = ""


def _clean_lines(text: Any, split_pattern: str = r"[\r\n]+") -> list[str]:
    """把「多行文本 / 列表」统一成去空行、去首尾空格、小写、保序去重的列表。

    关键词统一小写是刻意的：库里存的是小写，配置里大小写混写会导致
    「同一个词被当成两个词」这种难查的问题。前端也会提示「用英文小写」。

    split_pattern 默认只按换行切（关键词一行一个）；WxPusher 的 UID 允许
    逗号分隔（页面提示里就是这么写的），所以那一处会改成按逗号或换行切。
    """
    if isinstance(text, str):
        raw_items = re.split(split_pattern, text)
    elif isinstance(text, (list, tuple)):
        raw_items = [str(x) for x in text]
    else:
        raw_items = []

    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        # 依次剥掉：首尾空白 → 行首的 "- "（用户可能整段复制粘贴）
        # → 首尾引号（同样可能是粘贴带进来的）。顺序不能乱：先剥短横线再剥引号，
        # 否则 `- "xxx"` 会剩下一个孤零零的开引号。
        value = str(item).strip()
        value = value.lstrip("-").strip()
        value = value.strip("\"'").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clean_proxy(value: Any) -> str:
    """规范化浏览器代理输入。

    页面给的是「三选一 + 一个自定义地址框」，但服务端不能假设前端一定守规矩，
    所以这里再收一遍：留空 = 直连；system 不区分大小写；其余原样保留。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() == "system":
        return "system"
    return text


def form_from_payload(payload: dict[str, Any]) -> WizardForm:
    """前端 JSON → WizardForm（做过类型收敛，脏数据不会带进下一步）。"""
    sources_raw = payload.get("sources") or {}
    if sources_raw:
        # 前端确实传了开关状态 → 没提到的站点按「关」处理。
        # 不能按「默认开」处理：用户把三个站全关掉时，若 workday 被悄悄打开，
        # 页面看着是「一个都没选」，实际却在抓某一家公司——静默违背用户意图。
        sources = {name: bool(sources_raw.get(name, False)) for name in WIZARD_SOURCES}
    else:
        sources = {name: name in WIZARD_SOURCES_DEFAULT_ON for name in WIZARD_SOURCES}
    stage = str(payload.get("career_stage") or "student").strip().lower()
    if stage not in ("student", "graduate", "both"):
        stage = "student"

    return WizardForm(
        keywords=_clean_lines(payload.get("keywords")),
        graduate_keywords=_clean_lines(payload.get("graduate_keywords")),
        career_stage=stage,
        sources=sources,
        channel=str(payload.get("channel") or "").strip().lower(),
        wecom_webhook=str(payload.get("wecom_webhook") or "").strip(),
        email_smtp_host=str(payload.get("email_smtp_host") or "").strip(),
        email_smtp_port=_as_int(payload.get("email_smtp_port"), 465),
        email_user=str(payload.get("email_user") or "").strip(),
        email_auth_code=str(payload.get("email_auth_code") or "").strip(),
        email_to=str(payload.get("email_to") or "").strip(),
        telegram_bot_token=str(payload.get("telegram_bot_token") or "").strip(),
        telegram_chat_id=str(payload.get("telegram_chat_id") or "").strip(),
        wxpusher_app_token=str(payload.get("wxpusher_app_token") or "").strip(),
        wxpusher_uids=_clean_lines(payload.get("wxpusher_uids"), r"[\r\n,;、]+"),
        report_time=str(payload.get("report_time") or "09:00").strip(),
        score_threshold=_as_int(payload.get("score_threshold"), 60),
        realtime_threshold=_as_int(payload.get("realtime_threshold"), 80),
        browser_proxy=_clean_proxy(payload.get("browser_proxy")),
        profile_background=str(payload.get("profile_background") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
    )


# ---------------------------------------------------------------------------
# 校验：分「必须修」和「建议改」两级
# ---------------------------------------------------------------------------
#: 这几项属于凭据，页面只回传「是否已配置」，不回传明文。
#: 用户留空 = 不改动（见 _merge_secrets）；想换就重新粘贴一遍。
def _load_old_config() -> Optional[Any]:
    """读当前 config.yaml；文件不存在或读不出来都返回 None（不抛出）。"""
    if not CONFIG_PATH.exists():
        return None
    try:
        return load_config(CONFIG_PATH, require_runnable=False)
    except (ConfigError, OSError):
        return None


def _merge_secrets(form: WizardForm) -> WizardForm:
    """凭据字段留空时沿用旧值。

    为什么必须做：页面出于安全不回显 token 明文，所以输入框初始是空的。
    如果直接提交，空值会覆盖掉用户原来配好的 token —— 一个「只改了日报时间」
    的动作就会把推送搞坏。这里把「留空」解释为「不改」，符合直觉。
    """
    old = _load_old_config()
    if old is None:
        return form
    if not form.wecom_webhook:
        form.wecom_webhook = old.notify.wecom.webhook
    if not form.email_auth_code:
        form.email_auth_code = old.notify.email.auth_code
    if not form.telegram_bot_token:
        form.telegram_bot_token = old.notify.telegram.bot_token
    if not form.wxpusher_app_token:
        form.wxpusher_app_token = old.notify.wxpusher.app_token
    if not form.telegram_chat_id:
        form.telegram_chat_id = old.notify.telegram.chat_id
    if not form.email_user:
        form.email_user = old.notify.email.user
    if not form.email_to:
        form.email_to = ",".join(old.notify.email.to)
    return form


def prepare_form(payload: dict[str, Any]) -> WizardForm:
    """前端 JSON → 可校验的 WizardForm（含凭据沿用旧值的处理）。"""
    return _merge_secrets(form_from_payload(payload))


def validate_form(form: WizardForm) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """返回 (错误列表, 提示列表)。

    错误 = 保存会被拒绝（配置根本跑不起来）；
    提示 = 可以保存，但结果会不如预期（例如关键词超过免费版额度）。
    这个区分很重要：把「建议」也做成硬拦截，会让用户觉得程序在跟他作对。
    """
    errors: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []

    total_kw = len(form.keywords) + len(form.graduate_keywords)
    if not form.keywords and not form.graduate_keywords:
        errors.append({
            "field": "keywords",
            "message": "关键词还是空的。至少填 1 个你想找的岗位名称，"
                       "比如 data analyst、business analyst。",
        })
    if total_kw > tier.FREE_MAX_KEYWORDS:
        notes.append({
            "field": "keywords",
            "message": f"关键词一共 {total_kw} 个，免费版最多用前 {tier.FREE_MAX_KEYWORDS} 个"
                       f"（程序会按顺序取前 {tier.FREE_MAX_KEYWORDS} 个，其余忽略）。"
                       f"想全部生效需要高级版，或者删掉几个不重要的。",
        })

    if not any(form.sources.values()):
        errors.append({
            "field": "sources",
            "message": "四个招聘网站一个都没开。至少打开 JobsDB，否则抓不到任何岗位。",
        })

    proxy = form.browser_proxy.strip()
    if proxy and proxy.lower() != "system" and ":" not in proxy:
        errors.append({
            "field": "browser_proxy",
            "message": "代理地址要带端口，例如 http://127.0.0.1:7890 "
                       "（端口号在你代理软件的主界面上能看到）。",
        })

    if not form.channel:
        errors.append({"field": "channel", "message": "还没有选择推送通道。"
                                                       "消息要走哪个渠道发到手机上，必须选一个。"})
    elif form.channel not in tier.VALID_CHANNELS:
        errors.append({"field": "channel", "message": f"未知的通道「{form.channel}」。"})
    else:
        for field_name, label in _CHANNEL_REQUIRED[form.channel]:
            value = getattr(form, field_name)
            if isinstance(value, list):
                filled = bool(value)
            else:
                filled = bool(str(value).strip())
            if not filled:
                errors.append({
                    "field": field_name,
                    "message": f"{_CHANNEL_NAMES[form.channel]}还差一项：{label}。",
                })
        errors.extend(_channel_format_errors(form))

    if not re.fullmatch(r"\d{1,2}:\d{2}", form.report_time):
        errors.append({"field": "report_time", "message": "日报时间请按 09:00 这种格式填写。"})
    else:
        hh, mm = (int(x) for x in form.report_time.split(":"))
        if hh > 23 or mm > 59:
            errors.append({"field": "report_time", "message": "日报时间不存在，请重新选择。"})

    if not (0 <= form.score_threshold <= 100):
        errors.append({"field": "score_threshold", "message": "及格线要在 0-100 之间。"})
    if not (0 <= form.realtime_threshold <= 100):
        errors.append({"field": "realtime_threshold", "message": "实时推送线要在 0-100 之间。"})
    if form.realtime_threshold < form.score_threshold:
        errors.append({"field": "realtime_threshold",
                       "message": "实时推送线不能低于及格线（能实时推的岗位，肯定也该进日报）。"})

    bg = form.profile_background
    if not bg:
        notes.append({
            "field": "profile_background",
            "message": "个人背景留空了。AI 没有依据，只能靠猜，打分会很粗——"
                       "建议写两三句：读什么专业、做过什么、想找什么方向。",
        })
    elif len(bg) < _PROFILE_MIN_CHARS:
        notes.append({
            "field": "profile_background",
            "message": f"个人背景只有 {len(bg)} 个字，偏少。"
                       f"多写一点实习、项目、技能，打分会准得多。",
        })

    if form.api_key and not form.api_key.startswith("sk-"):
        notes.append({
            "field": "api_key",
            "message": "这个 key 不是 sk- 开头。DeepSeek 的 key 一般以 sk- 开头，"
                       "请确认复制完整了（少了字符会认证失败）。",
        })

    return errors, notes


def _channel_format_errors(form: WizardForm) -> list[dict[str, str]]:
    """通道字段的「格式」检查：填了但明显不像真的，提前指出比事后收不到消息好。"""
    out: list[dict[str, str]] = []
    if form.channel == "wecom":
        hook = form.wecom_webhook
        if hook and "qyapi.weixin.qq.com" not in hook:
            out.append({"field": "wecom_webhook",
                        "message": "这不像企业微信群机器人的地址（应以 "
                                   "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key= 开头）。"})
    elif form.channel == "email":
        for name, label in (("email_user", "发件邮箱"), ("email_to", "收件邮箱")):
            value = getattr(form, name)
            if value and "@" not in value:
                out.append({"field": name, "message": f"{label}看起来不完整，缺少 @。"})
    elif form.channel == "telegram":
        token = form.telegram_bot_token
        # 官方 token 形如 1234567890:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
        if token and not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
            out.append({"field": "telegram_bot_token",
                        "message": "token 形状不对：应该是「一串数字:一串字母数字」，"
                                   "中间有冒号。请完整复制，不要带空格。"})
        chat = form.telegram_chat_id
        if chat and not re.fullmatch(r"-?\d+", chat):
            out.append({"field": "telegram_chat_id",
                        "message": "chat_id 应该是一串纯数字（群聊可能以 - 开头）。"})
    return out


# ---------------------------------------------------------------------------
# YAML 渲染：在原有文本上定点替换，保留全部中文注释
# ---------------------------------------------------------------------------
def _yaml_scalar(value: Any) -> str:
    """把值渲染成安全的 YAML 标量。

    用 json.dumps 而不是自己拼引号：JSON 字符串本身就是合法的 YAML 双引号
    标量，转义规则完全覆盖（引号、反斜杠、换行都不会破坏结构）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _set_value(text: str, key: str, value: Any, *, label: str, raw: bool = False,
               append_if_missing: bool = False) -> str:
    """替换 `key: 旧值   # 注释` 中的旧值，**保留行尾注释**。

    为什么要保留：模板里的注释是给人看的说明（例如「465 = SSL；587 = STARTTLS」），
    用户以后回头改配置时还要靠它。所以只动值，不动注释。

    raw=True 表示 value 已经是「可以直接写进 YAML 的文本」（例如内联列表
    `["UID_x"]`），不要再套引号 —— 否则会被当成字符串，而不是列表。

    append_if_missing：找不到这一行时，在文件末尾补一条。只对**顶层**字段
    使用（缩进字段补错位置会变成别的段落的子项）。这一条是为了照顾「旧版
    config.yaml 里还没有这个字段」的老用户 —— 不能因为他们配置老，就把整份
    文件换成模板（那会连他们自己写的关键词和注释一起丢掉）。
    """
    pattern = re.compile(
        rf"^({re.escape(key)}:[ \t]*)"
        rf'("[^"]*"|\'[^\']*\'|[^#\n]*?)'
        rf"([ \t]*(?:#[^\n]*)?)$",
        re.MULTILINE,
    )
    rendered = str(value) if raw else _yaml_scalar(value)
    new_text, count = pattern.subn(
        lambda m: f"{m.group(1)}{rendered}{m.group(3)}", text, count=1
    )
    if count != 1:
        if not append_if_missing:
            raise _TemplateError(f"配置模板里找不到「{label}」这一行（{key}）")
        body = text.rstrip("\n")
        return (f"{body}\n\n# ---- 由配置向导补充（旧版配置里还没有这一项）----\n"
                f"{key}: {rendered}\n")
    return new_text


def _set_section_value(text: str, section: str, key: str, value: Any) -> str:
    """在 `section:` 段内设置 `key:` 的值；段里没有这一行就插到段首之后。

    为什么不复用 _set_value 的 append_if_missing：那个只会补到**文件末尾**，
    而补到末尾的 `  proxy: ""` 会挂到别的段落底下，YAML 语义整个变了。
    缩进字段必须长在自己那一段里。
    """
    if re.search(rf"^{re.escape(key)}:", text, re.MULTILINE):
        return _set_value(text, key, value, label=key.strip())

    rendered = _yaml_scalar(value)
    header = re.search(rf"^{re.escape(section)}:[ \t]*\n", text, re.MULTILINE)
    if header is None:
        # 连这一段都没有（很老的配置）→ 文末补一个完整的小段
        tail = "" if text.endswith("\n") else "\n"
        return f"{text}{tail}\n{section}:\n{key}: {rendered}\n"
    at = header.end()
    return f"{text[:at]}{key}: {rendered}\n{text[at:]}"


def _trailing_comment(line: str) -> str:
    """取出一行末尾的 `# 注释`（含前面的空格），没有就返回空串。

    要求 # 前面有一个空白字符，避免把值里的 # 误当成注释开头
    （例如岗位关键词 "c#"）。
    """
    found = re.search(r"(\s#[^\n]*)$", line.rstrip("\n"))
    return ("   " + found.group(1).strip()) if found else ""


def _set_block_list(text: str, key: str, items: list[str], *, label: str,
                    indent: int = 2, append_if_missing: bool = False) -> str:
    """只替换列表项，**保留块内所有注释行**。

    为什么不用「整块正则替换」：真实用户会在列表中间插注释，例如

        keywords:
          # --- 数据类 ---
          - "data scientist"
          # 2026-10-14 删掉了 fintech，因为它是行业名不是岗位名
          - "data analyst"

    整块替换会把这些注释连同列表一起丢掉；更糟的是，如果注释紧跟在
    `keywords:` 后面，正则匹配不到「连续的子项行」，就会变成「新列表插在
    旧列表上面」，同一个词出现两遍。所以这里改成按行处理：

    1. 找到 `key:` 那一行，往下收集所有「缩进到该层」的行（含注释）；
    2. 只把其中的 `- xxx` 行挑出来，用新列表替换；
    3. 插回第一个旧子项原来的位置 —— 头部注释还在上面，尾部注释还在下面。
    """
    pad = " " * indent
    lines = text.splitlines(keepends=True)
    start: Optional[int] = None
    key_re = re.compile(rf"^{re.escape(key)}:")
    for i, line in enumerate(lines):
        if key_re.match(line):
            start = i
            break

    if start is None:
        if not append_if_missing:
            raise _TemplateError(f"配置模板里找不到「{label}」这一行（{key}）")
        if items:
            body = f"{key}:\n" + "".join(f"{pad}- {_yaml_scalar(i)}\n" for i in items)
        else:
            body = f"{key}: []\n"
        tail = text.rstrip("\n")
        return (f"{tail}\n\n# ---- 由配置向导补充（旧版配置里还没有这一项）----\n"
                + body)

    # 收集属于这个列表的行：从 key 行往下，连续的「有这一层缩进」的行
    end = start + 1
    while end < len(lines) and lines[end].startswith(pad):
        end += 1
    block = lines[start + 1:end]

    old_item_pos = [k for k, ln in enumerate(block) if ln.strip().startswith("-")]
    new_items = [f"{pad}- {_yaml_scalar(it)}\n" for it in items]

    if items and len(old_item_pos) == len(items):
        # 数量没变 → 逐个原位更新，连每一条词后面的注释都保住
        # （用户常常只改日报时间，不该因此把「为什么加这个词」的说明擦掉）
        for pos, item in zip(old_item_pos, items):
            comment = _trailing_comment(block[pos])
            block[pos] = f"{pad}- {_yaml_scalar(item)}{comment}\n"
        new_block = block
    elif items:
        kept = [ln for k, ln in enumerate(block) if k not in old_item_pos]
        anchor = old_item_pos[0] if old_item_pos else 0
        shift = sum(1 for k in old_item_pos if k < anchor)   # anchor 之前被删掉的项数
        at = max(0, anchor - shift)
        new_block = kept[:at] + new_items + kept[at:]
    else:
        new_block = [ln for k, ln in enumerate(block) if k not in old_item_pos]

    head_value = "" if items else " []"

    # 重建 key 那一行（保留它原有的行尾注释）
    nl = "\n" if lines[start].endswith("\n") else ""
    comment = ""
    found = re.search(r"(#[^\n]*)$", lines[start].rstrip("\n"))
    if found:
        comment = "   " + found.group(1)
    lines[start] = f"{key}:{head_value}{comment}{nl}"

    return "".join(lines[:start + 1] + new_block + lines[end:])


def _set_sources(text: str, sources: dict[str, bool]) -> str:
    """整块重写 sources（各站点开关）。

    不用逐个 `_set_value` 的原因：`jobsdb:` 在 daily_limits 段里也出现一次，
    逐个替换容易改错位置。整块替换只认 `sources:` 之后的连续若干行，没有歧义。

    ★ 重写的名单必须来自 WIZARD_SOURCES，不能就地写几个名字：
    这是**整块替换**，漏写一个站点等于把它从配置里删掉。
    """
    pattern = re.compile(r"^(sources:\n)((?:  [A-Za-z_]+:[^\n]*\n)*)", re.MULTILINE)
    body = "".join(
        f"  {name}: {_yaml_scalar(bool(sources.get(name, False)))}\n"
        for name in WIZARD_SOURCES
    )
    new_text, count = pattern.subn(lambda m: m.group(1) + body, text, count=1)
    if count != 1:
        # 极旧的配置可能没有 sources 段：补到文件末尾，不整份重写
        tail = text.rstrip("\n")
        return (f"{tail}\n\n# ---- 由配置向导补充（旧版配置里还没有这一项）----\n"
                f"sources:\n{body}")
    return new_text


def render_config_text(form: WizardForm) -> str:
    """把表单渲染成完整的 config.yaml 文本。

    底板按「现有 config.yaml → config.example.yaml → 内置最小模板」逐个尝试。
    为什么要退避：用户的 config.yaml 可能来自旧版本，缺少后来新增的字段
    （例如 career_stage），定点替换会因为「找不到那一行」而失败。这时换一份
    底板重做，比让用户自己去找缺了什么强得多。只要有一份能完整套上，就不报错。
    """
    for base in _base_candidates():
        try:
            return _apply_form_to_text(base, form)
        except _TemplateError as exc:
            logger.warning(f"以当前底板渲染失败，换下一份重试：{exc}")
            continue
    raise _TemplateError("现有 config.yaml 和 config.example.yaml 都缺字段")


def _base_candidates() -> list[str]:
    """按优先级给出可用的底板文本（只保留能被 YAML 解析的那些）。"""
    out: list[str] = []
    for path in (CONFIG_PATH, TEMPLATE_PATH):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            logger.warning(f"{path.name} 无法解析，跳过它。")
            continue
        out.append(text)
    # 内置最小模板永远可作最后一道兜底（例如安装包残缺、两个文件都丢了）
    out.append(_minimal_yaml())
    return out


def _notify_block(form: WizardForm) -> str:
    """生成完整的 notify: 段（四个通道都写上，只有选中的那个有值）。

    四个通道全写的好处：字段位置固定，用户以后想换通道，改一下 channels
    那一行就行，不用再去翻文档找「企业微信该填在哪个缩进里」。
    """
    return (
        "# ---- 推送设置（四个通道完全平等，任选其一）----\n"
        "notify:\n"
        "  channels:\n"
        f"    - {_yaml_scalar(form.channel)}\n"
        "\n"
        "  wecom:\n"
        f"    webhook: {_yaml_scalar(form.wecom_webhook)}\n"
        "\n"
        "  email:\n"
        f"    smtp_host: {_yaml_scalar(form.email_smtp_host)}\n"
        f"    smtp_port: {form.email_smtp_port}\n"
        f"    user: {_yaml_scalar(form.email_user)}\n"
        f"    auth_code: {_yaml_scalar(form.email_auth_code)}\n"
        f"    to: {_yaml_scalar(form.email_to)}\n"
        "    use_ssl: true\n"
        "\n"
        "  telegram:\n"
        f"    bot_token: {_yaml_scalar(form.telegram_bot_token)}\n"
        f"    chat_id: {_yaml_scalar(form.telegram_chat_id)}\n"
        "\n"
        "  wxpusher:\n"
        f"    app_token: {_yaml_scalar(form.wxpusher_app_token)}\n"
        f"    uids: {json.dumps(form.wxpusher_uids, ensure_ascii=False)}\n"
    )


def _ensure_notify_section(text: str, form: WizardForm) -> str:
    """把旧版结构（顶层 `telegram:` 段）升成新结构（`notify:` 段）。

    为什么必须做一次迁移：早期版本的 config.yaml 只有

        telegram:
          bot_token: "..."
          chat_id: "..."

    而现在的模板是 `notify.channels + notify.telegram` 这种四通道结构。
    如果只是「找不到 notify 就换用模板」，用户自己攒的关键词、自己写的注释
    会一起消失 —— 那是真实的数据丢失，不是格式问题。

    迁移策略：把顶层 telegram 段整块换成 notify 段，凭据值从 form 里带过来
    （form 已经通过 _merge_secrets 继承了旧 token），所以一个字符都不会丢。
    """
    if re.search(r"^notify:", text, re.MULTILINE):
        return text          # 已经是新结构，交给后面的定点替换

    # 连前面的「# ---- Telegram 推送 ----」标题一起换掉，避免留下一个
    # 写着 Telegram、下面却是四通道的误导性注释
    pattern = re.compile(
        r"^(?:#[^\n]*[Tt]elegram[^\n]*\n)?telegram:\n(?:(?:[ \t][^\n]*)?\n)*",
        re.MULTILINE,
    )
    new_text, count = pattern.subn(lambda _m: _notify_block(form), text, count=1)
    if count == 1:
        logger.info("检测到旧版配置结构（顶层 telegram 段），已转换为 notify 段。")
        return new_text

    # 连顶层 telegram 段都没有（例如用户把它删干净了）→ 补在文件末尾
    return text.rstrip("\n") + "\n\n" + _notify_block(form)


def _apply_form_to_text(text: str, form: WizardForm) -> str:
    """把表单逐字段写进一份底板文本。

    顶层字段都带 append_if_missing=True：老用户的 config.yaml 可能缺后来新增
    的项，补一条比整份换成模板安全 —— 他们的关键词、注释、个人化设置都还在。
    缩进字段（webhook / bot_token 等）必须严格匹配，因为补错缩进会变成别的
    段落的子项，那就不是「缺一个字段」而是「配置结构错了」。
    """
    text = _ensure_notify_section(text, form)
    text = _set_sources(text, form.sources)
    text = _set_block_list(text, "keywords", form.keywords, label="关键词",
                           append_if_missing=True)
    text = _set_block_list(text, "graduate_keywords", form.graduate_keywords,
                           label="毕业生项目关键词", append_if_missing=True)
    text = _set_value(text, "career_stage", form.career_stage, label="求职阶段",
                      append_if_missing=True)

    # 推送通道：只写用户选中的那一个。四通道完全平等，没有「默认通道」。
    text = _set_block_list(text, "  channels", [form.channel], label="推送通道",
                           indent=4)

    text = _set_value(text, "    webhook", form.wecom_webhook, label="企业微信 webhook")
    text = _set_value(text, "    smtp_host", form.email_smtp_host, label="邮件服务器")
    text = _set_value(text, "    smtp_port", form.email_smtp_port, label="邮件端口")
    text = _set_value(text, "    user", form.email_user, label="发件邮箱")
    text = _set_value(text, "    auth_code", form.email_auth_code, label="邮箱授权码")
    text = _set_value(text, "    to", form.email_to, label="收件邮箱")
    text = _set_value(text, "    bot_token", form.telegram_bot_token, label="机器人 token")
    text = _set_value(text, "    chat_id", form.telegram_chat_id, label="chat_id")
    text = _set_value(text, "    app_token", form.wxpusher_app_token, label="appToken")
    text = _set_value(text, "    uids",
                      json.dumps(form.wxpusher_uids, ensure_ascii=False),
                      label="WxPusher UID", raw=True)

    text = _set_value(text, "score_threshold", form.score_threshold, label="及格线",
                      append_if_missing=True)
    text = _set_value(text, "realtime_threshold", form.realtime_threshold,
                      label="实时推送线", append_if_missing=True)
    text = _set_value(text, "report_time", form.report_time, label="日报时间",
                      append_if_missing=True)
    # 缩进字段，必须长在 browser: 段里，不能补到文件末尾
    text = _set_section_value(text, "browser", "  proxy", form.browser_proxy)
    return text


def _minimal_yaml() -> str:
    """兜底底板：模板缺失时用。字段与正式模板一致，只是没有讲解注释。"""
    return (
        "# 香港岗位雷达 · 配置（由配置向导生成）\n"
        "sources:\n"
        "  jobsdb: true\n"
        "  linkedin: false\n"
        "  indeed: false\n"
        "  workday: true\n"
        "keywords: []\n"
        "graduate_keywords: []\n"
        "keyword_synonyms: {}\n"
        "career_stage: student\n"
        'location: "Hong Kong"\n'
        "poll_interval_minutes: 30\n"
        "human_delay:\n"
        "  min_s: 2\n"
        "  max_s: 8\n"
        "daily_limits:\n"
        "  jobsdb: 150\n"
        "  linkedin: 40\n"
        "  indeed: 40\n"
        "  workday: 120\n"
        "keyword_rotation:\n"
        "  per_cycle: auto\n"
        "browser:\n"
        "  headless: true\n"
        "  cloudflare_wait_seconds: 45\n"
        "llm:\n"
        '  provider: "deepseek"\n'
        '  base_url: "https://api.deepseek.com"\n'
        '  model: "deepseek-chat"\n'
        '  api_key_env: "DEEPSEEK_API_KEY"\n'
        "  timeout_seconds: 20\n"
        "score_threshold: 60\n"
        "realtime_threshold: 80\n"
        "show_unscored: true\n"
        "max_job_age_days: 2\n"
        "notify:\n"
        "  channels: []\n"
        '  wecom:\n    webhook: ""\n'
        '  email:\n    smtp_host: ""\n    smtp_port: 465\n    user: ""\n'
        '    auth_code: ""\n    to: ""\n    use_ssl: true\n'
        '  telegram:\n    bot_token: ""\n    chat_id: ""\n'
        "  wxpusher:\n    app_token: \"\"\n    uids: []\n"
        "feedback_poll_seconds: 60\n"
        'report_time: "09:00"\n'
    )


def verify_config_text(text: str, form: WizardForm) -> list[str]:
    """把渲染结果重新解析一遍，逐字段比对表单。

    这一步是「绝不写坏配置」的技术保证：正则替换是脆弱的，替换错了位置、
    吃掉了别的行，光看文本看不出来，但解析成字典一比就现形。
    """
    problems: list[str] = []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return [f"生成的配置无法解析：{exc}"]
    if not isinstance(raw, dict):
        return ["生成的配置顶层不是键值结构。"]

    def _check(label: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            problems.append(f"{label} 写入后不一致（期望 {expected!r}，实际 {actual!r}）")

    _check("关键词", raw.get("keywords") or [], form.keywords)
    _check("毕业生项目关键词", raw.get("graduate_keywords") or [], form.graduate_keywords)
    _check("求职阶段", str(raw.get("career_stage", "")), form.career_stage)
    _check("站点开关", raw.get("sources") or {}, form.sources)

    notify = raw.get("notify") or {}
    _check("推送通道", notify.get("channels") or [], [form.channel])
    wecom = notify.get("wecom") or {}
    email = notify.get("email") or {}
    telegram = notify.get("telegram") or {}
    wxpusher = notify.get("wxpusher") or {}
    _check("企业微信 webhook", str(wecom.get("webhook", "")), form.wecom_webhook)
    _check("邮件服务器", str(email.get("smtp_host", "")), form.email_smtp_host)
    _check("发件邮箱", str(email.get("user", "")), form.email_user)
    _check("邮箱授权码", str(email.get("auth_code", "")), form.email_auth_code)
    _check("机器人 token", str(telegram.get("bot_token", "")), form.telegram_bot_token)
    _check("chat_id", str(telegram.get("chat_id", "")), form.telegram_chat_id)
    _check("appToken", str(wxpusher.get("app_token", "")), form.wxpusher_app_token)
    _check("WxPusher UID", [str(x) for x in (wxpusher.get("uids") or [])],
           form.wxpusher_uids)

    _check("日报时间", str(raw.get("report_time", "")), form.report_time)
    _check("及格线", raw.get("score_threshold"), form.score_threshold)
    _check("实时推送线", raw.get("realtime_threshold"), form.realtime_threshold)
    _check("浏览器代理", str((raw.get("browser") or {}).get("proxy") or ""),
           form.browser_proxy)
    return problems


# ---------------------------------------------------------------------------
# 画像与 API key
# ---------------------------------------------------------------------------
def _replace_block_scalar(text: str, key: str, body: str) -> str:
    """替换 `key: |` 这种块状标量（用于 profile.yaml 的 background 段）。

    做法是「按行扫描」而不是写一个巨大正则：块标量里可能有空行、有缩进不同
    的行、有以 # 开头的行（在块标量里它是正文而不是注释），正则很容易误判。
    扫描规则简单可靠：从 `key:` 那一行开始，往下吃掉所有「空行」和「有缩进
    的行」，遇到第一个顶格的非空行就停。
    """
    lines = text.splitlines(keepends=True)
    start: Optional[int] = None
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            start = idx
            break
    if start is None:
        # 用户自己的 profile.yaml 里没有这一段 → 补在文件末尾
        tail = "" if text.endswith("\n") else "\n"
        return f"{text}{tail}\n{key}: |\n{_indent_block(body)}\n"

    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped and not lines[end][:1] in (" ", "\t"):
            break
        end += 1

    head = "".join(lines[:start])
    tail = "".join(lines[end:])
    return f"{head}{key}: |\n{_indent_block(body)}\n{tail}"


def _indent_block(body: str, indent: int = 2) -> str:
    """把多行文本按 YAML 块标量要求缩进（空行不缩进，否则会带出尾随空格）。

    返回值不带结尾换行 —— 调用处统一补一个，避免出现两个连续空行。
    """
    pad = " " * indent
    out = []
    for line in body.splitlines():
        out.append(f"{pad}{line}" if line.strip() else "")
    return "\n".join(out)


def write_profile(form: WizardForm) -> Optional[str]:
    """写入个人画像。返回「写了什么」的说明；没需要写则返回 None。

    只在用户真的填了内容时才动这个文件 —— 不能因为「他这次只改了关键词」
    就把已有的画像清空。
    """
    if not form.profile_background:
        return None

    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PROFILE_PATH.exists():
        try:
            text = PROFILE_PATH.read_text(encoding="utf-8-sig")
        except OSError:
            text = ""
    else:
        text = ""
        if PROFILE_TEMPLATE_PATH.exists():
            text = PROFILE_TEMPLATE_PATH.read_text(encoding="utf-8-sig")

    new_text = _replace_block_scalar(text, "background", form.profile_background)
    PROFILE_PATH.write_text(new_text, encoding="utf-8", newline="\n")
    return f"data/profiles/profile.yaml（背景 {len(form.profile_background)} 字）"


def write_api_key(form: WizardForm) -> Optional[str]:
    """把 API key 写进 api_key.txt（**不写进 config.yaml**）。

    为什么不写进配置：config.yaml 有可能被用户分享出去求助，而 api_key.txt
    在 .gitignore 里单独列了一条。凭据和配置分开存，泄露面小一半。
    """
    if not form.api_key:
        return None
    API_KEY_PATH.write_text(form.api_key.strip() + "\n", encoding="utf-8", newline="\n")
    return "api_key.txt"


# ---------------------------------------------------------------------------
# 保存流程
# ---------------------------------------------------------------------------
def _backup_config() -> Optional[str]:
    """保存前备份现有 config.yaml，文件名带秒级时间戳（永不覆盖上一份）。"""
    if not CONFIG_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG_PATH.parent / f"config.yaml.bak-{stamp}"
    # 同一秒内连点两次保存时加后缀，避免把刚做的备份覆盖掉
    suffix = 1
    while backup.exists():
        backup = CONFIG_PATH.parent / f"config.yaml.bak-{stamp}-{suffix}"
        suffix += 1
    shutil.copy2(CONFIG_PATH, backup)
    return backup.name


def _mask(value: str) -> str:
    """给 diff 用的脱敏显示：只留头尾，中间打码。"""
    if not value:
        return "（空）"
    if len(value) <= 8:
        return "已填写"
    return f"{value[:4]}…{value[-3:]}"


def _build_diff(form: WizardForm) -> list[dict[str, str]]:
    """对比「保存前 → 保存后」，生成给用户看的变更清单。

    只列真正变了的行。解密类字段（token、授权码）只显示「已填写 / 未填」，
    不显示内容 —— 页面上不该出现完整凭据，即使是在本机。
    """
    rows: list[dict[str, str]] = []
    old = None
    if CONFIG_PATH.exists():
        try:
            old = load_config(CONFIG_PATH, require_runnable=False)
        except ConfigError:
            old = None

    def add(label: str, before: str, after: str) -> None:
        if before != after:
            rows.append({"label": label, "before": before, "after": after})

    def _fmt_kw(items: list[str]) -> str:
        return "、".join(items) if items else "（空）"

    add("关键词", _fmt_kw(old.keywords) if old else "（无旧配置）",
        _fmt_kw(form.keywords))
    add("毕业生项目关键词", _fmt_kw(old.graduate_keywords) if old else "（无旧配置）",
        _fmt_kw(form.graduate_keywords))
    add("求职阶段", old.career_stage if old else "（无旧配置）", form.career_stage)
    add("启用站点",
        "、".join(old.enabled_sources()) if old else "（无旧配置）",
        "、".join(n for n, v in form.sources.items() if v) or "（无）")
    add("推送通道",
        "、".join(tier.channel_label(c) for c in (old.notify.channels if old else [])) or "（无）",
        tier.channel_label(form.channel))

    old_tg = old.notify.telegram.bot_token if old else ""
    add("Telegram token", _mask(old_tg), _mask(form.telegram_bot_token))
    old_hook = old.notify.wecom.webhook if old else ""
    add("企业微信 webhook", _mask(old_hook), _mask(form.wecom_webhook))
    old_auth = old.notify.email.auth_code if old else ""
    add("邮箱授权码", _mask(old_auth), _mask(form.email_auth_code))
    add("日报时间", old.report_time if old else "（无旧配置）", form.report_time)
    add("及格线", str(old.score_threshold) if old else "（无旧配置）",
        str(form.score_threshold))
    add("实时推送线", str(old.realtime_threshold) if old else "（无旧配置）",
        str(form.realtime_threshold))
    return rows


def apply_form(form: WizardForm) -> dict[str, Any]:
    """校验 → 渲染 → 自检 → 备份 → 写盘。返回给前端的结果字典。

    顺序是刻意安排的：**任何可能失败的动作都在备份之前完成**（渲染、解析、
    逐字段比对）。这样「失败」永远发生在文件还没被碰过的时候，用户不会遇到
    「配置没了、新配置又没写成」的最坏情况。
    """
    errors, notes = validate_form(form)
    if errors:
        return {"ok": False, "errors": errors, "notes": notes}

    try:
        text = render_config_text(form)
    except _TemplateError as exc:
        return {"ok": False, "notes": notes,
                "errors": [{"field": "_", "message":
                            f"写入中止：{exc}。你的原配置没有被改动。"}]}

    problems = verify_config_text(text, form)
    if problems:
        return {"ok": False, "notes": notes,
                "errors": [{"field": "_", "message": "写入中止，配置未改动：" + "；".join(problems)}]}

    # 用程序自己的加载器再验一次：这能抓住「字段都对、但组合起来不合法」
    # 的情况（例如及格线与实时推送线的关系、轮转取值等）。
    tmp_path = CONFIG_PATH.parent / "__config_wizard_check__.yaml"
    try:
        tmp_path.write_text(text, encoding="utf-8", newline="\n")
        load_config(tmp_path, require_runnable=True)
    except ConfigError as exc:
        return {"ok": False, "notes": notes,
                "errors": [{"field": "_", "message": f"配置校验没通过：{exc}"}]}
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    diff = _build_diff(form)
    backup_name = _backup_config()

    CONFIG_PATH.write_text(text, encoding="utf-8", newline="\n")
    written = ["config.yaml"]
    profile_note = write_profile(form)
    if profile_note:
        written.append(profile_note)
    key_note = write_api_key(form)
    if key_note:
        written.append(key_note)

    return {
        "ok": True,
        "errors": [],
        "notes": notes,
        "written": written,
        "backup": backup_name,
        "diff": diff,
    }


# ---------------------------------------------------------------------------
# 当前状态（用来给页面预填）
# ---------------------------------------------------------------------------
def current_state() -> dict[str, Any]:
    """读出当前配置，供页面初始显示。

    凭据类字段只回传布尔值（是否已配置）+ 脱敏尾巴，不回传明文：页面是给
    人看的，明文留在本地文件里就够了。用户想换就直接覆盖输入。
    """
    state: dict[str, Any] = {
        "keywords": [],
        "graduate_keywords": [],
        # 关键词是「预填的示例」还是「用户自己填的」，页面据此决定要不要显示
        # 「请改掉这些词」的提醒。空手打开向导的人需要的是起点，不是空白。
        "keywords_are_samples": False,
        "career_stage": "student",
        "sources": {n: n in WIZARD_SOURCES_DEFAULT_ON for n in WIZARD_SOURCES},
        "channel": "",
        "report_time": "09:00",
        "score_threshold": 60,
        "realtime_threshold": 80,
        "browser_proxy": "",
        "profile_background": "",
        "has_api_key": False,
        "api_key_hint": "",
        "free_max_keywords": tier.FREE_MAX_KEYWORDS,
        "free_max_channels": tier.FREE_MAX_CHANNELS,
        "is_premium": tier.is_premium(),
        "has_config": CONFIG_PATH.exists(),
        "channels": [{"name": n, "label": tier.channel_label(n)} for n in tier.VALID_CHANNELS],
    }
    try:
        cfg = load_config(CONFIG_PATH, require_runnable=False)
        state.update({
            "keywords": list(cfg.keywords),
            "graduate_keywords": list(cfg.graduate_keywords),
            "career_stage": cfg.career_stage,
            "sources": {n: bool(cfg.sources.get(n)) for n in ("jobsdb", "linkedin", "indeed")},
            "channel": (cfg.notify.channels[0] if cfg.notify.channels else ""),
            "report_time": cfg.report_time,
            "score_threshold": cfg.score_threshold,
            "realtime_threshold": cfg.realtime_threshold,
            "browser_proxy": cfg.browser_proxy,
            "email_smtp_host": cfg.notify.email.smtp_host,
            "email_smtp_port": cfg.notify.email.smtp_port,
            "email_user": cfg.notify.email.user,
            "email_to": ",".join(cfg.notify.email.to),
            "telegram_chat_id": cfg.notify.telegram.chat_id,
            "wxpusher_uids": ",".join(cfg.notify.wxpusher.uids),
            # 凭据类：只告诉页面「有没有配置」，不回传明文
            "wecom_webhook_filled": bool(cfg.notify.wecom.webhook),
            "email_auth_filled": bool(cfg.notify.email.auth_code),
            "telegram_token_filled": bool(cfg.notify.telegram.bot_token),
            "wxpusher_app_token_filled": bool(cfg.notify.wxpusher.app_token),
            "has_api_key": bool(cfg.llm.resolve_api_key()),
        })
    except (ConfigError, OSError) as exc:
        # 配置坏了也要能打开页面 —— 否则用户就失去了唯一的自助修复入口
        state["load_error"] = str(exc)

    # 关键词改用「文件里的原文」，不用 load_config 的结果。
    # 原因很重要：load_config 会按免费版额度裁到前 7 个。如果页面拿裁剪后的
    # 值做预填，用户只是改了个日报时间、一保存，剩下的关键词就被永久删掉了。
    # 页面显示全部、按原样回写，程序运行时该裁还是会裁 —— 但文件不丢东西。
    try:
        raw_cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        for key in ("keywords", "graduate_keywords"):
            value = raw_cfg.get(key)
            if isinstance(value, list):
                state[key] = [str(x).strip() for x in value if str(x).strip()]
    except (OSError, yaml.YAMLError, AttributeError):
        pass

    # 文件里一个词都没有 → 预填示例词，让用户「改」而不是「从零想」。
    if not state["keywords"]:
        state["keywords"] = list(SAMPLE_KEYWORDS)
        state["keywords_are_samples"] = True

    try:
        raw_profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8")) or {}
        background = raw_profile.get("background")
        if isinstance(background, str):
            state["profile_background"] = background.strip()
    except (OSError, yaml.YAMLError):
        pass
    return state


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class _WizardServer(ThreadingHTTPServer):
    """带令牌的 HTTP 服务：句柄上挂令牌，供每个请求比对。"""

    daemon_threads = True       # Ctrl+C 时不必等空闲连接超时

    def __init__(self, addr: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 token: str) -> None:
        super().__init__(addr, handler)
        self.token = token
        # 本次会话里有没有成功保存过配置。
        # 安装脚本靠它判断「向导关掉之后该不该接着往下走」：用户可能只是
        # 打开看了一眼就关了，这时候往下跑体检毫无意义，应当提示他回去保存。
        self.saved_ok = False


class _Handler(BaseHTTPRequestHandler):
    """极简路由器：只有 5 个端点，写成 if/elif 比引入框架更清楚。"""

    server_version = "HKRadarWizard/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 -------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003
        """屏蔽逐请求的访问日志：控制台留给「怎么用」的说明文字。"""

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- 安全门 ---------------------------------------------------------
    def _host_ok(self) -> bool:
        """只接受 127.0.0.1 / localhost 的 Host 头。

        这是防 DNS rebinding 的一道保险：恶意页面可以把域名解析到 127.0.0.1
        来打本地服务，但它的 Host 头是那个域名，不是 127.0.0.1。
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _token_of(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        return (self.headers.get(TOKEN_HEADER) or (query.get(TOKEN_QUERY) or [""])[0]).strip()

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self._token_of(), self.server.token)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求内容不是合法 JSON：{exc}") from exc
        return data if isinstance(data, dict) else {}

    # -- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:                                  # noqa: N802
        if not self._host_ok():
            self._send_json({"ok": False, "error": "只允许从本机访问。"}, 403)
            return
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            # 令牌不对（包括「根本没带」）一律拒绝，不做跳转。
            # 早先的实现是「没带令牌就 302 跳到一个带令牌的地址」，图的是用户
            # 手敲 127.0.0.1:8765 时也方便；但那等于把令牌白送出去 ——
            # 任何能发一个 GET 的页面都能顺着 Location 头拿到令牌，然后就能
            # POST 改配置。方便不值得冒这个险，所以改成提示用户回控制台复制地址。
            if not self._token_ok():
                self._send_html(_forbidden_page(), 403)
                return
            self._send_html(_load_ui(self.server.token))
            return

        if path == "/api/state":
            if not self._token_ok():
                self._send_json({"ok": False, "error": "令牌无效，请用控制台里打印的地址打开。"}, 403)
                return
            self._send_json({"ok": True, "state": current_state()})
            return

        self._send_json({"ok": False, "error": "没有这个页面。"}, 404)

    def do_POST(self) -> None:                                 # noqa: N802
        if not self._host_ok():
            self._send_json({"ok": False, "error": "只允许从本机访问。"}, 403)
            return
        if not self._token_ok():
            self._send_json({"ok": False, "error": "令牌无效，请重新打开向导。"}, 403)
            return

        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        try:
            if path == "/api/save":
                result = apply_form(prepare_form(payload))
                if result.get("ok"):
                    # 只有真的写盘成功才算「保存过」；校验失败不算，
                    # 否则安装脚本会以为配置已就绪，直接跳去体检。
                    self.server.saved_ok = True
                self._send_json(result)
                return
            if path == "/api/doctor":
                self._send_json({"ok": True, "checks": _run_doctor_checks()})
                return
            if path == "/api/validate":
                # 只校验不写盘：让用户在看 diff 之前就能知道有没有问题
                errors, notes = validate_form(prepare_form(payload))
                self._send_json({"ok": True, "errors": errors, "notes": notes})
                return
            if path == "/api/quit":
                # 从页面上关掉向导，比让用户回命令行按 Ctrl+C 顺手得多
                # （Windows 下 Ctrl+C 还会追问「终止批处理操作吗(Y/N)」）
                self._send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        except Exception as exc:      # noqa: BLE001  服务不该因为一次请求就崩
            logger.exception("配置向导处理请求失败")
            self._send_json({"ok": False, "error": f"内部错误：{exc}"}, 500)
            return

        self._send_json({"ok": False, "error": "没有这个接口。"}, 404)


def _forbidden_page() -> str:
    """令牌不对时给用户看的页面（比一行 403 有用得多）。"""
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>需要从控制台打开</title></head>"
        "<body style=\"max-width:620px;margin:80px auto;padding:0 24px;"
        "font:16px/1.9 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        "color:#1f2328\">"
        "<h2 style='font-size:19px'>这个地址打不开</h2>"
        "<p>配置向导只接受<b>带密钥的完整地址</b>，直接输 IP 和端口是不行的。</p>"
        "<p>请回到那个黑色命令行窗口，里面打印了一行这样的地址：</p>"
        "<p style=\"background:#f4f6f9;border:1px solid #e3e7ee;border-radius:8px;"
        "padding:10px 12px;font-family:ui-monospace,Menlo,Consolas,monospace;"
        "font-size:13.5px;word-break:break-all\">"
        "http://127.0.0.1:8765/?t=<span style='color:#6b7280'>一串随机字符</span></p>"
        "<p style='color:#6b7280;font-size:14px'>把<b>完整的一行</b>复制到浏览器地址栏"
        "回车即可。如果那个窗口已经关了，重新双击 setup.bat。</p>"
        "</body></html>"
    )


def _run_doctor_checks() -> list[dict[str, str]]:
    """在页面上跑一遍体检。

    用 llm_test=False：体检按钮是「随时点一下看看」的小动作，不该每次都
    真发一个联网请求。
    """
    from hk_job_radar import doctor      # 延迟导入：doctor 依赖本模块的同级常量

    checks = doctor.run_checks(llm_test=False)
    return [
        {"group": c.group, "name": c.name, "status": c.status,
         "detail": c.detail, "hint": c.hint}
        for c in checks
    ]


def _load_ui(token: str) -> str:
    """读页面文件并注入令牌。"""
    try:
        html = UI_PATH.read_text(encoding="utf-8")
    except OSError:
        return ("<h1>页面文件缺失</h1>"
                "<p>找不到 setup_ui.html —— 安装包可能不完整，请重新解压。</p>")
    return html.replace("__WIZARD_TOKEN__", token)


def _bind_server(start: int = PORT_START, end: int = PORT_END) -> _WizardServer:
    """从 start 开始找一个能绑定的端口。

    为什么要扫描：8765 这类常用端口很容易被别的开发工具占着。报一个
    「Address already in use」给用户看，他既看不懂也不知道怎么改。
    """
    token = secrets.token_urlsafe(16)
    last_error: Optional[OSError] = None
    for port in range(start, end + 1):
        try:
            return _WizardServer(("127.0.0.1", port), _Handler, token)
        except OSError as exc:
            last_error = exc
            continue
    raise ConfigError(
        f"找不到可用端口（已试 {start}-{end}）。请关掉占用端口的程序，"
        f"或用 python run.py setup --port 8900 指定一个。"
    ) from last_error


def _open_browser_later(url: str, delay: float = 0.7) -> None:
    """稍等再打开浏览器：先让 serve_forever() 起来，避免「打开得太快连不上」。"""

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:      # noqa: BLE001  没有图形界面时静默跳过
            pass

    threading.Timer(delay, _open).start()


#: 向导被关掉、但这次会话里一次都没保存成功时的退出码。
#: 单独占一个值（而不是笼统的 1），是为了让安装脚本能分清两种情况：
#:   「用户还没保存，该提醒他回去保存」 vs 「向导本身起不来，该报错」。
EXIT_UNSAVED = 3


def run_wizard(*, port: Optional[int] = None, open_browser: bool = True,
               chained: bool = False) -> int:
    """启动向导，阻塞直到用户关掉它。返回进程退出码。

    返回码：
      0  配置已保存（本次会话里至少成功写盘一次）
      1  页面文件缺失
      2  端口用不了
      3  向导关了，但一次都没保存过 —— 见 EXIT_UNSAVED

    `chained` 为真表示本向导是被安装脚本串起来的一段（install.bat /
    install.command 自动打开）。此时收尾的提示语要说「我接着往下走」，
    而不是「你自己去双击下一个脚本」——用户此刻正在等下一步发生。
    """
    if not UI_PATH.exists():
        print(f"❌ 页面文件缺失：{UI_PATH}")
        return 1

    try:
        server = _bind_server()
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    if port is not None and port != server.server_port:
        # 用户指定了端口：关掉刚才探到的，改用指定的
        server.server_close()
        token = secrets.token_urlsafe(16)
        try:
            server = _WizardServer(("127.0.0.1", port), _Handler, token)
        except OSError:
            print(f"❌ 端口 {port} 用不了（可能被别的程序占着）。换个端口再试。")
            return 2

    url = f"http://127.0.0.1:{server.server_port}/?{TOKEN_QUERY}={server.token}"
    if chained:
        closing_hint = [
            "  在这个页面里填完，点最下面的「保存配置」。",
            "  保存好之后，点「关闭向导」—— 我会接着往下做体检，",
            "  你不用再双击任何东西。",
        ]
    else:
        closing_hint = [
            "  在这个页面里填完，点最下面的「保存配置」。",
            "  保存好之后，点「关闭向导」（或回这个窗口按 Ctrl + C），",
            "  然后双击 start.bat（Mac 上是 start.command）开始找工作。",
        ]

    print()
    print("=" * 62)
    print("  香港岗位雷达 · 配置向导")
    print("=" * 62)
    print()
    print("  浏览器地址（没自动打开就把这一行复制到浏览器地址栏）：")
    print(f"    {url}")
    print()
    for line in closing_hint:
        print(line)
    print()
    print("-" * 62)
    print("  这个页面只在你本机能打开，同一个 WiFi 下的其他电脑连不上。")
    print("  窗口关掉之后，地址就失效了。")
    print("-" * 62)
    print(flush=True)      # 立刻刷出来：输出被重定向到文件时也不会「看着像卡住」

    if open_browser:
        _open_browser_later(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass                  # 下面统一按 saved_ok 判断该说什么
    finally:
        server.shutdown()
        server.server_close()

    if not server.saved_ok:
        print("\n（这次没有保存任何设置。）")
        return EXIT_UNSAVED
    print("\n✅ 配置已保存。")
    return 0


__all__ = [
    "WizardForm", "form_from_payload", "prepare_form", "validate_form",
    "render_config_text", "verify_config_text", "apply_form", "current_state",
    "run_wizard", "EXIT_UNSAVED", "UI_PATH", "PORT_START", "PORT_END",
]
