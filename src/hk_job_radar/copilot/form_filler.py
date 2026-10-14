"""申请表单自动填充（T05，P1 核心）。

设计取舍（为什么"只填不交"）：
自动提交申请既违反各平台条款（LinkedIn 协议 8.2 明文禁止 bots），也容易
因表单字段误判而发出错误答案（例如把 visa 问题答反）。因此本模块只做
"把能确定的字段填好 + 把不确定的字段高亮给你看"，最后一下提交必须由你
亲手点击——这是产品的合规红线，也是对你求职结果负责。

填充策略（三层，与 QAEngine 一致）：
1. 标准字段（姓名/邮箱/电话/所在地/LinkedIn/学历/现职）→ 直接查资料库填充；
2. 下拉/单选 → 语义匹配选项文案后再选（选不中就不动，交给你）；
3. 开放式问题 → 交 QAEngine（规则 → LLM → 留空），答案一律标注来源。

风险字段（如"是否需要签证担保"这类是/否按钮、复选框）刻意不自动处理：
这类问题答错的代价极高，必须人工确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

from hk_job_radar.config import DATA_DIR
from hk_job_radar.models import JobPosting

# 平台识别：不同 ATS 的表单结构差异大，先识别再决定填充策略
_PLATFORM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("workday", re.compile(r"myworkdayjobs\.com|workday\.com", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io|boards\.greenhouse", re.I)),
    ("lever", re.compile(r"lever\.co|jobs\.lever", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com", re.I)),
    ("jobsdb", re.compile(r"jobsdb\.com", re.I)),
]

# 字段关键词 → 取值来源。顺序敏感：更具体的规则放前面，
# 否则 "name" 会把 "company name" 也吃掉。
_FIELD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(first\s*name|given\s*name|名字|名)", re.I), "first_name"),
    (re.compile(r"(last\s*name|family\s*name|surname|姓氏|姓)", re.I), "last_name"),
    (re.compile(r"(e-?mail|郵箱|邮箱)", re.I), "email"),
    (re.compile(r"(phone|mobile|tel|contact\s*number|電話|电话)", re.I), "phone"),
    (re.compile(r"(linkedin|profile\s*url|social)", re.I), "linkedin"),
    (re.compile(r"(city|location|address|所在|地區|地区)", re.I), "location"),
    (re.compile(r"(university|school|college|education|院校|學校|学校)", re.I), "education"),
    (re.compile(r"(current\s*company|employer|现任公司|目前公司)", re.I), "company"),
    (re.compile(r"(salary|compensation|薪資|薪资)", re.I), "expected_salary"),
    (re.compile(r"(visa|sponsorship|work\s*author|簽證|签证)", re.I), "visa_sponsorship"),
    (re.compile(r"(start\s*date|availability|available\s*from|notice)", re.I), "availability"),
    (re.compile(r"^\s*(full\s*)?name\s*$|full\s*name|your\s*name|姓名", re.I), "full_name"),
]

# 不自动处理的控件类型：答错代价太高，一律留给人工
_SKIP_INPUT_TYPES = {"checkbox", "radio", "file", "hidden", "submit", "button"}


@dataclass
class FilledField:
    """一个已填充字段的记录（供确认关卡逐条展示）。"""

    label: str          # 页面上看到的字段名
    value: str          # 填入的值
    source: str         # profile / rule / ai / manual
    selector: str = ""  # 便于编辑时回填


@dataclass
class FillResult:
    """一次填充的完整结果。"""

    job: JobPosting
    platform: str = "other"
    fields: list[FilledField] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # 未处理的控件说明
    resume_path: str = ""
    screenshot_path: str = ""


class FormFiller:
    """申请表单填充器。"""

    def __init__(self, config, browser, profile_store, qa_engine) -> None:
        self.config = config
        self.browser = browser
        self.store = profile_store              # ProfileStore：负责挑简历
        self.profile = profile_store.load()     # UserProfile：负责取值
        self.qa = qa_engine

    # ------------------------------------------------------------------
    @staticmethod
    def detect_platform(url: str) -> str:
        """按 URL 域名识别 ATS 平台（填充策略的分派依据）。"""
        for name, pattern in _PLATFORM_PATTERNS:
            if pattern.search(url or ""):
                return name
        return "other"

    # ------------------------------------------------------------------
    def fill(self, job: JobPosting) -> FillResult:
        """打开申请页 → 填充 → 返回结果（不提交）。"""
        page = self.browser.launch("apply", force_headless=False)  # 必须看得见
        result = FillResult(job=job, platform=self.detect_platform(job.url))

        logger.info(f"[副驾] 打开申请页：{job.url}（平台：{result.platform}）")
        page.goto(job.url, wait_until="domcontentloaded", timeout=60000)
        self.browser.wait_cloudflare(page)
        page.wait_for_load_state("networkidle", timeout=30000)
        self.browser.human_delay(3, 6)

        self._fill_standard_fields(page, result)
        self._upload_resume(page, job, result)
        result.screenshot_path = self._screenshot(page)
        return result

    # ------------------------------------------------------------------
    def _fill_standard_fields(self, page, result: FillResult) -> None:
        """遍历可见控件，按语义规则填充。"""
        controls = page.locator(
            "input:visible, select:visible, textarea:visible"
        ).all()
        logger.debug(f"[副驾] 页面上可见控件 {len(controls)} 个")

        for idx, ctrl in enumerate(controls):
            try:
                tag = self._tag_name(page, ctrl, idx)
                itype = (ctrl.get_attribute("type") or "").lower()
                if tag == "textarea":
                    self._handle_open_question(ctrl, result, idx)
                    continue
                if itype in _SKIP_INPUT_TYPES:
                    label = self._field_label(page, ctrl, idx)
                    result.skipped.append(f"{label}（{itype}，需你手动选择）")
                    continue
                if tag == "select":
                    self._handle_select(ctrl, result, idx)
                    continue

                label = self._field_label(page, ctrl, idx)
                key = self._match_key(label)
                if not key:
                    # 文本框但没匹配到标准字段 → 当作开放问题处理
                    self._handle_open_question(ctrl, result, idx)
                    continue

                value, source = self._value_for(key)
                if not value:
                    continue
                ctrl.fill(value)
                result.fields.append(FilledField(label=label, value=value,
                                                 source=source, selector=f"#{idx}"))
            except Exception as exc:
                logger.debug(f"[副驾] 控件 {idx} 填充失败（跳过）：{exc}")

    # ------------------------------------------------------------------
    def _handle_open_question(self, ctrl, result: FillResult, idx: int) -> None:
        """开放式问题：交给 QAEngine，答案标注来源后填入页面。"""
        label = self._field_label(page=None, ctrl=ctrl, idx=idx)
        if len(label.strip()) < 3:
            return
        answer, source = self.qa.answer(label)
        if not answer:
            result.skipped.append(f"{label}（开放问题，未生成答案，需你手填）")
            return
        try:
            ctrl.fill(answer)
        except Exception as exc:
            logger.debug(f"[副驾] 开放问题填充失败：{exc}")
            return
        result.fields.append(FilledField(label=label, value=answer,
                                         source=source or "manual", selector=f"#{idx}"))

    def _handle_select(self, ctrl, result: FillResult, idx: int) -> None:
        """下拉框：按资料值做语义匹配；匹配不到就留给人工（不乱选）。"""
        label = self._field_label(page=None, ctrl=ctrl, idx=idx)
        key = self._match_key(label)
        value, source = self._value_for(key) if key else ("", "")
        if not value:
            result.skipped.append(f"{label}（下拉框，未匹配，需你手动选择）")
            return
        try:
            ctrl.select_option(label=re.sub(r"\s+", " ", value))
            result.fields.append(FilledField(label=label, value=value,
                                             source=source or "profile", selector=f"#{idx}"))
        except Exception:
            result.skipped.append(f"{label}（下拉框无匹配选项，需你手动选择）")

    # ------------------------------------------------------------------
    def _upload_resume(self, page, job: JobPosting, result: FillResult) -> None:
        """上传简历：按岗位挑选版本后填入 file input。"""
        resume_name = self.store.pick_resume(job.title)
        if not resume_name:
            result.skipped.append("简历上传（未找到简历文件，请放入 data/resumes/）")
            return
        path = DATA_DIR / "resumes" / resume_name
        file_inputs = page.locator("input[type=file]").all()
        if not file_inputs:
            result.skipped.append("简历上传（页面未找到文件上传控件，可能需先点申请按钮）")
            return
        try:
            file_inputs[0].set_input_files(str(path))
            result.resume_path = str(path)
            result.fields.append(FilledField(label="简历文件", value=resume_name,
                                             source="profile"))
            logger.info(f"[副驾] 已上传简历：{resume_name}")
        except Exception as exc:
            logger.warning(f"[副驾] 简历上传失败：{exc}")
            result.skipped.append(f"简历上传失败（{exc}）")

    # ------------------------------------------------------------------
    def _screenshot(self, page) -> str:
        """保存填充后的页面截图，便于你核对与留档。"""
        try:
            path = DATA_DIR / "screenshots"
            path.mkdir(parents=True, exist_ok=True)
            file = path / "apply_latest.png"
            page.screenshot(path=str(file), full_page=True)
            return str(file)
        except Exception as exc:
            logger.debug(f"[副驾] 截图失败（忽略）：{exc}")
            return ""

    # ------------------------------------------------------------------
    # 小工具：控件识别与取值
    # ------------------------------------------------------------------
    @staticmethod
    def _tag_name(page, ctrl, idx: int) -> str:
        try:
            return (ctrl.evaluate("el => el.tagName.toLowerCase()") or "").strip()
        except Exception:
            return "input"

    @staticmethod
    def _field_label(page, ctrl, idx: int) -> str:
        """综合 placeholder / name / id / aria-label / 邻近 label 文案
        推断字段名（三选其优，尽量还原"人眼看到的标签"）。"""
        try:
            text = ctrl.evaluate("""el => {
                const parts = [];
                if (el.placeholder) parts.push(el.placeholder);
                if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label'));
                if (el.name) parts.push(el.name);
                if (el.id) {
                    const lab = document.querySelector(`label[for="${el.id}"]`);
                    if (lab && lab.innerText) parts.push(lab.innerText);
                }
                const closest = el.closest('label');
                if (closest && closest.innerText) parts.push(closest.innerText);
                const row = el.closest('[class*="field"], [class*="row"], li');
                if (row && row.innerText && row.innerText.length < 120) parts.push(row.innerText);
                return parts.join(' | ');
            }""")
            return re.sub(r"\s+", " ", (text or "")).strip()[:160]
        except Exception:
            return ""

    @staticmethod
    def _match_key(label: str) -> Optional[str]:
        """字段名 → 资料键。"""
        if not label:
            return None
        for pattern, key in _FIELD_RULES:
            if pattern.search(label):
                return key
        return None

    def _value_for(self, key: str) -> tuple[str, str]:
        """资料键 → (值, 来源)。"""
        p = self.profile
        if key == "full_name":
            return p.name, "profile"
        if key == "first_name":
            # 英文习惯：取第一个词（如 "Alex Chan" → "Alex"）
            return (p.name.split()[0] if p.name else ""), "profile"
        if key == "last_name":
            parts = p.name.split()
            return (parts[-1] if len(parts) > 1 else ""), "profile"
        if key == "email":
            return p.email, "profile"
        if key == "phone":
            return p.phone, "profile"
        if key == "linkedin":
            return str(p.basic.get("linkedin") or ""), "profile"
        if key == "location":
            return p.location, "profile"
        if key == "education":
            if p.education:
                edu = p.education[0]
                return f"{edu.get('school','')} — {edu.get('degree','')}".strip(" —"), "profile"
            return "", ""
        if key == "company":
            if p.work_experience:
                return str(p.work_experience[0].get("company") or ""), "profile"
            return "", ""
        # 以下几类走 screening_rules（口径统一，避免和打分画像说两套话）
        rule_map = {"expected_salary": "expected_salary",
                    "visa_sponsorship": "visa_sponsorship",
                    "availability": "availability"}
        if key in rule_map:
            return p.rule(rule_map[key]), "rule"
        return "", ""
