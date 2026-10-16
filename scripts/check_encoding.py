"""仓库编码 / 换行符规范自检（本地与 CI 共用，无第三方依赖）。

为什么需要这个脚本
------------------
本项目踩过两次「我这台机器好好的、别人下载下来就崩」的坑，两次的根因都不是
业务逻辑，而是**编码与换行符**。规则写在文档里没人会记得，所以固化成可执行的
检查：本地改完代码跑一下、每次提交由 CI 跑一遍。

规则
----
1. ``*.bat`` 必须 **CRLF + 纯 ASCII**
     - cmd.exe 按行解析批处理，LF 换行会让它解析错位，中文会被当成命令执行
       （报「'xxx' 不是内部或外部命令」）；
     - cmd.exe 对批处理里的多字节字符解析不可靠（UTF-8 无 BOM 会丢字节、
       带 BOM 会把 BOM 当命令），所以 .bat 里一个非 ASCII 字符都不能有。
       中文提示交给 Python 程序和文档承担。

2. ``*.command`` / ``*.sh`` 必须 **LF + 无 BOM**
     - CRLF 会让 bash 报 ``$'\\r': command not found``；
     - BOM 会顶掉 shebang（``#!/bin/bash`` 不再是文件开头），脚本直接失效。

3. ``requirements.txt`` 首行必须是 PEP263 编码声明
     - 没有它、文件里又有中文注释时，pip 会回落到系统首选编码
       （简体中文 Windows = cp936/GBK）→ ``UnicodeDecodeError: 'gbk' codec
       can't decode byte ...``。加一行声明即可明确指定 UTF-8。

用法
----
    python scripts/check_encoding.py          # 在本项目根目录下运行

退出码：0 = 全部合规；1 = 存在问题（并逐条打印文件与原因）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 项目根目录 = 本文件的上一级目录
ROOT = Path(__file__).resolve().parent.parent

# requirements.txt 首行必须以此开头（pip 的 PEP263 识别要求前两行内、行首为 #）
REQUIREMENTS_HEADER = "# -*- coding: utf-8 -*-"


def _read(path: Path) -> bytes:
    return path.read_bytes()


def check_bat(path: Path) -> list[str]:
    data = _read(path)
    problems = []
    crlf = data.count(b"\r\n")
    bare_lf = data.count(b"\n") - crlf
    if bare_lf:
        problems.append(f"有 {bare_lf} 个裸 LF 换行（.bat 必须全部 CRLF，否则 cmd 解析错位）")
    if crlf == 0 and data:
        problems.append("完全没有 CRLF 换行（.bat 必须是 CRLF）")
    non_ascii = [b for b in data if b > 127]
    if non_ascii:
        problems.append(
            f"含 {len(non_ascii)} 个非 ASCII 字节（.bat 里不能出现中文等非 ASCII 字符，"
            f"首次出现在第 {min(i for i, b in enumerate(data) if b > 127)} 字节）"
        )
    return problems


def check_shell_script(path: Path) -> list[str]:
    data = _read(path)
    problems = []
    if data.startswith(b"\xef\xbb\xbf"):
        problems.append("带 UTF-8 BOM（会让 shebang 失效，脚本无法运行）")
    crlf = data.count(b"\r\n")
    if crlf:
        problems.append(f"有 {crlf} 个 CRLF 换行（Shell 脚本必须全部 LF）")
    first_line = data.split(b"\n", 1)[0]
    if not first_line.startswith(b"#!"):
        problems.append("第一行不是 shebang（#!/bin/bash）")
    return problems


def check_requirements(path: Path) -> list[str]:
    data = _read(path)
    problems = []
    if data.startswith(b"\xef\xbb\xbf"):
        problems.append("带 UTF-8 BOM（pip 虽能识别，但与项目约定不符，建议去掉）")
    first_line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    if first_line != REQUIREMENTS_HEADER:
        problems.append(
            f"首行不是编码声明。\n"
            f"        当前：{first_line!r}\n"
            f"        应为：{REQUIREMENTS_HEADER!r}\n"
            f"        否则 pip 在简体中文 Windows（cp936）上会解码失败。"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append(f"文件本身不是合法 UTF-8：{exc}")
    return problems


def check_gitattributes(root: Path) -> list[str]:
    """确认 .gitattributes 真的锁定了这些换行规则（否则本机对了、别人 clone 还是坏的）。"""
    path = root / ".gitattributes"
    if not path.exists():
        return [".gitattributes 不存在（换行符规则不会被 git 固化）"]
    text = path.read_text(encoding="utf-8")
    required = ["*.bat text eol=crlf", "*.sh  text eol=lf", "*.command text eol=lf"]
    return [f".gitattributes 缺少规则：{r}" for r in required if r not in text]


def main() -> int:
    print(f"编码 / 换行符规范自检  ·  项目根目录：{ROOT}")
    print("-" * 72)

    groups = [
        ("*.bat（必须 CRLF + 纯 ASCII）", sorted(ROOT.glob("*.bat")), check_bat),
        ("*.command / *.sh（必须 LF + 无 BOM）",
         sorted(ROOT.glob("*.command")) + sorted((ROOT / "scripts").glob("*.sh")),
         check_shell_script),
        ("requirements.txt（首行必须是编码声明）",
         [ROOT / "requirements.txt"] if (ROOT / "requirements.txt").exists() else [],
         check_requirements),
    ]

    failed = 0
    checked = 0
    for title, files, checker in groups:
        print(f"\n[{title}]")
        if not files:
            print("  （没有匹配的文件，跳过）")
            continue
        for f in files:
            if not f.exists():
                continue
            checked += 1
            problems = checker(f)
            if problems:
                failed += 1
                print(f"  ✗ {f.name}")
                for p in problems:
                    print(f"      - {p}")
            else:
                print(f"  ✓ {f.name}")

    print("\n[.gitattributes 规则覆盖]")
    ga_problems = check_gitattributes(ROOT)
    if ga_problems:
        failed += 1
        for p in ga_problems:
            print(f"  ✗ {p}")
    else:
        print("  ✓ 已锁定 *.bat=crlf、*.sh=lf、*.command=lf")

    print("\n" + "-" * 72)
    if failed:
        print(f"❌ 有 {failed} 个文件不合规（共检查 {checked} 个）。")
        print("   修法见本脚本文件开头的说明，或 git log 里的相关提交。")
        return 1
    print(f"✅ 全部合规（共检查 {checked} 个文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
