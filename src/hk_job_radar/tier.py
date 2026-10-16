"""免费版 / 高级版分层（tier）——产品化的「限制地基」。

★ 开始前必须理解的一件事 ★
本仓库是 MIT 开源的，免费版的所有代码都在用户自己的电脑上，可读、可改。
所以**任何写在免费版源码里的限制都只是「软锁」**——用户把 7 改成 999 就破了。
这一点无法回避，也不必回避。

因此本模块的定位不是「防盗」，而是三件事：

1. **产品契约的单一出处**：免费版到底给多少额度，只在这一个文件里定义。
   以后想调额度（比如把 7 改成 5）只改这里，不会漏掉某个调用点。

2. **友好降级，而不是报错**：超限时【不崩、不报错】，只用前面几个 + 打一行
   中文提示。新手最怕的就是「我明明填了却不生效」的静默失败——宁可少给，
   也要说清楚为什么少给。

3. **升级引导**：提示里自然带上高级版说明，完成一次温和的转化触达。

真正的「锁」不在这个文件里，而在一个事实：**高级版功能不进公开仓库**。
多通道、实时秒推、趋势看板、投递管理这些功能的源码只存在于闭源的
premium_pack 里，用户改免费版的代码也变不出这些能力——那才是有效的防线。

作者 / 开发者想用完整功能：
    设环境变量 HK_RADAR_TIER=premium，或在项目根目录放一个非空的 premium.key
    （premium.key 已被 .gitignore 的 *.key 规则挡住，不会进仓库）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from hk_job_radar.config import PROJECT_ROOT

if TYPE_CHECKING:            # 只用于类型标注，避免与 config 形成循环导入
    from hk_job_radar.config import AppConfig

# ---------------------------------------------------------------------------
# 免费版额度（产品契约的唯一定义处）
# ---------------------------------------------------------------------------
FREE_MAX_KEYWORDS = 7        # 关键词总数上限（普通关键词 + 毕业生项目关键词合计）
FREE_MAX_CHANNELS = 1        # 推送通道数量上限（四个通道完全平等，任选其一）
FREE_REALTIME_PUSH = False   # 是否允许「高分岗位不等日报、发现即推」
FREE_DAILY_REPORT_TIMES = 1  # 每天定时日报的次数（手动 `report now` 不计入）

# ---------------------------------------------------------------------------
# 通道登记表
#
# ⚠️ 这里的顺序只是「展示 / 校验」用的固定顺序，【不是优先级】。
#    四个通道完全平等：免费版允许用户任选其一，做法是把想用的那个写在
#    config.yaml 的 notify.channels 列表第一位。系统不会替用户决定用哪个。
# ---------------------------------------------------------------------------
VALID_CHANNELS: tuple[str, ...] = ("wecom", "wxpusher", "email", "telegram")

CHANNEL_LABELS: dict[str, str] = {
    "wecom": "企业微信",
    "wxpusher": "WxPusher",
    "email": "邮件",
    "telegram": "Telegram",
}

# 升级引导语（不写死任何网址：高级版入口确定后再替换这一行即可）
UPGRADE_HINT = "想解锁完整额度？见 README 的「免费版与高级版」一节。"

# 作者 / 开发者解锁完整功能用的环境变量
_TIER_ENV = "HK_RADAR_TIER"

# 激活码（premium.key）的查找顺序。
# ★ 首选放在【用户主目录】而不是项目目录里，是一个刻意的安全设计：
#   项目目录会被整个打包成 ZIP 分发，如果 key 放在里面，一不小心就会把
#   完整版功能连同密钥一起发给所有用户。放在 ~/.hk_job_radar/ 就绝不会被误打包。
#   第二条只是方便就地调试，正式分发时不要用它。
_PREMIUM_KEY_PATHS: tuple[Path, ...] = (
    Path.home() / ".hk_job_radar" / "premium.key",
    PROJECT_ROOT / "premium.key",
)


# ---------------------------------------------------------------------------
def is_premium() -> bool:
    """当前是否运行在高级版。

    判定顺序：
      1) 环境变量 HK_RADAR_TIER=premium
      2) ~/.hk_job_radar/premium.key（首选，项目目录之外）
      3) 项目根目录的 premium.key（仅便于就地调试）
    三条都查不到 → 免费版。
    """
    if (os.environ.get(_TIER_ENV) or "").strip().lower() == "premium":
        return True
    for key_path in _PREMIUM_KEY_PATHS:
        try:
            if key_path.is_file() and key_path.read_text(encoding="utf-8-sig").strip():
                return True
        except OSError:
            continue
    return False


def channel_label(name: str) -> str:
    """通道机器名 → 给用户看的中文名（未知名字原样返回，方便排障）。"""
    return CHANNEL_LABELS.get(name, name)


def realtime_push_enabled() -> bool:
    """是否允许高分岗位即时推送（免费版不含此功能）。"""
    return is_premium() or FREE_REALTIME_PUSH


def effective_report_times(cfg: "AppConfig") -> list[str]:
    """实际生效的每日日报时间点列表。

    免费版只有 report_time 一个时间点（天然就是「每天 1 次」）；
    高级版可以配 report_times 多个时间点。手动 `report now` 不受此限制。
    """
    if is_premium():
        times = [str(t).strip() for t in (getattr(cfg, "report_times", []) or []) if str(t).strip()]
        if times:
            return times
    return [cfg.report_time]


# ---------------------------------------------------------------------------
def apply_free_limits(cfg: "AppConfig") -> list[str]:
    """把配置就地裁剪到免费版额度，返回「给用户看的提示」列表。

    ★ 关键设计：降级而不是报错。 ★
    超限时程序照常运行，只是忽略多余的部分，并返回一行说明。
    返回空列表 = 没有触发任何限制（不打扰用户）。
    """
    if is_premium():
        return []

    notes: list[str] = []

    # ① 关键词总数：普通关键词优先，其次毕业生项目关键词（与配置文件里的书写顺序一致）
    total = len(cfg.keywords) + len(cfg.graduate_keywords)
    if total > FREE_MAX_KEYWORDS:
        kept_kw = cfg.keywords[:FREE_MAX_KEYWORDS]
        remain = FREE_MAX_KEYWORDS - len(kept_kw)
        kept_gk = cfg.graduate_keywords[:remain]
        used = len(kept_kw) + len(kept_gk)
        cfg.keywords, cfg.graduate_keywords = kept_kw, kept_gk
        notes.append(
            f"关键词：免费版最多 {FREE_MAX_KEYWORDS} 个，已按顺序启用前 {used} 个，"
            f"忽略其余 {total - used} 个。")

    # ② 推送通道：只保留列表里的第 1 个（用户自定义的「用哪个」，不是系统指定）
    channels = list(cfg.notify.channels or [])
    if len(channels) > FREE_MAX_CHANNELS:
        kept = channels[:FREE_MAX_CHANNELS]
        dropped = [channel_label(x) for x in channels[FREE_MAX_CHANNELS:]]
        cfg.notify.channels = kept
        notes.append(
            f"推送通道：免费版只支持 {FREE_MAX_CHANNELS} 个，已启用「{channel_label(kept[0])}」，"
            f"忽略「{'、'.join(dropped)}」。")

    # ③ 每天 1 次日报：由 effective_report_times() 在调度层生效，这里不裁剪配置
    #    （用户写 report_times 不报错，只是免费版只认 report_time 那一个）。

    # ④ 实时秒推：由 realtime_push_enabled() 在流水线层生效。
    #    这里刻意不提示——没开秒推需求的用户不该被反复打扰，
    #    高分岗位无论如何都会出现在每日日报里，功能并未缺失。

    return notes


def log_limits(notes: list[str]) -> None:
    """把免费版限制提示打到控制台与日志（用户可见、也便于事后排障）。"""
    for note in notes:
        logger.warning(f"[免费版] {note}")
    if notes:
        logger.info(f"[免费版] {UPGRADE_HINT}")


__all__ = [
    "FREE_MAX_KEYWORDS", "FREE_MAX_CHANNELS", "FREE_REALTIME_PUSH",
    "FREE_DAILY_REPORT_TIMES",
    "VALID_CHANNELS", "CHANNEL_LABELS", "UPGRADE_HINT",
    "is_premium", "channel_label", "realtime_push_enabled",
    "effective_report_times", "apply_free_limits", "log_limits",
]
