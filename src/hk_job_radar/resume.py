"""简历解析：把简历文件读成纯文本，交给匹配器送进模型。

为什么不用现成的解析库
----------------------
`.docx` 本质是个 zip，正文在 `word/document.xml` 里，标准库 `zipfile` +
`xml.etree` 就够。多装一个 `python-docx` 意味着多一条依赖、多一个可能装不上的
环节，而这个项目对用户承诺的是「解压即运行」。简历只有几页，解析质量要求不高。

PDF 为什么不做
--------------
PDF 里的文字是按坐标画的，抽文本要靠额外的库（pypdf / pdfminer）。为了一个
「顺便也能读 PDF」的便利引入依赖，不划算。这里给出**明确的中文提示**，告诉
用户另存为 .docx 或 .txt——比装个库然后抽出乱码强。

文本上限
--------
简历通常 1-3 页、几千字。设 20000 字符上限是为了防「误选了整个作品集 PDF 转
出来的文本」这类情况，把 token 烧在一次分析上。
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from loguru import logger

#: 认得的扩展名。`.doc`（97-2003 老格式）不在其中——那是二进制格式，
#: 需要 Word/ LibreOffice 才能读，只能请用户另存。
SUPPORTED_SUFFIXES = (".txt", ".md", ".markdown", ".docx")

#: 送进模型的文本上限（字符）。
MAX_RESUME_CHARS = 20000

#: docx 正文的 XML 命名空间（Word 2007 以后固定这个）。
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeError(Exception):
    """简历读不出来。消息一律是给用户看的中文，直接 print 即可。"""


def load_resume_text(path: str | Path, limit: int = MAX_RESUME_CHARS) -> str:
    """读简历 → 纯文本。读不出来抛 `ResumeError`（消息可直接展示）。

    超长时截断并记一条日志：截断不是错误，但要留下痕迹，否则用户会奇怪
    「为什么我简历后半段没被算进去」。
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise ResumeError(f"找不到简历文件：{p}")
    if not p.is_file():
        raise ResumeError(f"这个路径不是文件：{p}")

    suffix = p.suffix.lower()
    if suffix == ".docx":
        text = _docx_text(p)
    elif suffix in (".txt", ".md", ".markdown"):
        text = _plain_text(p)
    elif suffix == ".pdf":
        text = _pdf_text(p)
    elif suffix == ".doc":
        raise ResumeError(
            f".doc 是老版 Word 格式，读不了。请用 Word 打开 {p.name}，"
            "「另存为」→ 选择「Word 文档 (.docx)」，再把 config.yaml 里的 "
            "resume_path 指向新的 .docx。")
    else:
        raise ResumeError(
            f"不认识的文件类型：{suffix or '（没有扩展名）'}。"
            f"支持：{'、'.join(SUPPORTED_SUFFIXES)}（PDF 见下方提示）。")

    text = _tidy(text)
    if not text:
        raise ResumeError(
            f"{p.name} 里没读到任何文字。"
            "如果是扫描件（图片版 PDF / 拍照），需要先做文字识别再另存为 .docx。")
    if len(text) > limit:
        logger.info(f"简历文本 {len(text)} 字，超过上限 {limit}，已截断后送进模型。")
        text = text[:limit]
    return text


# ---------------------------------------------------------------------------
# 各格式的具体读法
# ---------------------------------------------------------------------------
def _plain_text(path: Path) -> str:
    """纯文本：先按 UTF-8 读，失败再试 GB18030（国内老简历常见）。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise ResumeError(f"读不了 {path.name}：{exc}") from exc
    raise ResumeError(
        f"{path.name} 的编码认不出来。请用记事本打开、另存为 UTF-8 再试。")


def _docx_text(path: Path) -> str:
    """从 word/document.xml 里按段落抽文字。

    只取 `w:t`（文字节点），遇到 `w:tab` 补制表符、`w:br`/`w:cr` 补换行——
    简历里「技能 | Excel | SQL」这类同一行分栏的信息全靠这些节点才不会黏成
    一坨。表格单元格里也是 `w:p`，所以会被同一套逻辑抓到。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except KeyError as exc:
        raise ResumeError(
            f"{path.name} 里没有正文（word/document.xml）。"
            "可能是个空文件，或者是被改了扩展名的其它格式。") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise ResumeError(
            f"{path.name} 打不开：{exc}。"
            "如果是 Word 正在编辑这个文件，先保存并关闭再试。") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ResumeError(f"{path.name} 的正文结构损坏，无法解析：{exc}") from exc

    lines: list[str] = []
    for para in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
        elif lines:
            # 空段落＝用户敲的那一下回车，正是「教育经历」和「工作经历」之间的分隔。
            # 全丢掉的话两个板块会连在一起，模型容易把时间线读串。
            # 连续多个空段落交给 _tidy() 压成一个。
            lines.append("")
    return "\n".join(lines)


def _pdf_text(path: Path) -> str:
    """PDF：装了 pypdf 就读，没装就给一句人话，别让用户对着报错猜。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeError(
            f"PDF 需要额外装一个程序才能读。两条路选一条：\n"
            f"  1）用 Word 打开 {path.name} →「另存为」→ Word 文档 (.docx)，"
            "再把 resume_path 指向 .docx（推荐，最省事）；\n"
            "  2）在项目目录执行：.venv\\Scripts\\pip install pypdf，然后重试。") from None
    try:
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:                       # noqa: BLE001 - PDF 库的异常五花八门
        raise ResumeError(f"{path.name} 解析失败：{exc}") from exc
    return "\n".join(pages)


def _tidy(text: str) -> str:
    """统一换行、去掉行尾空白和连续空行。

    连续空行对模型没有信息量，只占 token；但**段落之间的空行要保留一行**，
    否则「教育经历 / 工作经历」会连成一片，模型容易把时间线读串。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
