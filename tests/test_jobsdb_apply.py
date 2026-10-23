"""JobsDB 站内投递副驾测试：选项匹配 + 答题口径。

重点不是「能不能跑起来」，而是**不会替你乱答**：
- 已经答过的题一律不动（站点记住的答案往往是对的），**只有经验年限例外**；
- 「有临时签证”和“有临时签证但有行业限制”只差一个词，绝不能选错；
- 「Less than 1 year」和「No experience」是两道不同的答案，不能混；
- 答不出来的题必须留空并报出来，不能猜一个填上去。

控件读取（`read_questions`）和回填（`fill_answer`）要真浏览器，这里不测；
它们的验证靠真实页面上跑一次 `run.py apply <uid>`。

兼容两种运行方式：pytest 与 `python tests/test_jobsdb_apply.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hk_job_radar.copilot.jobsdb_apply import (            # noqa: E402
    AssistReport,
    Question,
    apply_url,
    decide,
    is_onsite,
    pick_option,
    pick_salary,
)
# 真实页面抓下来的选项（OAC Junior Quant 那条，2026-10-21）
RIGHT_TO_WORK = (
    "I am a Hong Kong SAR citizen",
    "I am a Hong Kong SAR permanent resident",
    "I have a temporary visa (eg. QMAS, TTPS, IANG)",
    "I have a temporary visa with restrictions on industry (eg. TechTAS)",
    "I require sponsorship to work for a new employer (e.g. GEP - Professionals or "
    "Employment as Imported Workers visa)",
)
SALARY = ("$5K", "$6K", "$10K", "$14K", "$15K", "$16K", "$17K", "$18K",
          "$19K", "$20K", "$25K", "$30K", "$40K", "$50K", "$60K", "$80K", "$120K or more")
YEARS = ("No experience", "Less than 1 year", "1 year", "2 years", "3 years",
         "4 years", "5 years", "More than 5 years")
EDUCATION = (
    "Primary School",
    "Junior secondary school (Form 3 / Secondary 3 or equivalent)",
    "Associate degree / Higher Diploma or equivalent (HKQF level 4)",
    "Bachelor degree or equivalent (HKQF level 5)",
    "Postgraduate Diploma / Masters or equivalent (HKQF level 6)",
    "Doctoral degree (HKQF level 7)",
)

FORM = {"expected_salary": 20000, "years_experience": "less_than_1",
        "right_to_work": "temporary_visa", "education": "masters"}


# ---------------------------------------------------------------------------
# 选项匹配
# ---------------------------------------------------------------------------
def test_pick_right_to_work_never_lands_on_the_restricted_option():
    """★ 最危险的一组：「有临时签证」和「有临时签证但受行业限制」只差几个词。

    选错的话，雇主会以为你只能做特定行业——比不填还糟。
    """
    choice = pick_option(RIGHT_TO_WORK, must=("temporary visa",),
                         prefer=("iang",), avoid=("restriction", "sponsor"))
    assert choice == "I have a temporary visa (eg. QMAS, TTPS, IANG)"


def test_pick_option_prefers_more_keyword_hits():
    """多个选项都命中 must 时，prefer 命中数多的赢。"""
    options = ("Temporary visa", "Temporary visa (IANG / QMAS)")
    assert pick_option(options, must=("temporary",), prefer=("iang",)) \
        == "Temporary visa (IANG / QMAS)"


def test_pick_option_returns_blank_when_nothing_matches():
    """一个都匹配不上就返回空，让调用方留白——不能退而求其次乱选一个。"""
    assert pick_option(("Yes", "No"), must=("temporary visa",)) == ""


def test_pick_salary_takes_the_closest_band():
    assert pick_salary(SALARY, 20000) == "$20K"
    assert pick_salary(SALARY, 24000) == "$25K"
    assert pick_salary(SALARY, 8000) == "$6K"
    assert pick_salary(SALARY, 500000) == "$120K or more", "封顶档也要能选中"


def test_pick_salary_ignores_options_without_numbers():
    assert pick_salary(("Negotiable", "$20K"), 20000) == "$20K"
    assert pick_salary(("Negotiable",), 20000) == ""


def test_pick_salary_allows_floating_around_the_target():
    """20,000 上下都能接受（实习生报价不必咬死）。

    站点没给 $20K 这一档时，$18K 也算对得上；但绝不能因为「离得近」就
    跳到 $30K ——那不是浮动，那是虚报。
    """
    assert pick_salary(("$18K", "$30K"), 20000) == "$18K"
    assert pick_salary(("$15K", "$22K"), 20000) == "$22K"


def test_pick_salary_prefers_the_lower_band_when_equally_close():
    """一样近就选低的：报低一点顶多少谈几千，报高一点可能连面都见不到。"""
    assert pick_salary(("$18K", "$22K"), 20000) == "$18K"


def test_pick_salary_never_inflates_when_no_band_is_close():
    """没有接近的档时，宁可报低也不虚报。

    只剩 $25K / $30K 这种全高于期望的选项时，取最低那档（表单必须选一个），
    并在说明里标出来让人自己核对。
    """
    assert pick_salary(("$25K", "$30K"), 20000) == "$25K"
    assert pick_salary(("$30K", "$40K"), 20000) == "$30K"


# ---------------------------------------------------------------------------
# 每类题的答题口径
# ---------------------------------------------------------------------------
def test_answers_right_to_work():
    q = Question("Which of the following statements best describes your right to work "
                 "in Hong Kong?", "radio", RIGHT_TO_WORK, "")
    answer = decide(q, FORM)
    assert answer is not None
    assert "temporary visa (eg. QMAS" in answer.choice
    assert "restriction" not in answer.choice


def test_answers_expected_salary_with_configured_value():
    """表单报 20K（书面期望 25K 是谈薪用的，两者刻意不同）。"""
    q = Question("What's your expected monthly basic salary?", "select", SALARY, "")
    answer = decide(q, FORM)
    assert answer is not None and answer.choice == "$20K"
    assert "20,000" in answer.why


def test_answers_years_of_experience_as_less_than_one_year():
    """口径统一「Less than 1 year」——不能答成 No experience（会抹掉实习）。"""
    q = Question("How many years' experience do you have as a Quantitative Analyst?",
                 "select", YEARS, "")
    answer = decide(q, FORM)
    assert answer is not None and answer.choice == "Less than 1 year"


def test_years_question_works_for_any_role_name():
    """同一个口径要能套在不同岗位名上（Risk Management Analyst / Data Analyst …）。"""
    for role in ("Risk Management Analyst", "Data Analyst", "Data Engineer"):
        q = Question(f"How many years' experience do you have as a {role}?",
                     "select", YEARS, "")
        answer = decide(q, FORM)
        assert answer is not None and answer.choice == "Less than 1 year", role


def test_answers_highest_education_as_masters():
    q = Question("What is your highest level of education?", "select", EDUCATION, "")
    answer = decide(q, FORM)
    assert answer is not None
    assert "Masters" in answer.choice and "level 6" in answer.choice


# ---------------------------------------------------------------------------
# 不该动的东西
# ---------------------------------------------------------------------------
def test_already_answered_question_is_left_alone():
    """★ 站点把上次的答案记下来了（实测工作权那条已经选对）。

    盲目覆盖等于每次投递都替你重新做一遍决定——尤其是薪资，你上次可能
    是故意填低的。
    """
    q = Question("What's your expected monthly basic salary?", "select",
                 SALARY, "$20K")
    assert q.answered
    assert decide(q, FORM) is None


def test_work_right_is_not_overwritten_even_if_it_looks_odd():
    """工作权被覆盖的风险最大（选错等于自断资格），所以答过就绝不动。

    这里故意把旧值设成「需要担保」——看着不对，但改它得由人决定。
    """
    q = Question("Which of the following statements best describes your right to "
                 "work in Hong Kong?", "radio", RIGHT_TO_WORK,
                 "I require sponsorship to work for a new employer (e.g. GEP - "
                 "Professionals or Employment as Imported Workers visa)")
    assert decide(q, FORM) is None


def test_years_overwrites_no_experience():
    """★ 经验年限是唯一的例外：答过也要改成 Less than 1 year。

    站点在有的岗位记着「No experience」，两条申请摆在一起口径不一致，
    雇主会以为这些答案是随手填的。
    """
    q = Question("How many years' experience do you have as a Quantitative Analyst?",
                 "select", YEARS, "No experience")
    answer = decide(q, FORM)
    assert answer is not None
    assert answer.choice == "Less than 1 year"
    assert answer.previous == "No experience", "报告里要能说出改前是什么"


def test_years_already_correct_is_not_touched():
    """已经是 Less than 1 year 就不动——不能为了「覆盖」而重复写一遍。"""
    q = Question("How many years' experience do you have as a Data Analyst?",
                 "select", YEARS, "Less than 1 year")
    assert decide(q, FORM) is None


def test_unknown_question_is_left_blank():
    q = Question("Do you have a valid driving licence?", "select",
                 ("Yes", "No"), "")
    assert decide(q, FORM) is None, "没覆盖的题必须留空，不能猜"


def test_question_without_options_is_skipped():
    q = Question("Something odd", "select", (), "")
    assert decide(q, FORM) is None


# ---------------------------------------------------------------------------
# 地址拼装与站内判断
# ---------------------------------------------------------------------------
def test_apply_url_uses_external_id():
    class _Job:
        external_id = "95293720"
        url = "https://hk.jobsdb.com/job/95293720?type=standard&ref=search-standalone#"

    assert apply_url(_Job()) == "https://hk.jobsdb.com/job/95293720/apply"


def test_apply_url_falls_back_to_url_when_id_missing():
    class _Job:
        external_id = ""
        url = "https://hk.jobsdb.com/job/95291469?ref=x"

    assert apply_url(_Job()).endswith("/job/95291469/apply")


def test_onsite_detection():
    assert is_onsite("https://hk.jobsdb.com/job/1/apply")
    assert not is_onsite("https://www.hangseng.com/careers/apply")


# ---------------------------------------------------------------------------
# 报告文案
# ---------------------------------------------------------------------------
def test_report_never_claims_it_submitted():
    """报告最后一句必须把提交动作交还给人——这是合规红线。"""
    report = AssistReport()
    q = Question("What's your expected monthly basic salary?", "select", SALARY, "")
    report.answers.append(decide(q, FORM))
    text = report.summary()
    assert "自己点" in text and "提交" in text


def test_report_for_external_site_says_it_cannot_help():
    report = AssistReport(onsite=False)
    assert "副驾帮不上" in report.summary()


def test_report_separates_overwritten_answers_from_new_ones():
    """改掉旧答案和补答是两件事，报告里要分开数。

    「替你答了 1 道」和「改写了 1 道你之前答过的」分量完全不同——后者
    得让人回去多看两眼。
    """
    report = AssistReport()
    report.changed.append(decide(
        Question("How many years' experience do you have as a Data Analyst?",
                 "select", YEARS, "No experience"), FORM))
    text = report.summary()
    assert "改写了 1 道" in text and "替你答了" not in text
    lines = "\n".join(report.detail_lines())
    assert "No experience → Less than 1 year" in lines


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]
    for fn in _fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(_fns)} tests passed.")
