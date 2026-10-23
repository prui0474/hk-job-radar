"""CLI 命令实现（架构第 8 节）：run / once / report / stats / apply / login。

run.py 只负责把 src/ 加入 sys.path 并调用这里的 main()——所有命令的
装配（config → db → pipeline → scheduler）都集中于此，方便学习者从
入口开始单步跟踪整个系统的启动路径。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

from hk_job_radar import channels
from hk_job_radar.config import (
    PROJECT_ROOT,
    ConfigError,
    load_config,
    require_llm_key,
    require_notify,
    setup_logging,
)
from hk_job_radar.db import Database
from hk_job_radar.pipeline import Pipeline
from hk_job_radar.scheduler import Scheduler


# ---------------------------------------------------------------------------
# 各命令实现
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    """`hk_radar run`：启动常驻监控（推荐配合 Windows 任务计划开机自启）。"""
    config = load_config()
    require_notify(config)      # 一个可用的推送通道都没有 → 中文报错并退出
    require_llm_key(config)     # 缺 API key → 提示设置（打分会全降级，明确告知）
    db = Database(str(_db_path()))
    pipeline = Pipeline(config, db)
    Scheduler(config, pipeline).start()   # 阻塞直至 Ctrl+C
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """`hk_radar report now`：立即生成并推送今日日报（不等定时）。"""
    config = load_config()
    require_notify(config)
    db = Database(str(_db_path()))
    # force=True：用户手动点名要一份，就给他一份（不受"今天已推过"的去重限制）
    Pipeline(config, db).run_daily_report(force=True)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """`hk_radar once`：抓一轮 → 打分 → 推日报 → 退出（不常驻）。

    为什么需要它：定时轮询只在电脑开着时才跑。如果习惯是"开电脑十几分钟就关"，
    就没必要常驻——双击 run_once.bat 跑一轮、拿到结果、直接关机。
    与常驻模式共用同一套配额/轮转/去重逻辑，不会多抓、也不会重复推。
    """
    config = load_config()
    require_notify(config)
    require_llm_key(config)
    db = Database(str(_db_path()))
    pipeline = Pipeline(config, db)
    summary = pipeline.run_monitoring_cycle()   # 里含"开机补发日报"的兜底
    sent = pipeline.run_daily_report()          # 再兜一次：确保这次开机不白跑
    print("─" * 52)
    for source, stat in (summary.get("sources") or {}).items():
        print(f"  {source:<9} 抓到 {stat.get('fetched', 0):>3} → 初筛留 {stat.get('kept', 0):>3}"
              f" → 新入库 {stat.get('new', 0):>3} → 已打分 {stat.get('scored', 0):>3}"
              f"（打分失败 {stat.get('degraded', 0)}）")
    print(f"  实时推送（≥{config.realtime_threshold} 分）   {summary.get('pushed_realtime', 0)} 个")
    print(f"  推送通道：{'、'.join(pipeline.notifier.labels) or '（未配置）'}")
    print(f"  日报：{'已推送 ✅' if sent else '本次无需推送（今日已推过 / 暂无新岗位）'}")
    print("─" * 52)
    if not sent:
        print("提示：日报今天已经推送过了，新岗位会累计到明天那份；")
        print("      想强制再要一份，双击 report_now.bat（Mac 为 report_now.command）。")
    print("✅ 本轮结束，可以关闭本窗口（直接关机也行）。")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    """`hk_radar notify test`：给已启用的推送通道发一条测试消息。

    为什么必须有这个命令：推送是唯一输出口，配错了等于「程序一直在跑、
    但你永远收不到东西」。等 09:00 的日报来验证太慢，所以给一个 5 秒的验证入口
    ——配好通道后立刻双击一次，收到消息就说明通了。
    """
    config = load_config()
    require_notify(config)          # 没配好的话这里就给中文提示并退出
    from hk_job_radar.notify.dispatch import Notifier

    db = Database(str(_db_path()))
    notifier = Notifier.from_config(config, db)

    print(f"正在向 {len(notifier.channels)} 个通道发送测试消息："
          f"{'、'.join(notifier.labels)}")
    message = (
        "✅ 香港岗位雷达 · 测试消息\n"
        + "━" * 18 + "\n"
        "看到这条说明推送通道配置成功。\n"
        "以后每天会以同样的方式把岗位日报发到这里。"
    )
    results = notifier.send_text(message)

    ok = [name for name, good in results.items() if good]
    bad = [name for name, good in results.items() if not good]
    for name in ok:
        print(f"  ✅ {channels.channel_label(name)}：发送成功，去你的收件端看一眼")
    for name in bad:
        print(f"  ❌ {channels.channel_label(name)}：发送失败（原因见 data/logs/ 下的日志）")

    print("─" * 52)
    if not ok:
        print("提示：最常见的原因是凭据复制不完整、或填错了位置。")
        print("      可对照 docs/推送渠道对比.md 里的「收不到消息？」一节逐条检查。")
        return 1
    if bad:
        print("部分通道失败，但至少有一个通了——不影响日常使用。")
    print("✅ 测试完成。收得到消息就说明配置没问题，可以开始用了。")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """`hk_radar stats`：打印今日运行状态（P0-8）。只读命令，不需要任何凭据。

    刻意传 require_runnable=False：stats 只读数据库、顺带显示每日上限，
    即使用户还没填关键词也应该能打开；填词的事留给抓取类命令去拦。
    """
    config = load_config(require_runnable=False)
    db = Database(str(_db_path()))
    s = db.get_stats()

    print(f"📊 香港岗位雷达 · 运行状态（{s['date']}，香港时间）")
    print("─" * 44)
    if s["pages_by_source"]:
        for source, pages in s["pages_by_source"].items():
            limit = config.daily_limits.get(source, "?")
            print(f"  今日抓取页数 · {source:<10} {pages} 页（每日上限 {limit}）")
    else:
        print("  今日抓取页数 · 尚无记录")
    print(f"  今日新入库岗位        {s['jobs']['new_today']} 个")
    print(f"  待进日报岗位          {s['jobs']['unreported']} 个")
    print(f"  今日已推送岗位        {s['jobs']['reported_today']} 个")
    print(f"  高分岗位累计（≥80）   {s['jobs']['high_score_total']} 个")
    print(f"  未打分岗位累计        {s['jobs']['unscored_total']} 个（LLM 降级）")
    print(f"  今日用户反馈          {s['feedback_today']} 条")
    if s["events_today"]:
        events = "、".join(f"{k} ×{v}" for k, v in s["events_today"].items())
        print(f"  今日运行事件          {events}")
    if not config.keywords:
        print("─" * 44)
        print("提示：config.yaml 里的 keywords 还没填，程序暂时抓不到岗位。")
        print("      填上你想找的岗位名称后即可开始（见 README 快速上手第 4 步）。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """`hk_radar doctor`：一键自检。

    只读诊断：不改配置、不动数据，只告诉你「哪里没配好、怎么修」。
    有 ❌ 项时返回 1，全通过（或只有提醒）返回 0 —— 方便写进脚本做前置检查。
    """
    from hk_job_radar.doctor import run_doctor

    return run_doctor(llm_test=not args.no_llm)


def cmd_setup(args: argparse.Namespace) -> int:
    """`hk_radar setup`：启动本地网页配置向导。

    这是个「会一直占着窗口」的命令（像 run 一样），关掉窗口或按 Ctrl+C
    才结束。所以它放在命令行里而不是做成静默后台服务 —— 用户看得见、
    停得掉，才知道发生了什么。

    返回码见 setup_wizard.run_wizard：3 表示向导关了但一次都没保存过。
    安装脚本靠这个码决定「要不要提醒用户回去点保存」。
    """
    from hk_job_radar.setup_wizard import run_wizard

    return run_wizard(port=args.port, open_browser=not args.no_browser,
                      chained=bool(getattr(args, "chain", False)))


def cmd_guide(args: argparse.Namespace) -> int:
    """`hk_radar guide <key>`：打印安装流程里的一段中文提示。

    存在的唯一理由是编码：批处理文件（.bat）必须是纯 ASCII，否则
    cmd.exe 在非 65001 代码页下会把它整片解析成乱码。而 Windows 用户
    最需要的恰恰是中文说明，所以这些长句交给 Python 来打印。
    """
    from hk_job_radar.install_guide import main as guide_main

    key = getattr(args, "key", None)
    return guide_main([key] if key else [])


def cmd_apply(args: argparse.Namespace) -> int:
    """`hk_radar apply <job_id> [--probe]`：投递副驾（T05）。

    两条路，按岗位来源分派：
      - JobsDB → `copilot/jobsdb_apply.py`：站内四步申请，只答「雇主问题」那一步；
      - 其它站点 → 旧的通用填表器（`copilot/form_filler.py`），尚未在真实页面验证。

    合规红线：本函数不点任何提交按钮。JobsDB 那条路只点一次 `Continue`
    （把「选择文件」翻到「雇主问题」），剩下的点击全归用户。
    """
    config = load_config()
    db = Database(str(_db_path()))
    try:
        job = db.get_job(args.job_id)
        if job is None:
            print(f"❌ 找不到岗位：{args.job_id}")
            print("   提示：job_uid 形如 jobsdb:12345678；可以在工作台卡片上复制，"
                  "或用 `run.py stats` 看看库里有哪几条。")
            return 1

        # 延迟导入：copilot 依赖 playwright，stats/login 等命令不该被它拖累
        from hk_job_radar.copilot.profile import ProfileStore
        from hk_job_radar.scrapers.browser import BrowserManager

        profile = ProfileStore().load()
        # form_answers 是【选择题】的结构化答案（薪资档位、经验年限、学历）。
        # 它和 screening_rules 里那些「话术」刻意分开：一段话塞不进下拉框，
        # 硬塞只能靠猜关键词，猜错就答到别的选项上去了。
        rules = profile.raw.get("screening_rules") or {}
        form_answers = dict(rules.get("form_answers") or {})

        browser = BrowserManager(config)
        if job.source == "jobsdb":
            return _apply_jobsdb(db, browser, job, form_answers,
                                 dry_run=bool(getattr(args, "probe", False)))
        return _apply_generic(db, browser, job, config, profile)
    finally:
        db.close()


def _shot(page, name: str = "apply_latest.png") -> str:
    """存一张截图给用户核对用。失败不阻断流程。"""
    try:
        from hk_job_radar.config import DATA_DIR

        out = DATA_DIR / "screenshots"
        out.mkdir(parents=True, exist_ok=True)
        path = out / name
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:          # noqa: BLE001
        logger.debug(f"截图失败（忽略）：{exc}")
        return ""


def _apply_jobsdb(db, browser, job, form_answers: dict, *, dry_run: bool = False) -> int:
    """JobsDB 站内申请的副驾流程。**永不提交**。"""
    from hk_job_radar.copilot import jobsdb_apply
    from hk_job_radar.models import ApplicationRecord, now_hk

    url = jobsdb_apply.apply_url(job)
    print(f"\n🚗 投递副驾：{job.title} @ {job.company}")
    print(f"   申请页：{url}")
    if dry_run:
        print("   （试跑：只告诉你我会选什么，不往页面里写任何值）")

    page = browser.launch(job.source, force_headless=False)   # 必须看得见
    print("   正在打开申请页（浏览器窗口会弹出来，不用管它）…", flush=True)
    try:
        # ★ 等 domcontentloaded 而不是 networkidle：JobsDB 页面永远有后台请求，
        #   networkidle 在真实页面上从来不会就绪（实测每次都超时）。
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        browser.wait_cloudflare(page)
        page.wait_for_timeout(3000)
        report = jobsdb_apply.assist(page, job, form_answers, dry_run=dry_run)
        report.screenshot = _shot(page)
    except Exception as exc:          # noqa: BLE001
        logger.exception(f"投递副驾执行失败：{exc}")
        browser.close()
        print(f"❌ 副驾执行失败：{exc}（详见 data/logs/ 日志）")
        return 1

    print()
    print(report.summary())
    for line in report.detail_lines():
        print(line)
    if report.screenshot:
        print(f"\n页面截图：{report.screenshot}")

    if not report.onsite or report.note:
        browser.close()
        if not report.onsite:
            print("\n（已经打开那个页面了，就在浏览器里——直接在那儿填吧。）")
            try:
                input("看完按回车关闭浏览器 > ")
            except (EOFError, KeyboardInterrupt):
                pass
            browser.close()
        return 0

    if dry_run:
        browser.close()
        print("\n（试跑结束：页面一个字都没改。去掉 --probe 才会真的填。）")
        return 0

    print("\n" + "=" * 62)
    print("接下来的每一步都由你操作：")
    print("  1) 在浏览器里核对每个答案——尤其「期望薪资」和「经验年限」这两项；")
    print("  2) 点 Continue 依次经过「更新资料」和「复核」两步；")
    print("  3) 在最后一页亲手点 Submit。")
    print("本系统不会替你提交——这是合规红线。")
    print("=" * 62)
    try:
        submitted = input("提交完成后输入 y 留档，直接回车表示先不记 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        submitted = ""
    browser.close()

    if submitted in ("y", "yes"):
        record = ApplicationRecord(
            job_uid=job.job_uid,
            company=job.company,
            title=job.title,
            applied_date=now_hk().date().isoformat(),
            channel="jobsdb",
            resume_version="",
        )
        db.save_application(record)
        # 走 set_board_status 而不是 update_status：要把这次投递记进
        # status_history，工作台「近 7 天投递」和漏斗才看得见。
        db.set_board_status(job.job_uid, "applied", note="投递副驾")
        print(f"✅ 已留档并标成已投：{record.title} @ {record.company}"
              f"（{record.applied_date}）")
    else:
        print("未留档（本次不计入投递记录）。")
    return 0


def _apply_generic(db, browser, job, config, profile) -> int:
    """非 JobsDB 岗位的旧路径（通用填表器 + 终端逐字段确认）。

    保留它是因为 LinkedIn / 猎头直投的页面结构完全不同，而新写的
    JobsDB 专用逻辑套不过去。这条路**没有在真实页面上验证过**，
    所以先照实说一句。
    """
    from hk_job_radar.copilot.confirm import ConfirmGate
    from hk_job_radar.copilot.form_filler import FormFiller
    from hk_job_radar.copilot.profile import ProfileStore
    from hk_job_radar.copilot.qa_engine import QAEngine
    from hk_job_radar.config import DATA_DIR
    from hk_job_radar.models import ApplicationRecord, now_hk

    resumes_dir = DATA_DIR / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    if not any(resumes_dir.iterdir()):
        print(f"⚠️  {resumes_dir} 目录下没有简历文件，副驾没法自动上传。")

    print(f"\n⚠️  {job.source} 的申请页结构和 JobsDB 不一样，这条路还没在真实"
          f"页面上验证过，字段识别可能不准。")
    print("   遇到不对的字段，请在浏览器里直接改，别指望它填对。")

    store = ProfileStore()
    filler = FormFiller(config, browser, store, QAEngine(config.llm, profile))

    print(f"\n🚗 投递副驾启动：{job.title} @ {job.company}")
    print(f"   链接：{job.url}\n")

    try:
        result = filler.fill(job)
        # 复用同一个 page（BrowserManager 按 source_key 缓存，不会重开）
        page = browser.launch("apply", force_headless=False)
        decision = ConfirmGate().review(result, page)
    except Exception as exc:          # noqa: BLE001
        logger.exception(f"投递副驾执行失败：{exc}")
        browser.close()
        print(f"❌ 副驾执行失败：{exc}（详见 data/logs/ 日志）")
        return 1

    if decision != "confirmed":
        browser.close()
        print("已放弃本次投递（未留档）。")
        return 0

    print("\n" + "=" * 62)
    print("请在弹出的浏览器窗口中补齐缺项，并亲手点击 Submit / 提交按钮。")
    print("本系统不会替你提交——这是产品的合规红线。")
    print("=" * 62)
    try:
        submitted = input("已在浏览器中提交请输入 y 留档，否则直接回车跳过 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        submitted = ""
    browser.close()

    if submitted in ("y", "yes"):
        record = ApplicationRecord(
            job_uid=job.job_uid,
            company=job.company,
            title=job.title,
            applied_date=now_hk().date().isoformat(),
            channel=result.platform,
            resume_version=Path(result.resume_path).name if result.resume_path else "",
        )
        db.save_application(record)
        db.set_board_status(job.job_uid, "applied", note="投递副驾")
        print(f"✅ 已留档：{record.title} @ {record.company}（{record.applied_date}）")
    else:
        print("未留档（本次不计入投递记录）。")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """`hk_radar login jobsdb`：手动登录引导模式（T02）。

    打开有头浏览器 → 用户手动完成登录/过 Cloudflare 验证 → 回到终端按
    回车 → Cookie 随 persistent context 持久化到 data/browser/<source>/。
    之后所有抓取都复用这份登录态，无需再次过盾。
    """
    source = args.source
    if source not in ("jobsdb", "linkedin", "indeed"):
        print(f"不支持的站点：{source}（可选：jobsdb / linkedin / indeed）")
        return 1

    config = load_config()
    from hk_job_radar.scrapers.browser import BrowserManager

    # 换一种登录方式（--cookie）：不重新登录、不碰 reCAPTCHA，直接搬运
    # 用户正常浏览器里的登录凭证 Cookie。这是针对 LinkedIn 验证码加载
    # 不出来这一顽疾的替代方案。
    if getattr(args, "cookie", False):
        return _import_cookie_login(config, source)

    browser = BrowserManager(config)
    page = browser.launch(source, force_headless=False)   # 登录必须看得见浏览器

    entry_urls = {
        "jobsdb": "https://hk.jobsdb.com/",
        "linkedin": "https://www.linkedin.com/",
        "indeed": "https://hk.indeed.com/",
    }
    try:
        page.goto(entry_urls[source], wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:  # noqa: BLE001 - 打不开是常见情况，给中文提示而不是 traceback
        print(f"\n⚠️  打开 {source} 时出错：{exc}")
        print("   通常是网络到不了这个站点。先双击 doctor.bat，看第 5 项「网络出口」。")
        browser.close()
        return 1

    print("=" * 60)
    print(f"已打开 {source} 浏览器窗口。请在浏览器中完成：")
    print("  1) 登录你的账号（LinkedIn 建议使用求职专用小号）；")
    print("  2) 如出现 Cloudflare/验证码页面，手动完成验证；")
    print("  3) 确认页面能正常浏览后，回到本窗口按回车。")
    print("（登录态会保存在 data/browser/ 目录，后续抓取自动复用）")
    print("=" * 60)

    # 先给它一点时间自动通过；过不去就立刻说清卡在哪，不让人对着转圈猜。
    # 这里不等满抓取用的 45 秒 —— 登录模式本来就是让人来操作的。
    passed = browser.wait_cloudflare(page, timeout_s=_LOGIN_CHALLENGE_WAIT_S,
                                     quiet=True)
    if passed:
        print("✅ 页面正常（没有验证页，或已自动通过）。")
    else:
        print()
        _diagnose_login_failure(source)

    try:
        input("\n完成后按回车关闭浏览器 > ")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        settled = False
        try:
            settled = browser.wait_cloudflare(page, timeout_s=3, quiet=True)
        except Exception:  # noqa: BLE001 - 页面正在关/跳转时取状态会失败，忽略
            settled = False
        try:
            final_url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            final_url = ""
        browser.close()

    if source == "linkedin" and any(
            frag in final_url for frag in ("/login", "/signup", "authwall")):
        print("\n⚠️  页面还停在 LinkedIn 的登录页 —— 这次没有登录成功。")
        print("   可以改用搬 cookie 的方式：双击 login_linkedin.bat，选第 2 项。")
        return 1
    if not settled:
        print("\n⚠️  页面看起来还在验证中，这次存下的登录态可能无效。")
        print("   按上面的提示处理网络后，再双击一次这个脚本。")
        return 1

    print("\n✅ 登录态已保存。接下来双击 start.bat（Mac 上是 start.command）就开始跑了。")
    return 0


#: login 时等待验证页自动通过的时间。比抓取时的 45 秒短：这个模式本来就是
#: 让人来操作的，过不去应当尽快说清原因，而不是让人干等。
_LOGIN_CHALLENGE_WAIT_S = 20

#: 各站点「过验证」真正依赖的第三方域名。
#: 注意：网站首页能打开，不代表这个域名能打开 —— 而验证能否走完全看它。
_PROBE_REQUIRED = {
    "jobsdb": ("Cloudflare 验证组件", "challenges.cloudflare.com"),
    "indeed": ("Cloudflare 验证组件", "challenges.cloudflare.com"),
    "linkedin": ("人机验证组件", "www.google.com"),
}


def _diagnose_login_failure(source: str) -> None:
    """验证没过时，替用户分清「网络到不了」还是「浏览器被判定为自动化」。

    这两种情况的下一步完全相反：前者要改网络通道，后者要换出口 IP 或亲手点
    复选框。而用户在屏幕上看到的都只是「一直转」，分不出来。
    """
    from hk_job_radar.doctor import tcp_probe

    name, host = _PROBE_REQUIRED[source]
    if not tcp_probe(host):
        print("⚠️  验证没有通过。我替你看了一下网络，问题出在网络这一侧：\n")
        print(f"    你的网络连不上 {host}")
        print(f"    ——{name}就放在这个域名上。验证所需的东西下载不下来，")
        print("    所以页面会一直转，跟你点不点、等多久都无关。\n")
        print("  三个办法，按省事程度排：")
        print("    1. 把代理软件切成 TUN / 虚拟网卡模式，再双击一次这个脚本。")
        print("       （这种模式在网络层接管流量，程序不需要额外设置）")
        print('    2. 在 config.yaml 里把 browser.proxy 改成 "system"，')
        print("       或直接填你的代理地址，例如 http://127.0.0.1:7890。")
        print("    3. 换一个能直连香港的网络环境。\n")
        print("  拿不准就先双击 doctor.bat，第 5 项「网络出口」会直接告诉你通不通。")
        return

    print("⚠️  验证没有通过，但网络是通的（验证组件能连上）。\n")
    print("  说明卡在浏览器这一侧，常见原因有两个：")
    print("    1. 那个复选框需要你亲手点一下才会开始验证 —— 请直接点它；")
    print("    2. 当前出口 IP 被这个站点标成了高风险（机房 IP、共享节点常见），")
    print("       换一个代理节点回来再试，通常就过了。")


def _import_cookie_login(config, source: str) -> int:
    """「复制 Cookie」登录：搬运用户正常浏览器里的登录凭证，绕开验证码。

    适用场景：LinkedIn 在自动化浏览器里 reCAPTCHA 加载不出来（网络/代理
    环境问题），反复登录失败。改为让用户在自己的 Chrome/Edge 里正常登录，
    再复制 li_at / JSESSIONID 两个 Cookie 值粘贴进来即可。

    合规红线：全程只搬运 Cookie，绝不索取、不保存账号密码。
    """
    from hk_job_radar.scrapers.browser import BrowserManager
    from hk_job_radar.scrapers.linkedin import save_session

    browser = BrowserManager(config)
    page = browser.launch(source, force_headless=True)

    print("=" * 62)
    print(f"换一种方式登录 {source}（复制 Cookie，不再和验证码死磕）")
    print("=" * 62)
    print("请先在【你自己的正常浏览器】里登录好 LinkedIn（Chrome/Edge 均可），")
    print("然后按下图步骤把两个 Cookie 值复制过来。\n")

    li_at = input("第 1 步：粘贴 li_at 的值，然后回车 > ").strip()
    if not li_at:
        print("❌ 没有粘贴到任何内容，已取消（未改动任何登录态）。")
        browser.close()
        return 1
    jsessionid = input("第 2 步（可选）：粘贴 JSESSIONID 的值，没有就按回车跳过 > ").strip()

    # 先存到本地文件（每次抓取都会重新注入），再注入当前会话做验证
    save_session(li_at, jsessionid)
    cookies = [{"name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/"}]
    if jsessionid:
        cookies.append({"name": "JSESSIONID", "value": jsessionid,
                        "domain": ".linkedin.com", "path": "/"})

    browser.inject_cookies(source, cookies)

    # 验证登录态：访问 feed 页。未登录会被 302 到 /login 或 /signup；
    # 已登录则停留在 /feed/。这是最干净的判据。
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        # 若 li_at 无效，LinkedIn 可能在登录墙/安全页之间反复跳转，触发
        # 重定向上限。捕获后按"验证失败"处理，给用户可读的中文提示。
        print(f"\n❌ 验证失败：访问 LinkedIn 时出现异常（{exc}）。")
        print("   通常是 li_at 已失效或被 LinkedIn 判定为异地登录。")
        print("   请回浏览器重新复制最新、完整的 li_at 值再试一次。")
        browser.close()
        return 1
    time.sleep(2)   # 留出 JS 重定向时间（若 Cookie 无效会跳登录墙）
    url = page.url or ""
    if any(frag in url for frag in ("/login", "/signup", "authwall", "session_redirect")):
        print("\n❌ 验证失败：页面仍被重定向到了登录墙。")
        print("   常见原因：li_at 已过期、复制不完整、或复制时带了多余空格/引号。")
        print("   请回浏览器重新复制完整的 li_at 值再试一次。")
        browser.close()
        return 1

    print("\n✅ 登录态注入成功！LinkedIn 已识别为已登录状态。")
    print("   Cookie 已保存到本地，之后每次抓取会自动重新注入，无需再做这一步。")
    browser.close()
    return 0


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def _db_path():
    """数据库路径（延迟导入以复用 config 的路径常量）。"""
    from hk_job_radar.config import DATA_DIR, DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def cmd_board(args: argparse.Namespace) -> int:
    """`hk_radar board`：打开岗位工作台（P2 本地网页看板）。

    只读命令的兄弟——它要写 `jobs.status`，所以先跑一次配置校验：
    配置有毛病时在这里给中文提示，比开了网页再一堆 500 好。
    不加载 LLM key、不启动浏览器、不抓取，纯本地服务。
    """
    from hk_job_radar.workbench import serve

    cfg = load_config(require_runnable=False)
    db = Database(str(_db_path()))
    db.init_schema()
    try:
        return serve(db, port=args.port, open_browser=not args.no_browser, cfg=cfg)
    finally:
        db.close()


def cmd_match(args: argparse.Namespace) -> int:
    """`hk_radar match <job_uid>`：简历与岗位逐项对照。

    与 `board` 的分工：board 是日常点按钮的地方，分析属于「要投之前认真看
    一遍」，一次几十秒、要花 token，所以做成显式命令而不是打开就看。
    """
    from hk_job_radar import resume as resume_mod
    from hk_job_radar.matcher import MatchError, ResumeMatcher

    cfg = load_config(require_runnable=False)
    db = Database(str(_db_path()))
    db.init_schema()
    try:
        if not args.job_uid:
            return _list_match_candidates(db)

        job = db.get_job(args.job_uid)
        if job is None:
            print(f"库里没有这个岗位：{args.job_uid}")
            print("去掉参数直接运行 `python run.py match`，会列出可以分析的岗位编号。")
            return 1

        resume_file = cfg.resolve_resume_path()
        if resume_file is None:
            print("还没告诉我在哪份简历上做分析。")
            print("打开 config.yaml，在最后加一行（Windows 路径用正斜杠最稳）：")
            print('  resume_path: "C:/Users/你的用户名/Desktop/简历/CV.docx"')
            print("支持 .docx / .txt / .md；PDF 请先在 Word 里另存为 .docx。")
            return 1

        try:
            resume_text = resume_mod.load_resume_text(resume_file)
        except resume_mod.ResumeError as exc:
            print(f"❌ {exc}")
            return 1

        try:
            matcher = ResumeMatcher(cfg.llm, resume_text)
        except MatchError as exc:
            print(f"❌ {exc}")
            return 1
        signature = matcher.signature()

        result = None if args.force else db.get_analysis(job.job_uid, signature)
        if result:
            print(f"（用上次的分析结果，{result.get('_created_at', '')[:10]}；"
                  "要重算加 --force）\n")
        else:
            print(f"正在分析「{job.title}」…（要调模型，一般 20–60 秒，别关窗口）")
            try:
                result = matcher.analyze(job)
            except MatchError as exc:
                print(f"\n❌ {exc}")
                return 1
            db.save_analysis(job.job_uid, signature, matcher.model, result)

        _print_analysis(job, result)
        return 0
    finally:
        db.close()


def cmd_export(args: argparse.Namespace) -> int:
    """`hk_radar export`：把岗位与投递进展导成文件。

    为什么默认三种都导：用户拿到这份东西的场景多半是「发给别人看」或
    「自己留档」，而 HTML 能看、Excel 能改、PDF 好发——一次都不缺最省事。
    三种都在本地生成，不上传任何地方。
    """
    from hk_job_radar import export as export_mod

    cfg = load_config(require_runnable=False)       # noqa: F841 - 仅做配置校验
    db = Database(str(_db_path()))
    db.init_schema()
    try:
        formats = (("html", "xlsx", "pdf") if args.format == "all"
                   else (args.format,))
        out_dir = Path(args.out) if args.out else PROJECT_ROOT / "exports"
        limit = args.limit if args.limit and args.limit > 0 else export_mod.DEFAULT_LIMIT
        try:
            written, problems = export_mod.export(
                db, out_dir, formats=formats, days=args.days, limit=limit)
        except export_mod.ExportError as exc:
            print(f"❌ {exc}")
            return 1

        if written:
            print(f"导出完成，文件在：{out_dir}")
            for path in written:
                size = path.stat().st_size / 1024
                print(f"  · {path.name}  ({size:.0f} KB)")
            print("\nHTML 双击就能看；PDF 能直接发；Excel 可以自己记账或改。")
            print("（都在本机生成，没有上传到任何地方）")
        for note in problems:
            print(f"\n⚠️  {note}")

        if args.open:
            target = next((p for p in written if p.suffix == ".html"), None)
            if target is not None:
                _open_path(target)
        return 0 if written else 1
    finally:
        db.close()


def cmd_sync_applied(args: argparse.Namespace) -> int:
    """`hk_radar sync-applied`：把招聘网站上的投递记录读回来。

    为什么不做成每天自动跑：每次要起一个浏览器、翻五页，多一次请求就多一分
    被反爬盯上的机会。你投完一批自己点一下，比定时轮询更合适，也更好理解
    「它到底做了什么」。
    """
    from hk_job_radar import applied_sync

    cfg = load_config(require_runnable=False)
    db = Database(str(_db_path()))
    db.init_schema()
    try:
        if args.dry_run:
            print("试跑模式：只读不写，先看看会标哪些、哪些对不上。")
        print("正在读取 JobsDB 的投递记录（Activity → Applied）……")
        print("会打开一个浏览器窗口自动翻页，读完自己关掉，期间不用管它。")
        if args.backfill:
            print("补录模式：对不上的记录会作为新岗位写进库（只有标题/公司/日期）。")
            if args.dry_run:
                print("（同时开了 --dry-run，所以这次只统计，不写。）")
        try:
            report = applied_sync.sync(
                db, cfg,
                headless=True if args.headless else None,
                max_pages=args.pages if args.pages > 0 else applied_sync.MAX_PAGES,
                dry_run=args.dry_run,
                backfill=args.backfill)
        except applied_sync.SyncError as exc:
            print(f"\n❌ {exc}")
            return 1

        print()
        print(report.summary())
        # 默认只列 8 条：真跑起来对不上的可能有几十条，一次刷满屏会盖住
        # 上面那句「标记了几个」。想看全的加 --all。
        for line in report.detail_lines(limit=999 if args.all else 8):
            print(line)
        if args.dry_run:
            print("\n（试跑结束：数据库一个字都没改。去掉 --dry-run 才会真的标。）")
        elif report.marked or report.backfilled:
            print("\n打开工作台（双击 board.bat）就能在「已投」标签下看到它们。")
        return 0
    finally:
        db.close()


def _open_path(path: Path) -> None:
    """用系统默认程序打开文件；打不开也不该让导出失败。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                 # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])   # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
    except Exception as exc:                        # noqa: BLE001
        print(f"（自动打开失败，请自己双击 {path.name}：{exc}）")


def _list_match_candidates(db: Database) -> int:
    """不带参数时：列出最近的高分岗位，让用户挑一个复制编号。"""
    from hk_job_radar.workbench import VIEW_FILTERS

    jobs = db.board_jobs(statuses=VIEW_FILTERS["pending"], limit=12)
    if not jobs:
        print("库里还没有待处理的岗位。先跑 `python run.py once` 抓一轮。")
        return 1
    print("挑一个岗位，复制编号再运行一次，例如：")
    print("  python run.py match jobsdb:95291469\n")
    for job in jobs:
        score = f"{job.llm_score} 分" if job.llm_score is not None else "未打分"
        print(f"  {job.job_uid:<24} {score:<7} {job.title[:34]} @ {job.company[:18]}")
    print("\n（只列最近 12 条待处理岗位，按分数从高到低）")
    return 0


def _print_analysis(job, result: dict) -> None:
    """把分析结果排成终端里能读的样子。

    不输出 Markdown 表格：终端里 `|` 和中文混排对不齐，反而更难读。
    """
    bar = "─" * 62
    print(bar)
    print(f" {job.company} · {job.title}")
    score = result.get("match_score")
    summary = result.get("summary") or ""
    print(f" 契合度：{score if score is not None else '—'} 分" +
          (f" —— {summary}" if summary else ""))
    print(bar)

    matched = result.get("matched") or []
    print("\n✅ 简历里已经对上的：")
    if matched:
        for item in matched:
            print(f"   · {item}")
    else:
        print("   （没有明显对上的点，说明这份简历需要为这个岗位重写）")

    gaps = result.get("gaps") or []
    print("\n⚠️  缺口（按重要性排）：")
    if not gaps:
        print("   （没发现明显缺口）")
    for i, gap in enumerate(gaps, 1):
        print(f"   {i}. {gap.get('skill') or ''}")
        if gap.get("why"):
            print(f"      为什么重要：{gap['why']}")
        if gap.get("how"):
            print(f"      怎么补：{gap['how']}")

    questions = result.get("interview") or []
    if questions:
        print("\n🗣  面试可能会问：")
        for i, qa in enumerate(questions, 1):
            print(f"   {i}. {qa.get('q') or ''}")
            if qa.get("tests"):
                print(f"      在考什么：{qa['tests']}")
            if qa.get("tip"):
                print(f"      回答要点：{qa['tip']}")
    print("\n" + bar)
    print("提醒：「怎么补」只能基于简历里已有的东西改写。如果它建议的是新增经历，"
          "先真的做了再写上去。")
    print(bar)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hk_radar",
        description="香港岗位雷达：定时监控求职网站的新岗位，按你的背景打分后推送到手机。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="启动常驻监控（轮询 + 日报 + 反馈轮询）")
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="抓一轮就退出并推日报（适合每天只开机一小会儿）")
    p_once.set_defaults(func=cmd_once)

    p_report = sub.add_parser("report", help="立即生成今日日报")
    p_report.add_argument("when", nargs="?", default="now", choices=["now"],
                          help="目前仅支持 now（立即推送）")
    p_report.set_defaults(func=cmd_report)

    p_stats = sub.add_parser("stats", help="查看今日抓取量/新岗位数/推送数")
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = sub.add_parser(
        "doctor", help="一键自检：15 项体检，逐项告诉你哪里没配好、怎么修")
    p_doctor.add_argument("--no-llm", action="store_true",
                          help="跳过 AI 连通性实测（默认会真发一次很小请求，只消耗几个 token）")
    p_doctor.set_defaults(func=cmd_doctor)

    p_setup = sub.add_parser(
        "setup", help="打开网页配置向导（在浏览器里点选填配置，不用手改 YAML）")
    p_setup.add_argument("--port", type=int, default=None,
                         help="指定端口（默认从 8765 开始自动找一个空的）")
    p_setup.add_argument("--no-browser", action="store_true",
                         help="不自动打开浏览器，只打印地址")
    p_setup.add_argument("--chain", action="store_true",
                         help="由安装脚本串联调用：保存后提示「我接着往下走」"
                              "（单独运行 setup.bat 时不需要这个开关）")
    p_setup.set_defaults(func=cmd_setup)

    p_guide = sub.add_parser(
        "guide", help="打印安装流程里的中文提示（供安装脚本调用，平时用不到）")
    p_guide.add_argument("key", nargs="?", default=None,
                         help="提示语名称；省略则列出全部可用的")
    p_guide.set_defaults(func=cmd_guide)

    p_notify = sub.add_parser("notify", help="推送通道相关操作")
    notify_sub = p_notify.add_subparsers(dest="notify_action", required=True)
    p_notify_test = notify_sub.add_parser(
        "test", help="给已启用的通道发一条测试消息（5 秒验证配置是否正确）")
    p_notify_test.set_defaults(func=cmd_notify_test)

    p_apply = sub.add_parser("apply", help="投递副驾：打开申请页、答好选择题，提交由你点")
    p_apply.add_argument("job_id", help="岗位 ID（job_uid，如 jobsdb:12345678）")
    p_apply.add_argument("--probe", action="store_true",
                         help="试跑：只报告「本来会怎么选」，不往页面写任何值")
    p_apply.set_defaults(func=cmd_apply)

    p_login = sub.add_parser("login", help="打开浏览器手动登录指定站点（首次配置用）")
    p_login.add_argument("source", choices=["jobsdb", "linkedin", "indeed"],
                         help="站点名")
    p_login.add_argument("--cookie", action="store_true",
                         help="换一种方式：复制你自己浏览器里的 Cookie 登录（绕开验证码）")
    p_login.set_defaults(func=cmd_login)

    p_board = sub.add_parser(
        "board", help="打开岗位工作台（本地网页看板，点按钮记投递进度）")
    p_board.add_argument("--port", type=int, default=0,
                         help="指定端口（默认从 8787 起自动找一个空的）")
    p_board.add_argument("--no-browser", action="store_true",
                         help="不自动打开浏览器，只打印地址")
    p_board.set_defaults(func=cmd_board)

    p_match = sub.add_parser(
        "match", help="简历与岗位逐项对照：缺口清单 + 可能的面试问题")
    p_match.add_argument("job_uid", nargs="?", default=None,
                         help="岗位编号（形如 jobsdb:95291469）；省略则列出可选的岗位")
    p_match.add_argument("--force", action="store_true",
                         help="忽略上次结果、重新调用模型（简历改了不必加，指纹会自动失效）")
    p_match.set_defaults(func=cmd_match)

    p_export = sub.add_parser(
        "export", help="把岗位与投递进展导出成 HTML / Excel / PDF")
    p_export.add_argument("--format", "-f", default="all",
                          choices=["all", "html", "xlsx", "pdf"],
                          help="导出格式，默认三种都导")
    p_export.add_argument("--out", "-o", default=None,
                          help="输出目录（默认是项目下的 exports）")
    p_export.add_argument("--days", type=int, default=30,
                          help="统计口径看最近多少天（默认 30）")
    p_export.add_argument("--limit", type=int, default=0,
                          help="明细最多导多少条（默认 500）")
    p_export.add_argument("--open", action="store_true",
                          help="导出后自动打开 HTML")
    p_export.set_defaults(func=cmd_export)

    p_sync = sub.add_parser(
        "sync-applied",
        help="读取 JobsDB 上的投递记录，自动把对应岗位标成「已投」")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="只读不写：先看看会标哪些、哪些岗位对不上")
    p_sync.add_argument("--pages", type=int, default=0,
                        help="最多读几页（默认按站点上限，一般 5 页就到底）")
    p_sync.add_argument("--headless", action="store_true",
                        help="不显示浏览器窗口（默认显示，过一次验证更容易）")
    p_sync.add_argument("--all", action="store_true",
                        help="列出全部「库里找不到」的岗位（默认只显示前 8 条）")
    p_sync.add_argument("--backfill", action="store_true",
                        help="对不上但站点给了岗位编号的，补录进库（只有标题/公司/日期）")
    p_sync.set_defaults(func=cmd_sync_applied)
    return parser


def main() -> int:
    """CLI 总入口：run.py 调用这里。返回进程退出码。"""
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        # 配置问题给用户看得懂的中文提示，不抛 traceback
        print(f"❌ 配置错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception(f"命令执行失败：{args.command}")
        print(f"❌ 执行失败：{exc}（详见 data/logs/ 下日志）", file=sys.stderr)
        return 1
