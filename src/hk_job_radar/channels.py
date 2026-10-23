"""推送通道登记表。

只做一件事：把配置文件里写的通道机器名（`telegram` / `wecom` …）和
给用户看的中文名对应起来，并在配置校验时提供「合法取值的唯一出处」。

⚠️ 这里的顺序只是「展示与校验」用的固定顺序，**不是优先级**。
   四个通道完全平等，用哪个由配置里 `notify.channels` 的书写顺序决定，
   系统不会替用户挑。
"""
from __future__ import annotations

VALID_CHANNELS: tuple[str, ...] = ("wecom", "wxpusher", "email", "telegram")

CHANNEL_LABELS: dict[str, str] = {
    "wecom": "企业微信",
    "wxpusher": "WxPusher",
    "email": "邮件",
    "telegram": "Telegram",
}


def channel_label(name: str) -> str:
    """通道机器名 → 给用户看的中文名（未知名字原样返回，方便排障）。"""
    return CHANNEL_LABELS.get(name, name)


__all__ = ["VALID_CHANNELS", "CHANNEL_LABELS", "channel_label"]
