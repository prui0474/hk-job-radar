"""安装流程里的中文提示语。

为什么这些文案放在 Python 里，而不直接写进 .bat
--------------------------------------------------
Windows 的 cmd.exe 会用「当前代码页」去解析批处理文件。简体中文系统上
默认是 936(GBK)，于是写在 .bat 里的中文会被整片解析成乱码，甚至让某个
字符被当成命令分隔符，报出莫名其妙的错。本项目因此规定 .bat 必须纯
ASCII（CI 里有一条检查专门盯这个）。

但 Windows 用户最需要的恰恰是中文说明。折中办法就是：.bat 里只留最短的
英文步骤标题，所有长句交给 Python 打印 —— Python 的输出编码由
PYTHONUTF8=1 控制，不受代码页影响，中文一直是好的。

附带好处：Mac 的 .command 可以直接写中文，但两端共用这一份文案后，
改一次两边同步，不会出现「Mac 上说的和 Windows 上说的不一样」。

用法（由 install.bat / install.command 调用）：
    python run.py guide need-save
    python run.py guide            # 列出所有可用的 key
"""
from __future__ import annotations

import sys
from typing import Sequence

#: 每段提示用「行元组」而不是一整块大字符串：
#: 这样每一行在源码里就是独立的一行，改哪行一眼能看见，
#: 也不会因为编辑器自动折行而在屏幕上多出奇怪的空白。
_MESSAGES: dict[str, tuple[str, ...]] = {
    # 用户打开了向导，却没点「保存配置」就把窗口关了
    "need-save": (
        "",
        "-" * 60,
        " 配置还没有保存。",
        "",
        " 你打开了配置页面，但没有点最下面的「保存配置」，",
        " 所以文件一个字节都没有改动。",
        "",
        " 再双击一次 install.bat，这次记得填完点「保存配置」，",
        " 然后点「关闭向导」。",
        "-" * 60,
        "",
    ),
    # 向导压根没起来（端口被占、页面文件缺失等）
    "wizard-failed": (
        "",
        "-" * 60,
        " 配置向导没能启动。",
        "",
        " 常见原因是端口被别的程序占着，或者项目文件没有解压完整。",
        " 往上翻一下刚才的报错，它通常会写明具体原因。",
        " 看不懂的话，把整个窗口截图发我。",
        "-" * 60,
        "",
    ),
    # 体检有 ❌
    "doctor-failed": (
        "",
        "-" * 60,
        " 体检没有全部通过。",
        "",
        " 上面标着 ❌ 的那几项要先修好，每一项右边都写了该怎么改。",
        " 按着改完，再双击一次 install.bat 就行（已装好的东西会跳过）。",
        "",
        " 自己搞不定的话，把整个窗口截图发我。",
        "-" * 60,
        "",
    ),
    # 体检全绿，准备过一次人机验证
    "verify-intro": (
        "",
        "=" * 60,
        " 最后一步：过一次 JobsDB 的人机验证",
        "=" * 60,
        "",
        " 马上会弹出一个浏览器窗口，就是以后抓岗位用的那个浏览器。",
        " 你只需要做一件事：在里面勾一下「我是人类」，",
        " 等页面正常显示出岗位列表就可以了。",
        "",
        " 为什么要你亲手点：这道验证是用来区分人和程序的。",
        " 程序替你点既不尊重对方的规则，也很容易失效。",
        " 你自己过一次，之后一个多月都不用再管它。",
        "",
        " 如果你现在不方便，直接关掉那个窗口也行，不影响后面使用。",
        "=" * 60,
        "",
    ),
    # 验证没过
    "verify-skip": (
        "",
        "-" * 60,
        " 这次没过。不影响继续使用，只是可能抓到的岗位少一些。",
        "",
        " 想补上：双击 login_jobsdb.bat（Mac 上是 login_jobsdb.command）。",
        " 想知道卡在哪：双击 doctor.bat，看第 5 项「网络出口」。",
        "-" * 60,
        "",
    ),
    # 首次安装全部走完
    "done": (
        "",
        "=" * 60,
        " 全部就绪。",
        "=" * 60,
        "",
        " 现在双击 start.bat 就开始找工作了。",
        "",
        " 每天 09:00 会自动跑一轮，把日报推到你选的通道；",
        " 新岗位 AI 会按你的画像打分，80 分以上的会单独提醒你。",
        "",
        " 以后想改关键词或推送通道：双击 setup.bat",
        " 觉得哪里不对劲：        双击 doctor.bat",
        "",
        " 完整图文教程（中文）：docs/getting-started-zh.md",
        "=" * 60,
        "",
    ),
    # 重复运行 install（环境已就绪，配置没动）
    "repeat-done": (
        "",
        "=" * 60,
        " 环境已就绪，你的配置保持原样。",
        "=" * 60,
        "",
        " 想改配置：双击 setup.bat",
        " 直接开工：双击 start.bat",
        "=" * 60,
        "",
    ),
}


def message(key: str) -> str:
    """返回 key 对应的一整段提示语（含首尾空行，方便直接 print）。

    key 不存在时抛 KeyError —— 让调用方显式处理，而不是悄悄打印一片空白。
    """
    return "\n".join(_MESSAGES[key])


def available() -> list[str]:
    """所有可用的 key，按字母序。"""
    return sorted(_MESSAGES)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：`guide <key>`。省略 key 时列出全部。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法：python run.py guide <key>")
        print("可用的 key：" + "、".join(available()))
        return 2

    key = args[0]
    try:
        print(message(key))
    except KeyError:
        print(f"没有这段提示语：{key}")
        print("可用的 key：" + "、".join(available()))
        return 2
    return 0


__all__ = ["message", "available", "main"]
