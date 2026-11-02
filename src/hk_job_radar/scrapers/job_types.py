"""岗位类型 → 各站 URL 过滤参数的动态映射（合规：学生签证只抓实习/兼职）。

为什么单独一个模块：三个站点对"岗位类型"的 URL 表达完全不同——
- LinkedIn 用 `f_JT` 字母码（I/P/F/C/T/V）；
- Indeed 用新版统一过滤器 `sc=0kf:jt(...);`（`jt` 单值参数无法表达"多类"）；
- JobsDB（SEEK 平台）用 `worktype` 数字码（242/243/244/245）。

把映射集中在这里，抓取器只调用对应的 builder。映射是"从 config.job_types
动态生成"的——支持任意组合，不写死"只支持实习+兼职"；但默认配置即
实习+兼职，full time 永不默认加入（加入时 config.py 会打 WARNING 提醒）。
"""
from __future__ import annotations

from urllib.parse import quote

# ---------------------------------------------------------------------------
# 通用别名 → 规范名。config.yaml 里用户可能写 part-time / parttime / full-time
# / intern 等各种写法，先归一化再查各站映射表，保证大小写/连字符/同义词都能命中。
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    "intern": "internship",
    "interns": "internship",
    "internship": "internship",
    "internships": "internship",
    "part-time": "part time",
    "parttime": "part time",
    "part time": "part time",
    "full-time": "full time",
    "fulltime": "full time",
    "full time": "full time",
    "permanent": "full time",   # 香港语境 permanent 通常=全职长工
    "contract": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "volunteer": "volunteer",
    "casual": "casual",
    "vacation": "casual",
    "casual/vacation": "casual",
}


def normalize_job_types(job_types: list[str]) -> list[str]:
    """归一化并去重（保持 config 顺序）；未知类型原样保留，由调用方决定取舍。"""
    result: list[str] = []
    for jt in job_types:
        key = (jt or "").strip().lower()
        canon = _ALIASES.get(key, key)
        if canon and canon not in result:
            result.append(canon)
    return result


# ---------------------------------------------------------------------------
# LinkedIn：f_JT 参数。岗位类型首字母码：
#   F=full-time  P=part-time  C=contract  T=temporary  I=internship  V=volunteer
# 多选用逗号分隔，逗号需 URL 编码为 %2C（即 f_JT=I%2CP）。
# ---------------------------------------------------------------------------
LINKEDIN_JT_MAP: dict[str, str] = {
    "internship": "I",
    "part time": "P",
    "full time": "F",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
}


def build_linkedin_f_jt(job_types: list[str]) -> str:
    """返回 f_JT 的 URL 参数值（已编码逗号），如 'I%2CP'；无可用类型返回 ''。"""
    codes = [LINKEDIN_JT_MAP[t] for t in normalize_job_types(job_types)
             if t in LINKEDIN_JT_MAP]
    return quote(",".join(codes), safe="") if codes else ""


# ---------------------------------------------------------------------------
# Indeed：`jt` 单值参数只能表达一种岗位类型，多类必须用新版过滤器
# `sc=0kf:jt(a,b);`。例：实习+兼职 → sc=0kf%3Ajt%28internship%2Cparttime%29%3B
# ---------------------------------------------------------------------------
INDEED_JT_MAP: dict[str, str] = {
    "internship": "internship",
    "part time": "parttime",
    "full time": "fulltime",
    "contract": "contract",
    "temporary": "temporary",
}


def build_indeed_sc(job_types: list[str]) -> str:
    """返回 Indeed sc 过滤器的完整参数（含 'sc=' 前缀与 URL 编码）。

    例：build_indeed_sc(["internship", "part time"])
        → 'sc=0kf%3Ajt%28internship%2Cparttime%29%3B'
    无可用类型返回 ''（调用方据此跳过该过滤参数）。
    """
    values = [INDEED_JT_MAP[t] for t in normalize_job_types(job_types)
              if t in INDEED_JT_MAP]
    if not values:
        return ""
    sc = f"0kf:jt({','.join(values)});"
    return "sc=" + quote(sc, safe="")


# ---------------------------------------------------------------------------
# JobsDB（SEEK 平台）：worktype 数字码。
#   242 = full time   243 = part time   244 = contract/temp   245 = casual/vacation
# 注意：JobsDB/SEEK 没有独立的 "internship" 工作类型（UI 只有四个桶），
# 实习岗位通常被归入 part time(243) 或 casual/vacation(245) 这两个"非全职"桶。
# 因此把 internship 映射为 243+245，尽量覆盖实习、同时避开 full time(242)。
# ---------------------------------------------------------------------------
JOBSDB_WORKTYPE_MAP: dict[str, str | tuple[str, ...]] = {
    "full time": "242",
    "part time": "243",
    "contract": "244",
    "temporary": "244",
    "casual": "245",
    "internship": ("243", "245"),
}


def build_jobsdb_worktype(job_types: list[str]) -> str:
    """返回 worktype 参数值（逗号分隔、已编码），如 '243%2C245'；空返回 ''。"""
    codes: list[str] = []
    for t in normalize_job_types(job_types):
        mapped = JOBSDB_WORKTYPE_MAP.get(t)
        if not mapped:
            continue
        for code in (mapped if isinstance(mapped, tuple) else (mapped,)):
            if code not in codes:
                codes.append(code)
    return quote(",".join(codes), safe="") if codes else ""


# ---------------------------------------------------------------------------
# 反向：从「卡片上印的类型标签」读回岗位类型。
#
# 为什么需要：URL 过滤只能表达「我要搜索什么」，不能证明「抓回来的这条就是」。
# 三个站点都不把类型放进结构化字段，但 JobsDB 的卡片会把 "Part time" 单独
# 印在标题下方——那是站点自己的标注，比我们从标题里猜可靠得多。
#
# ★ 必须逐行比对，不能对整段文字做子串匹配：标题里本身就可能写着
#   "Part-time Data Analyst"，那是岗位名字的一部分，不是站点的类型标签。
# ---------------------------------------------------------------------------
_SITE_TYPE_LABELS: dict[str, str] = {
    "internship": "internship",
    "intern": "internship",
    "part time": "part time",
    "part-time": "part time",
    "parttime": "part time",
    "full time": "full time",
    "full-time": "full time",
    "fulltime": "full time",
    "permanent": "full time",     # 香港语境 permanent 通常=全职长工
    "contract/temp": "contract",
    "contract": "contract",
    "temporary": "temporary",
    "casual": "casual",
    "casual/vacation": "casual",
}

#: 同一张卡片可能同时印多个类型（如 "Part time" + "Contract/Temp"）。
#: 按这个顺序取第一个——实习/兼职优先，因为它们是「合法可投」的那一侧。
_SITE_TYPE_PRIORITY: tuple[str, ...] = (
    "internship", "part time", "full time", "contract", "temporary", "casual",
)


#: JobsDB 把类型印成一句话：「This is a Full time job」/「This is a Contract/Temp job」，
#: 而不是独立的「Full time」。只剥离这一个固定句式，**绝不做子串匹配**——
#: 标题里的 "Part-time Data Analyst" 是岗位名字的一部分，不是站点标注。
_TYPE_SENTENCE_PREFIX = "this is a "
_TYPE_SENTENCE_SUFFIX = " job"


def extract_site_job_type(text: str) -> str:
    """从岗位卡片全文里提取站点标注的岗位类型。

    返回 'internship' | 'part time' | 'full time' | 'contract' | 'temporary'
    | 'casual' | ''（卡片上没有任何类型标签）。

    ★ 必须**逐行**比对，不能对整段文字做子串匹配：标题本身就可能写着
      "Part-time Data Analyst"，那是岗位名字的一部分，不是站点标签。
    ★ JobsDB 的类型印在「This is a X job」这句话里（实测 2026-10 页面）——
      不剥离这个句式的话，JobsDB 的站点标签**永远提取为空**，整个类型判定
      会退化成「靠标题猜」。这里只认这一个固定前后缀，其余原样逐行比对。

    ★ 返回 'contract' / 'temporary' / 'casual' 时由调用方决定是否放行
      （见 AppConfig.allowed_job_type_classes），本函数不做合规判断。
    """
    found: set[str] = set()
    for line in (text or "").splitlines():
        key = " ".join(line.strip().lower().split())   # 归一空白，去首尾
        mapped = _SITE_TYPE_LABELS.get(key)
        if mapped is None and key.startswith(_TYPE_SENTENCE_PREFIX) \
                and key.endswith(_TYPE_SENTENCE_SUFFIX):
            inner = key[len(_TYPE_SENTENCE_PREFIX):-len(_TYPE_SENTENCE_SUFFIX)]
            mapped = _SITE_TYPE_LABELS.get(inner.strip())
        if mapped:
            found.add(mapped)
    for kind in _SITE_TYPE_PRIORITY:
        if kind in found:
            return kind
    return ""
