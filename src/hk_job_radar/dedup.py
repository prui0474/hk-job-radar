"""跨站重复岗位识别。

要解决的问题
------------
同一个岗位常常同时挂在 JobsDB、LinkedIn、Indeed、企业官网上。本程序现有的
去重只看「站点 + 站内 ID」和「完整 URL」，这两条对跨站完全无效——同一个岗位
在四个站就是四条记录，日报里推四次，用户要自己发现「这几条其实是同一个」。

怎么判定「同一个岗位」
--------------------
拿「公司名 + 职位名」做指纹，两边都先规范化再比较：

    Hang Seng Bank Limited / Data Analyst (Ref: 2024-1234)
    Hang Seng Bank         / Data Analyst
    → 都归一成 hang seng bank / data analyst → 判为同一岗位

★ 只做**完全相等**，不做模糊匹配。相似度 90% 的两个职位名，很可能真的是
  两个岗位（「Data Analyst - Risk」和「Data Analyst - Credit」差得更少，
  但它们是两个团队在招人）。宁可漏掉一些重复，也不要误合并。

为什么不直接删掉重复的那条
--------------------------
同一家公司可以同时招两个「Data Analyst」，分属不同部门。程序分不清
「一个岗位挂在两个站」和「真的有两个岗位」，删错了用户就永远看不到那个
机会——这比推重复严重得多。

所以这里只产出**标记**：日报上写「♻️ 同一岗位另见 #3」，两个链接都还在，
点哪个由用户自己判断。唯一会主动跳过的是实时推送——那个场景下用户刚在
十分钟前收到过同一条，再推一次纯属骚扰。

已知覆盖不到的情况（这是设计取舍，不是遗漏）
------------------------------------------
1. 中英文公司名：`恒生银行` 与 `Hang Seng Bank` 规范化后不同，不会被识别。
   要解决它得维护一张公司名对照表，每家都要人工确认，收益不成比例。
2. 招聘中介：JobsDB 上大量岗位的公司名是猎头（如 `Robert Walters`），
   同一岗位经不同中介发布，公司名天然不同。
3. 职位名后带部门/团队名的：只有括号和编号会被剥掉，`Data Analyst - Risk`
   与 `Data Analyst` 视为两个岗位（见上面的理由）。
"""
from __future__ import annotations

import re
import unicodedata

from hk_job_radar.models import JobPosting

#: 算不出指纹时写入的哨兵值。
#: 为什么不用空串：空串是「还没算过」，回填逻辑每次启动都会重算一遍；
#: 缺公司名或职位名的岗位永远算不出来，用一个显式的值把它标记成「已知无法判断」。
NO_KEY = "-"

#: 公司名末尾的法律实体后缀。规范化时剥掉，否则
#: `Hang Seng Bank` 和 `Hang Seng Bank Limited` 会被当成两家公司。
_COMPANY_SUFFIXES = frozenset({
    "limited", "ltd", "company", "co", "corporation", "corp",
    "incorporated", "inc", "plc", "llc", "llp", "pte",
    "holdings", "holding", "group", "enterprises", "enterprise",
})

#: 职位名里的编号标记。这些东西区分的是「一次招聘流程」，不是「一个岗位」，
#: 剥掉才能让同一个岗位在四个站上撞在一起。
_REF_PATTERN = re.compile(
    r"\b(?:ref|reference|job\s*id|job\s*no|req(?:uisition)?(?:\s*id)?|position\s*id)"
    r"\b\s*[:#.]?\s*[\w./-]+",
    re.IGNORECASE,
)

#: 括号内容：`(Ref: 123)`、`（6 个月合约）`、`[Contract]`。
#: 合约期属于「这一批招聘」的属性，同一岗位换个合约长度还是同一岗位。
_BRACKET_PATTERN = re.compile(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]")

#: 不是字母也不是数字的字符一律当分隔符。中文字符属于 \w，不受影响。
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def _fold(text: str) -> str:
    """统一大小写与全半角，再把标点当空格。

    NFKC 是必须的：招聘网站里全角括号、全角字母很常见（`ＤＡＴＡ`），
    不归一化会让同一个词算成两个。
    """
    folded = unicodedata.normalize("NFKC", text or "").lower()
    # `&` 在英文公司名里等于 `and`，两边都写成 and 才能对上
    folded = folded.replace("&", " and ")
    return " ".join(_NON_WORD.sub(" ", folded).split())


def normalize_company(name: str) -> str:
    """公司名归一化：去法律后缀、去标点、统一大小写。

    例：`Hang Seng Bank Limited` / `HANG SENG BANK` / `Hang Seng Bank, Ltd.`
        → 三者在函数内都是 `hang seng bank`。
    """
    tokens = _fold(name).split()
    # 反复剥：`xxx co limited` 这种叠着写两个后缀的也要剥干净
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_title(title: str) -> str:
    """职位名归一化：剥括号内容与招聘编号，其余原样保留。

    不剥「- Risk」「/ Credit」这类部门后缀，因为它们区分的是**不同岗位**。
    """
    text = unicodedata.normalize("NFKC", title or "")
    text = _REF_PATTERN.sub(" ", text)
    text = _BRACKET_PATTERN.sub(" ", text)
    return _fold(text)


def dedupe_key_of(company: str, title: str) -> str:
    """算指纹。任一字段为空就返回 NO_KEY —— 信息不全时不猜。"""
    normalized_company = normalize_company(company)
    normalized_title = normalize_title(title)
    if not normalized_company or not normalized_title:
        return NO_KEY
    return f"{normalized_company}|{normalized_title}"


def dedupe_key(job: JobPosting) -> str:
    """岗位对象的指纹（入库与回填都走这里，保证口径唯一）。"""
    return dedupe_key_of(job.company, job.title)


def annotate_duplicates(jobs: list[JobPosting]) -> int:
    """给同一批岗位里的重复项打上「另见 #N」标记，返回标记条数。

    为什么只在**同一批**里比：一次日报里的岗位才是用户同时看到的一组。
    跨批次（今天推送的 vs 上周推送的）不标注，那时的重复早就在实时推送
    那一层被拦掉了。

    标记给**后出现的那条**：列表按分数降序排，先出现的是更该看的那条。
    """
    first_seen: dict[str, int] = {}
    marked = 0
    for index, job in enumerate(jobs, start=1):
        key = job.dedupe_key or dedupe_key(job)
        if key == NO_KEY:
            continue
        previous = first_seen.get(key)
        if previous is None:
            first_seen[key] = index
            continue
        job.duplicate_note = f"♻️ 同一岗位另见 #{previous}"
        marked += 1
    return marked
