"""资料库读写与简历版本选择（T05）。

为什么资料单独放 data/profiles/profile.yaml 而不是写进代码：
1. 简历内容会变（换了实习、考了证），配置与代码分离才能"改配置不改代码"；
2. profile.yaml 的 background 段同时是 LLM 打分画像，一份文件两处复用，
   避免"打分画像"与"填表资料"说两套口径（例如签证状态）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger

from hk_job_radar.config import DATA_DIR, PROFILE_PATH

# 简历目录：放多版本英文简历（PDF/DOCX），按文件名语义自动挑选
RESUMES_DIR = DATA_DIR / "resumes"

# 岗位方向 → 简历文件名关键词（用于按岗位挑最合适的简历版本）
# ⚠️ 下面这两张表（_ROLE_RESUME_HINTS / _ROLE_PATTERNS）是【可自定义的默认示例】。
#    它们决定「投递副驾」如何把岗位归入哪个方向、进而挑哪份简历。
#    请按你自己的求职方向修改（同时保持两张表的方向键一致）；
#    如果你的目标方向与默认不同却没改，副驾会挑错简历版本。
_ROLE_RESUME_HINTS: dict[str, tuple[str, ...]] = {
    "quant": ("quant", "trading", "finance"),
    "data_science": ("data_scien", "ds", "ml", "machine"),
    "data_analyst": ("data_analyst", "da", "analyst"),
    "business_analyst": ("business", "ba", "bi"),
    "data_engineer": ("data_eng", "de", "engineer"),
    "mt": ("mt", "management_trainee", "graduate"),
}

# 岗位标题 → 岗位方向的判定规则（顺序敏感：先匹配到的优先）
_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("quant", re.compile(r"quant\w*|trading|trader|proprietary", re.I)),
    ("mt", re.compile(r"management\s+trainee|\bMT\b|graduate\s+program|graduate\s+trainee", re.I)),
    ("data_engineer", re.compile(r"data\s+engineer|big\s+data|etl|data\s+platform", re.I)),
    ("data_science", re.compile(r"data\s+scientist|machine\s+learning|\bML\b|\bAI\b|deep\s+learning", re.I)),
    ("business_analyst", re.compile(r"business\s+analyst|\bBI\b|commercial\s+analyst", re.I)),
    ("data_analyst", re.compile(r"data\s+analyst|analytics|reporting", re.I)),
]


@dataclass
class UserProfile:
    """结构化的个人资料（profile.yaml 的镜像）。"""

    basic: dict = field(default_factory=dict)
    education: list[dict] = field(default_factory=list)
    work_experience: list[dict] = field(default_factory=list)
    work_authorization: dict = field(default_factory=dict)
    job_preferences: dict = field(default_factory=dict)
    screening_rules: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    # 常用字段的快捷访问（填表时高频使用）
    @property
    def name(self) -> str:
        return str(self.basic.get("name_en") or self.basic.get("name_zh") or "")

    @property
    def email(self) -> str:
        return str(self.basic.get("email") or "")

    @property
    def phone(self) -> str:
        return str(self.basic.get("phone") or "")

    @property
    def location(self) -> str:
        return str(self.basic.get("location") or "Hong Kong")

    def rule(self, key: str) -> str:
        """取筛选问题答题口径（screening_rules 段）。"""
        return str(self.screening_rules.get(key) or "").strip()


class ProfileStore:
    """资料库读写 + 简历版本选择。"""

    def __init__(self, path: Path | None = None, resumes_dir: Path | None = None) -> None:
        self.path = path or PROFILE_PATH
        self.resumes_dir = resumes_dir or RESUMES_DIR

    # ------------------------------------------------------------------
    def load(self) -> UserProfile:
        """读取 profile.yaml → UserProfile。文件缺失/格式错误返回空画像
        （填表时缺什么就留空让你手填，而不是直接崩掉）。"""
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.warning(f"未找到 {self.path}，投递副驾将缺少预填资料。")
            return UserProfile()
        except yaml.YAMLError as exc:
            logger.warning(f"profile.yaml 解析失败（使用空画像）：{exc}")
            return UserProfile()
        if not isinstance(data, dict):
            return UserProfile()

        def _dict(key: str) -> dict:
            value = data.get(key) or {}
            return value if isinstance(value, dict) else {}

        def _list(key: str) -> list[dict]:
            value = data.get(key) or []
            return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []

        return UserProfile(
            basic=_dict("basic"),
            education=_list("education"),
            work_experience=_list("work_experience"),
            work_authorization=_dict("work_authorization"),
            job_preferences=_dict("job_preferences"),
            screening_rules=_dict("screening_rules"),
            raw=data,
        )

    # ------------------------------------------------------------------
    def list_resumes(self) -> list[Path]:
        """列出 data/resumes/ 下的简历文件（PDF/DOCX）。"""
        if not self.resumes_dir.exists():
            return []
        return sorted(p for p in self.resumes_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in (".pdf", ".doc", ".docx"))

    def pick_resume(self, job_title: str = "") -> str:
        """按岗位标题挑选最合适的简历版本，返回文件名。

        策略：先用岗位方向匹配文件名关键词；匹配不到就返回第一份（通用版）。
        没有任何简历文件时返回空串，由调用方提示用户放入简历。
        """
        files = self.list_resumes()
        if not files:
            return ""

        role = None
        for name, pattern in _ROLE_PATTERNS:
            if pattern.search(job_title or ""):
                role = name
                break

        if role:
            hints = _ROLE_RESUME_HINTS.get(role, ())
            for path in files:
                low = path.name.lower()
                if any(h in low for h in hints):
                    logger.info(f"简历匹配：{job_title!r} → {path.name}")
                    return path.name
        logger.info(f"简历匹配：未命中方向（{job_title!r}），使用 {files[0].name}")
        return files[0].name
