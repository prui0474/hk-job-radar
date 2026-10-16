"""CLI 命令实现（架构第 8 节）：run / once / report / stats / apply / login。

run.py 只负责把 src/ 加入 sys.path 并调用这里的 main()——所有命令的
装配（config → db → pipeline → scheduler）都集中于此，方便学习者从
入口开始单步跟踪整个系统的启动路径。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from loguru import logger

from hk_job_radar import tier
from hk_job_radar.config import (
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
    print(f"  实时推送（≥{config.realtime_threshold} 分）   {summary.get('pushed_realtime', 0)} 个"
          f"{'' if tier.realtime_push_enabled() else '（免费版不含秒推，已并入日报）'}")
    print(f"  推送通道：{'、'.join(pipeline.notifier.labels) or '（未配置）'}")
    print(f"  日报：{'已推送 ✅' if sent else '本次无需推送（今日已推过 / 暂无新岗位）'}")
    print("─" * 52)
    if not sent:
        print("提示：日报今天已经推送过了，新岗位会累计到明天那份；")
        print("      想强制再要一份，双击 report_now.bat（Mac 为 report_now.command）。")
    print("✅ 本轮结束，可以关闭本窗口（直接关机也行）。")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """`hk_radar stats`：打印今日运行状态（P0-8）。只读命令，不需要任何凭据。"""
    config = load_config()   # 仅用于校验提示；统计本身只依赖数据库
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
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """`hk_radar apply <job_id>`：投递副驾（T05）。

    完整流程：
      取岗位 → 选简历 → 打开申请页 → 填充字段/上传简历/生成筛选问题答案
      → 【逐字段人工确认】→ 提示用户在浏览器中亲手提交 → 投递留档

    合规红线：本函数不点击任何提交按钮，提交动作完全由用户完成。
    """
    config = load_config()
    db = Database(str(_db_path()))

    job = db.get_job(args.job_id)
    if job is None:
        print(f"❌ 找不到岗位：{args.job_id}")
        print("   提示：job_uid 形如 jobsdb:12345678；可用 `python run.py stats` 查看，"
              "或到日报卡片里复制岗位 ID。")
        return 1

    # 延迟导入：copilot 依赖 playwright，stats/login 等命令不应被它拖累
    from hk_job_radar.copilot.confirm import ConfirmGate
    from hk_job_radar.copilot.form_filler import FormFiller
    from hk_job_radar.copilot.profile import ProfileStore
    from hk_job_radar.copilot.qa_engine import QAEngine
    from hk_job_radar.config import DATA_DIR
    from hk_job_radar.models import ApplicationRecord, now_hk
    from hk_job_radar.scrapers.browser import BrowserManager

    resumes_dir = DATA_DIR / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    if not any(resumes_dir.iterdir()):
        print(f"⚠️  {resumes_dir} 目录下没有简历文件。")
        print("   请把英文简历（PDF，如 resume_DS_en_v2.pdf）放进去，副驾才能自动上传。")

    store = ProfileStore()
    profile = store.load()
    browser = BrowserManager(config)
    filler = FormFiller(config, browser, store, QAEngine(config.llm, profile))

    print(f"\n🚗 投递副驾启动：{job.title} @ {job.company}")
    print(f"   链接：{job.url}\n")

    try:
        result = filler.fill(job)
        page = browser.launch("apply", force_headless=False)   # 复用同一页面供编辑
        decision = ConfirmGate().review(result, page)
    except Exception as exc:
        logger.exception(f"投递副驾执行失败：{exc}")
        print(f"❌ 副驾执行失败：{exc}（详见 data/logs/ 日志）")
        return 1
    finally:
        pass   # 浏览器在用户完成提交后再关（close 在下方分支里调用）

    if decision != "confirmed":
        browser.close()
        print("已放弃本次投递（未留档）。")
        return 0

    print("\n" + "=" * 62)
    print("请在弹出的浏览器窗口中：")
    print("  1) 逐项核对上面的字段（尤其是 🔶 AI 生成的答案）；")
    print("  2) 补齐【需你手动处理】的项目；")
    print("  3) 亲手点击 Submit / 提交按钮。")
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
        db.update_status(job.job_uid, "applied")
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
    page.goto(entry_urls[source], wait_until="domcontentloaded", timeout=60000)

    print("=" * 60)
    print(f"已打开 {source} 浏览器窗口。请在浏览器中完成：")
    print("  1) 登录你的账号（LinkedIn 建议使用求职专用小号）；")
    print("  2) 如出现 Cloudflare/验证码页面，手动完成验证；")
    print("  3) 确认页面能正常浏览后，回到本窗口按回车。")
    print("（登录态会保存在 data/browser/ 目录，后续抓取自动复用）")
    print("=" * 60)
    try:
        input("完成后按回车关闭浏览器 > ")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        browser.close()
    print("✅ 登录态已持久化。可运行 `python run.py stats` 或 `python run.py run` 开始使用。")
    return 0


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hk_radar",
        description="香港岗位雷达：定时监控求职网站的新岗位并推送 Telegram 日报。",
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

    p_apply = sub.add_parser("apply", help="投递副驾（T05 交付）")
    p_apply.add_argument("job_id", help="岗位 ID（job_uid，如 jobsdb:12345678）")
    p_apply.set_defaults(func=cmd_apply)

    p_login = sub.add_parser("login", help="打开浏览器手动登录指定站点（首次配置用）")
    p_login.add_argument("source", choices=["jobsdb", "linkedin", "indeed"],
                         help="站点名")
    p_login.add_argument("--cookie", action="store_true",
                         help="换一种方式：复制你自己浏览器里的 Cookie 登录（绕开验证码）")
    p_login.set_defaults(func=cmd_login)
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
