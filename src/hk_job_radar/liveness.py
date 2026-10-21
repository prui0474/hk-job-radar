"""推送前复核：这个岗位现在还接受申请吗？

为什么需要这一层
----------------
抓取只读搜索结果列表页，而「已过期 / 目前不接受申請」这类状态**只写在详情页上**，
列表卡片上根本不存在。所以程序天生看不见岗位关没关——用户点进日报里的链接，
才发现是死链。

怎么决定「砍掉谁」
------------------
原则只有一条：**只砍确认已经失效的，不按年龄一刀切。**
一个挂了 14 天但还在收简历的岗位，对求职者依然有价值；一条 2 天前就已撤下的
广告，多留一天都是垃圾。年龄和「还能不能投」不是一回事，实测过：10 条 14 天前
的 JobsDB 岗位里 9 条仍然在招。

所以按「能不能复核」分成两档：
- 能复核的（RECHECKABLE_SOURCES）：年龄不再是砍它的理由，只留一道 30 天的硬天花板
  （MAX_RECHECK_AGE_DAYS），中间那段交给本模块逐条看。
- 不能复核的（LinkedIn）：只能靠年龄兜，走 config.report_max_age_days 那条更严的线。

为什么 LinkedIn 不能复核
------------------------
LinkedIn 的用户协议明文禁止机器人，而「逐个点开岗位详情」恰恰是最容易被识别成
机器人的行为（频率、停留时长、滚动轨迹都异常）。本程序对 LinkedIn 的纪律是
「只读搜索列表页、不点详情」，这条线不为准确率让步。
代价是 LinkedIn 的过期岗位仍然可能漏过去——这是刻意的取舍，不是遗漏。

失败一律放行（fail-open）
------------------------
打不开、超时、被 Cloudflare 挡住、站点改版导致文案对不上——全部按「还活着」处理。
理由：漏掉一个过期岗位只是让用户白点一次，而错杀一个好岗位可能让用户错过一整个机会。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from hk_job_radar.models import JobPosting

#: 「能复核」的站点：这些站的岗位到底还招不招，事后查得出来。
#: 只要在名单里，岗位年龄就不再是砍掉它的理由——交给复核去判断。
#:   - jobsdb / indeed：打开详情页看有没有关闭文案（见下方 DETAIL_CHECK_SOURCES）
#:   - workday：抓取时接口就返回了 canApply（详见 scrapers/workday.py），
#:     等于抓的那一刻已经复核过一次，不必再开页面。
RECHECKABLE_SOURCES: tuple[str, ...] = ("jobsdb", "indeed", "workday")

#: 其中真正要「打开详情页」才能判断的站点。
#: LinkedIn 不在里面：它的用户协议明文禁止机器人，而逐个点开岗位详情恰恰最容易
#: 被识别成机器人。代价是 LinkedIn 的过期岗位只能靠年龄兜——这是刻意的取舍。
DETAIL_CHECK_SOURCES: tuple[str, ...] = ("indeed", "jobsdb")

#: 关闭状态的确切文案。繁简两套都要——站点文案随浏览器 locale 变化，
#: 而且部分条目会中英混排（Indeed 香港站就是这样）。
#: 匹配前会把所有空白去掉再比对，所以这里按「无空格」形式书写。
CLOSED_MARKERS: tuple[str, ...] = (
    # Indeed：用户实测原文（繁体）
    "此職位在indeed上已過期",
    "此职位在indeed上已过期",
    "thisjobhasexpiredonindeed",
    # JobsDB：广告已被撤下的实际文案（2026-10-20 实测，h2 标题就是这句）
    #   副标题：「Jobs remain on Jobsdb for 30 days, unless the advertiser removes them sooner」
    "thisjobisnolongeradvertised",
    "nolongeradvertised",
    "不再刊登",            # 中文界面下的对应文案（「此職位已不再刊登」等，取最短公共片段）
    # 两个站通用的过期表述
    "此職位已過期",
    "此职位已过期",
    "職位已過期",
    "职位已过期",
    "thisjobhasexpired",
    "職位已關閉",
    "职位已关闭",
    "thisjobisnolongeravailable",
    # 「停止收简历」类表述（LinkedIn 那条就是这个状态；别的站也可能用）
    "目前不接受申請",
    "目前不接受申请",
    "不再接受申請",
    "不再接受申请",
    "已停止接受申請",
    "已停止接受申请",
    "nolongeracceptingapplications",
    # 招聘方主动撤下
    "職位已暫停招聘",
    "职位已暂停招聘",
)

#: 站点把我们拦死时的文案（Cloudflare 的 WAF 拒绝页，不是「等一等就能过」的挑战页）。
#: 实测 Indeed 的**详情页**就是这样：列表页能过，点进单个岗位直接被拦。
#: 这类页面重试没有意义，识别出来是为了立刻停手——否则每条岗位白等一轮过盾。
BLOCK_MARKERS: tuple[str, ...] = (
    "请求被拦截",
    "請求被攔截",
    "yourrequestwasblocked",
    "sorry,youhavebeenblocked",
    "accessdenied",
    "rayid",
)

#: 「能复核」的岗位最多放到多老。超过这条线连复核都不做了，直接不推。
#: 为什么需要一个天花板：有些雇主把同一个 requisition 挂几个月，关掉又重新发，
#: 复核它只是白花时间；而一条三个月前的岗位就算还开着，对求职者的价值也很低。
#: 这条线**只**作用于 RECHECKABLE_SOURCES；LinkedIn 这类不能复核的站点另有
#: 更严的线（config.report_max_age_days），因为它只能靠年龄兜。
MAX_RECHECK_AGE_DAYS = 30

#: 单次复核的默认上限。日报通常只有个位数岗位，20 已经足够宽松；
#: 设上限是防止第一次运行回填了几十个岗位时，推送被拖到十几分钟。
DEFAULT_MAX_CHECKS = 20
#: 整批复核的时间预算（秒）。超了就停手，剩下的按「还活着」处理。
DEFAULT_MAX_SECONDS = 150.0
#: 单个岗位页最多等多久过盾。这里刻意不用全局的 cloudflare_wait_seconds（默认更长）：
#: 复核只是个附加动作，不值得为它卡住半分钟——过不去就当「查不出来」放行。
PER_JOB_CLOUDFLARE_WAIT = 8


def is_closed_text(text: str) -> bool:
    """判断一段页面文字里有没有「已关闭」的表述。

    先去掉全部空白再比对：站点的提示语常被拆成多行、或在中文里插了全角空格，
    直接 `in` 匹配会漏（例如「此職位在 Indeed 上已過期」中间就有空格）。
    """
    if not text:
        return False
    hay = "".join(text.split()).lower()
    return any(marker in hay for marker in CLOSED_MARKERS)


def is_blocked_text(text: str) -> bool:
    """判断页面是不是「被站点拦死」而不是「岗位已关闭」。

    两者的区别很关键：被拦死说明这次判断根本没做成，必须放行（否则会把
    一整站的好岗位全错杀）；已关闭才是要剔除的。
    """
    if not text:
        return False
    hay = "".join(text.split()).lower()
    return any(marker in hay for marker in BLOCK_MARKERS)


def _age_key(job: JobPosting) -> float:
    """排序键：越小 = 越老。

    优先用发布日；抓不到发布日（Indeed 列表页就没有）退化成入库时间，
    与 db 里的年龄口径保持一致（见 Database._AGE_SQL）。
    """
    return (job.posted_at or job.first_seen_at).timestamp()


@dataclass
class LivenessResult:
    """复核结果：只挑出「确定已经关了」的那批，其余一律视为可推。"""

    closed: list[JobPosting] = field(default_factory=list)
    checked: int = 0
    skipped: int = 0        # 因超上限 / 超时预算 / 站点不支持而没查的


def check_jobs(jobs: list[JobPosting], browser, *,
               max_checks: int = DEFAULT_MAX_CHECKS,
               max_seconds: float = DEFAULT_MAX_SECONDS,
               timeout_ms: int = 20000) -> LivenessResult:
    """逐个打开详情页，把确定已关闭的岗位挑出来。

    jobs：本轮准备推送的岗位（已经过了分数与时效两道闸门，数量通常很少）。
    browser：BrowserManager 实例，由调用方创建与关闭（复用它持有的登录态/过盾状态）。
    """
    result = LivenessResult()
    todo = [j for j in jobs if j.source in DETAIL_CHECK_SOURCES]
    result.skipped += len(jobs) - len(todo)
    # 最老的先查：年龄越大越可能已经招满，把有限的复核预算花在最可能出问题的那批上。
    # 这样「没查到、按放行处理」的剩下那几个，是相对新的、更可能还在招的。
    todo.sort(key=_age_key)
    if len(todo) > max_checks:
        result.skipped += len(todo) - max_checks
        todo = todo[:max_checks]
        logger.info(f"推送前复核：本轮只查最老的 {max_checks} 个（其余 {result.skipped} 个直接放行）")
    if not todo:
        return result

    started = time.monotonic()
    by_source: dict[str, list[JobPosting]] = {}
    for job in todo:
        by_source.setdefault(job.source, []).append(job)

    for source, batch in by_source.items():
        try:
            page = browser.launch(source)
        except Exception as exc:
            # 浏览器起不来（profile 被占用 / 依赖缺失）→ 整批放行，绝不阻断推送
            logger.warning(f"推送前复核：{source} 浏览器启动失败，本轮该站跳过（{exc}）")
            result.skipped += len(batch)
            continue

        for pos, job in enumerate(batch):
            if time.monotonic() - started > max_seconds:
                logger.info(f"推送前复核：已用满 {max_seconds:.0f} 秒预算，剩余岗位直接放行")
                result.skipped += 1
                continue
            verdict = _probe(page, job, browser, timeout_ms)
            result.checked += 1
            if verdict == "closed":
                result.closed.append(job)
                logger.info(f"推送前复核：{job.job_uid} 已关闭（{job.title[:40]}），本轮不推")
            elif verdict == "blocked":
                # 已经被拦死，继续查只会一条条白等。整站收手，剩下的放行。
                logger.warning(
                    f"推送前复核：{source} 拒绝了我们的详情页请求（Cloudflare 拦截），"
                    f"本站本轮不再复核，其余岗位照常推送。")
                result.skipped += len(batch) - pos - 1
                break

    return result


def _probe(page, job: JobPosting, browser, timeout_ms: int) -> str:
    """打开单个岗位页，返回 closed / alive / blocked / unknown。

    unknown（查不出来）与 alive 对调用方的效果相同——都放行，
    分开只是为了日志与测试里能区分「判断结果」和「没查成」。
    """
    try:
        page.goto(job.url, wait_until="domcontentloaded", timeout=timeout_ms)
        # 过盾只等待、不破解；刻意用较短的预算，过不去就放行，不为附加动作卡住推送
        passed = browser.wait_cloudflare(page, timeout_s=PER_JOB_CLOUDFLARE_WAIT, quiet=True)
        # 只看可见文字（inner_text 不含隐藏模板），避免把页面里的隐藏文案当成提示
        text = page.inner_text("body") or ""
        if is_blocked_text(text):
            return "blocked"
        if not passed:
            return "unknown"
        browser.human_delay()
        return "closed" if is_closed_text(text) else "alive"
    except Exception as exc:
        logger.debug(f"推送前复核：{job.job_uid} 打开失败（按放行处理）：{exc}")
        return "unknown"
