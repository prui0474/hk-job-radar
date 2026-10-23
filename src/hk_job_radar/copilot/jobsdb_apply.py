"""JobsDB 站内申请的表单助手（投递副驾 · 站点专用）。

为什么单独一个文件，而不是改 `copilot/form_filler.py`
------------------------------------------------------
因为我先把真实申请页打开看了一遍（2026-10-21），结论是**它假设的那套表单
在 JobsDB 上不存在**：

| form_filler 找的 | JobsDB 站内申请的真实页面 |
|---|---|
| 姓名 / 邮箱 / 电话 文本框 | **一个都没有**（站点自己存着账号资料） |
| textarea 开放问题 | 0 个 |
| `select_option(label=精确值)` | 选项文案是 "$25K"、"Less than 1 year" 这种，精确匹配必失配 |
| 进入页面就扫控件 | 第 1 步只有「选简历」，真正的题在第 2 步 |

真实流程是四步（URL 会跟着变）：
    /job/<id>/apply                      ← Choose documents（选简历/求职信）
    /job/<id>/apply/role-requirements    ← Answer employer questions（雇主问题）★ 副驾干这个
    （Update Jobsdb Profile）            ← 改个人资料，副驾不碰
    （Review and submit）                ← 提交，必须你亲手点

所以这里只做一件事：**把第 2 步那些选择题按资料库的口径答了**，然后停手。
第 1 步的简历站点自己记着（实测已经选好了），不用我们管。

四条硬规矩
----------
1. **只填没答的题，只有一个例外**：经验年限一律改成「Less than 1 year」。
   站点会把上次的答案记下来（实测「工作权」已经是对的），盲目覆盖等于每次
   投递都替你重新做一遍决定；但经验年限不同——同一个库里出现「No experience」
   和「Less than 1 year」两种答法，会让人怀疑这些答案是不是随手填的。
   所以这一项**覆盖**，其余一律不碰。
2. **只碰选择题**（radio / select）。文本框和复选框一律留给人工——那些多半是
   「谈谈你的优势」，乱填比空着糟。
3. **永不点提交**，连 `Continue` 都不点：答完就停在这一页，后面三次点击归你。
4. **答不出来就留空并说出来**，不猜。没覆盖的题会进报告的「要你自己答」一栏。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

#: 站内申请页的路径。实测 `/job/<id>/apply` 直接可用，不用去点详情页上的
#: 「Apply」按钮——那个按钮的文字是 `Apply` + 一个不可见字符（U+2060），
#: 按文字匹配永远匹配不上，靠 URL 稳得多。
APPLY_PATH = "/job/{job_id}/apply"

#: 「这条要跳去雇主自己的招聘站」的判据：点开申请页后域名不再是 jobsdb。
#: 实测恒生银行的 MT 项目就是这种——跳到 HSBC 的 Careers 站点，要注册账号、
#: 填一份长表。那不是副驾能帮的，只能明确告诉你「这条得去官网填」。
ONSITE_HOST = "jobsdb.com"

#: 第二步的路径片段。**不能单独拿它判断「到没到题目页」**：实测直接打开
#: `/job/<id>/apply/role-requirements` 时，地址会保留但这个 URL，内容却还是
#: 第一步（向导状态在前端，服务端不认深链）。所以判定只能用下面那个标记。
QUESTIONS_PATH = "/role-requirements"

#: 题目控件的名字前缀。JobsDB 的问卷字段一律叫 `questionnaire.HK_Q_...`，
#: 而第一步「选简历」的两个单选框叫 `document-select-*` / `coverLetter-method`。
#: 这是唯一可靠的「我现在到底在第几步」的判据——URL 和标题都会被前端糊弄。
_QUESTIONNAIRE_JS = """() => document.querySelectorAll('[name^="questionnaire."]').length"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Question:
    """页面上的一道题（一个 radio 组，或一个 select）。"""

    label: str                      # 题目原文
    kind: str                       # "radio" / "select"
    options: tuple[str, ...] = ()   # 所有选项文案
    current: str = ""               # 现在选中的那项（空 = 还没答）
    name: str = ""                  # 控件 name（回填时定位用）

    @property
    def answered(self) -> bool:
        return bool(self.current.strip())

    def brief(self) -> str:
        head = self.label if len(self.label) <= 60 else self.label[:60] + "…"
        return f"{head}（{len(self.options)} 个选项）"


@dataclass(frozen=True)
class Answer:
    """一条决定：把哪道题选成什么，依据是什么。"""

    question: Question
    choice: str
    source: str = "profile"         # profile / rule
    why: str = ""
    #: 覆盖之前的旧答案（空 = 这道题原来没答，是补上的）。
    #: 报告里要分开说「补答」和「改了你的旧答案」——后者得让人多看两眼。
    previous: str = ""

    def brief(self) -> str:
        return f"{self.question.label[:50]} → {self.choice}"


@dataclass
class AssistReport:
    """一次填表尝试的结果（给人看，不是给程序看）。"""

    url: str = ""
    onsite: bool = True             # False = 跳去了雇主官网
    answers: list[Answer] = field(default_factory=list)        # 原来空着的，补答
    changed: list[Answer] = field(default_factory=list)        # 原来答过，改掉了
    left_blank: list[Question] = field(default_factory=list)   # 答不出，留给你
    untouched: list[Question] = field(default_factory=list)    # 你已经答过，没动
    screenshot: str = ""
    note: str = ""                  # 特殊情况（比如这条岗位没有雇主问题）

    def summary(self) -> str:
        if not self.onsite:
            return "这条岗位的申请页跳去了雇主自己的网站，副驾帮不上，请直接在那边填。"
        if self.note:
            return self.note
        parts = []
        if self.answers:
            parts.append(f"替你答了 {len(self.answers)} 道题。")
        if self.changed:
            parts.append(f"改写了 {len(self.changed)} 道你之前答过的题。")
        if self.untouched:
            parts.append(f"{len(self.untouched)} 道你之前答过，没动。")
        if self.left_blank:
            parts.append(f"还有 {len(self.left_blank)} 道我没把握，留给你。")
        if not parts:
            parts.append("这页没有需要我作答的选择题。")
        parts.append("核对无误后请自己点 Continue 走到最后一步提交——我不点提交按钮。")
        return " ".join(parts)

    def detail_lines(self) -> list[str]:
        lines: list[str] = []
        if self.answers:
            lines.append("我替你选的：")
            for a in self.answers:
                lines.append(f"  · {a.question.label[:60]} → {a.choice}"
                             f"（依据：{a.why}）")
        if self.changed:
            lines.append("我改掉的旧答案（改前 → 改后）：")
            for a in self.changed:
                lines.append(f"  · {a.question.label[:60]}：{a.previous} → {a.choice}"
                             f"（依据：{a.why}）")
        if self.untouched:
            lines.append("你之前答过、我没动的：")
            for q in self.untouched:
                lines.append(f"  · {q.label[:60]} → {q.current}")
        if self.left_blank:
            lines.append("我没把握、需要你自己选的：")
            for q in self.left_blank:
                opts = " / ".join(q.options[:4])
                lines.append(f"  · {q.label[:60]}（选项：{opts}…）")
        return lines


# ---------------------------------------------------------------------------
# 选项匹配：把「资料库里的取值」映射到「页面给的选项」
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """归一化：小写 + 去不可见字符 + 压空白。

    站点文案里混着 U+2060（word joiner）这类不可见字符（实测 Apply 按钮上就有），
    不剥掉会让 `"apply" == "apply"` 失败。
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cc"))
    return re.sub(r"\s+", " ", text).strip().lower()


def _as_money(raw: Any) -> int:
    """把 profile 里的薪资取值读成整数月薪（20000 / "20K" / "HKD 25,000"）。"""
    if raw is None or raw == "":
        return 0
    low = _norm(str(raw)).replace(",", "").replace("$", "").replace("hkd", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*k", low)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"(\d{3,7})", low)
    if m:
        return int(m.group(1))
    return 0


def pick_option(options: tuple[str, ...] | list[str], *,
                must: tuple[str, ...] = (),
                prefer: tuple[str, ...] = (),
                avoid: tuple[str, ...] = ()) -> str:
    """在选项里挑一个：先过 `must`（全部命中），再按 `prefer` 命中数排序。

    `avoid` 命中的选项直接排除——「有临时签证但有行业限制」这种选项对
    本科学签 + IANG 的人是错的答案，只差一个词，必须显式排除掉。
    """
    scored: list[tuple[int, int, str, str]] = []
    for idx, raw in enumerate(options):
        low = _norm(raw)
        if not low:
            continue
        if any(_norm(a) in low for a in avoid):
            continue
        if any(_norm(m) not in low for m in must):
            continue
        score = sum(1 for p in prefer if _norm(p) in low)
        scored.append((-score, idx, raw, low))
    if not scored:
        return ""
    scored.sort()
    return scored[0][2]


def _money(text: str) -> int | None:
    """从 "$20K" / "HKD 25,000" / "20K" 这类文案里取出每月金额。"""
    low = _norm(text).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*k", low)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"(\d{4,6})", low)
    if m:
        return int(m.group(1))
    return None


#: 薪资浮动区间：20,000 ± 4,000 都算「对得上」。
#: 2026-10-22 用户拍板：实习/初级岗位报价不必咬死一个数，站点给什么档就选
#: 最接近的那档；但也别为了「对上数字」把 20K 报成 25K——虚报的代价是被直接筛掉。
SALARY_TOLERANCE = 0.20


def pick_salary(options: tuple[str, ...] | list[str], target: int,
                tolerance: float = SALARY_TOLERANCE) -> str:
    """挑一档月薪。三条优先顺序：

    1. 落在 `target` 上下 `tolerance` 范围内的最近一档；一样近时**选低的**
       （报低一点顶多少谈几千，报高一点可能连面都见不到）；
    2. 范围内没有就取「不超过 target 的最大一档」——宁可报低，不虚报；
    3. 全都比 target 高（低薪岗位才会遇到），只能取最低那档，
       并在 `decide()` 的说明里写清「站点没有接近的档，请自己核对」。
    """
    bands = [(raw, _money(raw)) for raw in options]
    bands = [(raw, value) for raw, value in bands if value is not None]
    if not bands:
        return ""

    low, high = target * (1 - tolerance), target * (1 + tolerance)
    inside = [(raw, value) for raw, value in bands if low <= value <= high]
    if inside:
        # 排序键里带上 value：距离相同时数值小的赢，这就是「宁低不高」。
        inside.sort(key=lambda band: (abs(band[1] - target), band[1]))
        return inside[0][0]

    lower = [band for band in bands if band[1] <= target]
    if lower:
        return max(lower, key=lambda band: band[1])[0]
    return min(bands, key=lambda band: band[1])[0]


#: 工作权：本人口径是「学生签 → 毕业后 IANG，不需要担保」。
#: must 里要求出现 temporary visa；avoid 里排掉「有行业限制」那一项。
_RIGHT_TO_WORK = dict(
    must=("temporary visa",),
    prefer=("iang", "qmas", "ttps"),
    avoid=("restriction", "sponsor"),
)

#: 学历：硕士 → HKQF level 6。有的站点不写 QF 等级，只写 Masters。
_EDUCATION = dict(prefer=("masters", "postgraduate"), must=())

#: 经验年限：全部答「Less than 1 year」（两段实习合计约 4 个月）。
#: 注意不能用 `must=("less",)` 之类——「Less than 1 year」和「No experience」
#: 是不同选项，答「No experience」会把实习经历抹掉。
_YEARS = dict(prefer=("less than 1 year", "less than one year"), must=())

#: 题目识别：先用这些正则判断这道题在问什么，再决定用哪条规则。
_SALARY_Q = re.compile(r"salary|compensation|remuneration|pay\s+expect|monthly\s+basic",
                       re.I)
_YEARS_Q = re.compile(r"how\s+many\s+years|years?\s+of\s+experience|years?.{0,20}experience",
                      re.I)
_EDU_Q = re.compile(r"education|qualification|highest\s+level", re.I)
_WORK_RIGHT_Q = re.compile(r"right\s+to\s+work|work\s+(author|rights|entitlement)|visa|"
                           r"sponsorship|citizen|resident", re.I)


def decide(question: Question, form_answers: dict[str, Any]) -> Answer | None:
    """给一道题决定答案。返回 None = 不动（没把握 / 已经是这个答案）。

    `form_answers` 就是 profile.yaml 里 `screening_rules.form_answers` 那一段。

    覆盖规则（2026-10-22 拍板）：**只有「经验年限」会覆盖你已答过的答案**，
    其余题目一律不碰。理由：年限这一项在不同岗位上答成 `No experience` /
    `Less than 1 year` 两种，投出去的两份申请自相矛盾；而工作权、薪资、学历
    站点记着的通常是对的（薪资尤其可能是你故意填低的那一版）。
    """
    if not question.options:
        return None

    label = question.label

    if _YEARS_Q.search(label):
        # 口径统一「Less than 1 year」：两段实习合计约 4 个月。
        # 不按岗位领域分开判——问的是「你在 X 这行几年」，实习没到一年就是没到，
        # 说成 No experience 反而把实习抹掉了。
        choice = pick_option(question.options, **_YEARS)
        if not choice:
            return None
        if question.answered and _norm(choice) == _norm(question.current):
            return None                     # 已经是这个答案，不用动它
        return Answer(question, choice, "profile", "统一口径：实习未满一年",
                      previous=question.current)

    if question.answered:
        return None                         # 其余题目：你答过就不动

    if _WORK_RIGHT_Q.search(label):
        choice = pick_option(question.options, **_RIGHT_TO_WORK)
        if choice:
            return Answer(question, choice, "profile",
                          "学生签 + 毕业后 IANG，不需要雇主担保")

    if _SALARY_Q.search(label):
        target = _as_money(form_answers.get("expected_salary"))
        if target:
            choice = pick_salary(question.options, target)
            if choice:
                value = _money(choice) or 0
                # 偏离超过容忍区间就明说，别让「选了 $30K」看起来像算对了：
                # 那种情况多半是这条岗位的档位设定根本不适用（比如按周薪/日薪列）。
                if abs(value - target) <= target * SALARY_TOLERANCE:
                    why = f"资料库口径 {target:,}/月（可上下浮动），这是最接近的一档"
                else:
                    why = (f"站点没有 {target:,} 附近的档，只有 {choice}——"
                           f"这一项请你自己核对")
                return Answer(question, choice, "profile", why)

    if _EDU_Q.search(label):
        choice = pick_option(question.options, **_EDUCATION)
        if choice:
            return Answer(question, choice, "profile", "数据科学硕士（HKQF 6）")

    return None


# ---------------------------------------------------------------------------
# 页面读取与回填（只有这两个函数碰 Playwright）
# ---------------------------------------------------------------------------
_READ_JS = r"""() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = el => {
    let out = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) out = l.innerText || '';
    }
    if (!out) {
      const c = el.closest('label');
      if (c) out = c.innerText || '';
    }
    return (out || '').replace(/\s+/g, ' ').trim();
  };
  // 题干：往上找最近的 fieldset/legend，或 role=group；
  // 找不到就退到"前一个兄弟节点的文字"（JobsDB 的 select 就是这种结构）。
  const questionOf = el => {
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && lg.innerText.trim()) return lg.innerText.replace(/\s+/g, ' ').trim();
    }
    const grp = el.closest('[role=group], [role=radiogroup]');
    if (grp) {
      const t = grp.getAttribute('aria-label') || grp.innerText.split('\n')[0] || '';
      if (t.trim()) return t.replace(/\s+/g, ' ').trim();
    }
    let prev = el.closest('div, li');
    for (let i = 0; i < 4 && prev; i++) {
      const sib = prev.previousElementSibling;
      if (sib && sib.innerText && sib.innerText.trim().length < 300) {
        return sib.innerText.replace(/\s+/g, ' ').trim();
      }
      prev = prev.parentElement;
    }
    return '';
  };

  const out = [];
  // ① radio 组：同名归一组
  const groups = {};
  [...document.querySelectorAll('input[type=radio]')].filter(vis).forEach(el => {
    const key = el.name || el.id;
    if (!key) return;
    groups[key] = groups[key] || [];
    groups[key].push(el);
  });
  Object.entries(groups).forEach(([name, els]) => {
    const opts = els.map(e => labelOf(e)).filter(Boolean);
    if (opts.length < 2) return;                 // 单个 radio 通常是勾选条款，不管
    const chosen = els.find(e => e.checked);
    out.push({kind: 'radio', name,
              label: questionOf(els[0]),
              options: opts,
              current: chosen ? labelOf(chosen) : '',
              index: els.map(e => e.id || '')});
  });

  // ② select
  [...document.querySelectorAll('select')].filter(vis).forEach(el => {
    const opts = [...el.options].map(o => (o.text || '').trim())
      .filter(t => t && !/^(select|please|--|choose)/i.test(t));
    if (!opts.length) return;
    const cur = el.selectedIndex >= 0 ? (el.options[el.selectedIndex].text || '').trim() : '';
    const placeholder = cur && /^(select|please|--|choose)/i.test(cur) ? '' : cur;
    out.push({kind: 'select', name: el.name || el.id,
              label: questionOf(el), options: opts, current: placeholder,
              index: []});
  });
  return out;
}"""


def read_questions(page: Any) -> list[Question]:
    """读出页面上所有的选择题（radio 组 + select）。"""
    raw = page.evaluate(_READ_JS) or []
    questions: list[Question] = []
    for item in raw:
        questions.append(Question(
            label=item.get("label") or "",
            kind=item.get("kind") or "select",
            options=tuple(item.get("options") or ()),
            current=item.get("current") or "",
            name=item.get("name") or "",
        ))
    return questions


def is_onsite(url: str) -> bool:
    """申请页还是不是留在 JobsDB 站内。"""
    return ONSITE_HOST in (url or "")


def wait_for_questions(page: Any, timeout_s: int = 20) -> bool:
    """等「雇主问题」这一步真的渲染出来。

    判据是页面上出现了 `questionnaire.*` 命名的控件。为什么不看 URL：地址变成
    `/apply/role-requirements` 的那一刻组件还没重绘，此时读页面拿到的是上一步
    那两个「选简历 / 求职信」单选框——第一版就是这么误判的，报告里写着
    「2 道你之前答过」，其实一道真题目都没读到。
    """
    for _ in range(max(1, timeout_s) * 2):
        try:
            if page.evaluate(_QUESTIONNAIRE_JS) > 0:
                page.wait_for_timeout(600)      # 让下拉框把选项也渲染出来
                return True
        except Exception:          # noqa: BLE001
            pass
        page.wait_for_timeout(500)
    return False


def advance_to_questions(page: Any, timeout_s: int = 20) -> bool:
    """从「选择文件」翻到「雇主问题」这一页。

    整个流程里副驾**只点这一次 Continue**，三条理由：
    - 深链不成立（见 `QUESTIONS_PATH` 的说明），不点就到不了题目页；
    - 第一步（选简历）实测站点已经替你选好了，没有可填的东西；
    - 再往后点一次就是「更新个人资料」和「复核提交」，那不归程序管。

    返回是否真的看到了题目。
    """
    try:
        if page.evaluate(_QUESTIONNAIRE_JS) > 0:
            return True                      # 已经在题目页了（重跑时常见）
    except Exception:          # noqa: BLE001
        pass

    try:
        page.get_by_role("button", name="Continue", exact=False).first.click(timeout=10000)
    except Exception as exc:          # noqa: BLE001
        logger.warning(f"[副驾] 找不到 Continue：{exc}")
        return False
    return wait_for_questions(page, timeout_s)


def fill_answer(page: Any, answer: Answer) -> bool:
    """把一条决定落到页面上。返回是否成功。

    只做两件事：`select_option` 和「点那个 radio 的标签」。
    绝不点按钮——`Continue` / `Submit` 都不在我们手里。
    """
    question = answer.question
    try:
        if question.kind == "select":
            page.locator(f"select[name='{question.name}']").first.select_option(
                label=answer.choice)
            return True
        # radio：按选项文案点对应的 label
        radios = page.locator(f"input[type=radio][name='{question.name}']")
        count = radios.count()
        for i in range(count):
            radio = radios.nth(i)
            if _norm(radio.evaluate(
                    """el => {
                        const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
                        return (lab ? lab.innerText : (el.closest('label') ? el.closest('label').innerText : ''));
                    }""")) == _norm(answer.choice):
                radio.check()
                return True
        return False
    except Exception as exc:          # noqa: BLE001
        logger.warning(f"回填失败（{answer.question.label[:40]}）：{exc}")
        return False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def apply_url(job: Any) -> str:
    """岗位 → 站内申请页地址。

    优先用 external_id 拼（最稳），拼不出来再从原链接里挖编号。
    """
    job_id = str(getattr(job, "external_id", "") or "")
    if not job_id:
        m = re.search(r"/job/(\d+)", getattr(job, "url", "") or "")
        job_id = m.group(1) if m else ""
    if not job_id:
        return getattr(job, "url", "") or ""
    return f"https://hk.{ONSITE_HOST}" + APPLY_PATH.format(job_id=job_id)


def assist(page: Any, job: Any, form_answers: dict[str, Any],
           *, dry_run: bool = False) -> AssistReport:
    """在已经打开申请页的浏览器上读题、答题。`page` 由调用方负责开和关。

    `dry_run=True` 只报告「本来会选什么」，**不往页面里写任何值**。
    但它照样会点那一次 `Continue` —— 不翻到题目页就看不到题目，
    那就没法告诉你「我本来会怎么答」。这一次点击不提交任何东西。
    """
    report = AssistReport(url=page.url, onsite=is_onsite(page.url))
    if not report.onsite:
        return report

    if not advance_to_questions(page):
        report.note = ("没找到「Continue」，或者这条岗位没有「雇主问题」这一步。"
                       "页面停在原地，我没有填也没有点。")
        return report
    report.url = page.url

    questions = read_questions(page)
    logger.info(f"[副驾] 读到 {len(questions)} 道选择题")

    for q in questions:
        answer = decide(q, form_answers)
        if answer is None:
            # 有答案但决定不动 → 「你已经答过」；本来就没答 → 「留给你」
            (report.untouched if q.answered else report.left_blank).append(q)
            continue
        if dry_run or fill_answer(page, answer):
            # previous 非空 = 这道题原来有答案，是被我们改掉的，单独归一类
            (report.changed if answer.previous else report.answers).append(answer)
        else:
            report.left_blank.append(q)
    return report
