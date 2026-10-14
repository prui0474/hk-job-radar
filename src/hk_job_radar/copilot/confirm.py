"""人工确认关卡（T05）——合规红线的最后一道闸门。

为什么这一关不可配置关闭：
1. 平台条款层面：LinkedIn/Indeed 明文禁止第三方自动化投递，系统若出现
   "自动提交"代码路径，账号风险由用户承担而用户可能并不知情；
2. 结果层面：表单字段识别有误时（尤其 visa、薪资、入职时间这类问题），
   自动提交会把错误答案直接发给雇主，代价远高于多花 30 秒人工看一眼。

因此本模块的终态永远是同一句话：请在浏览器中亲手检查并点击提交。
"""
from __future__ import annotations

from loguru import logger

from hk_job_radar.copilot.form_filler import FillResult

# 来源 → 展示标记：让"哪些是机器生成的"一眼可辨
_SOURCE_MARK = {
    "profile": "✅ 资料库",
    "rule": "✅ 答题口径",
    "ai": "🔶 AI 生成（务必审阅）",
    "manual": "✏️ 待你填写",
}


class ConfirmGate:
    """CLI 逐字段确认关卡。"""

    def review(self, result: FillResult, page=None) -> str:
        """展示填充结果并逐条确认。

        返回 "confirmed"（用户确认，可自行提交）或 "abandoned"（放弃本次）。
        注意：本方法不触发任何提交动作。
        """
        self._render(result)
        while True:
            try:
                choice = input("确认无误请按回车；输入 e 编辑某字段，q 放弃本次投递 > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "abandoned"
            if choice in ("", "y", "yes"):
                return "confirmed"
            if choice == "q":
                return "abandoned"
            if choice == "e":
                self._edit(result, page)
                self._render(result)
                continue
            print("  无法识别的输入：回车=确认，e=编辑，q=放弃。")

    # ------------------------------------------------------------------
    def _render(self, result: FillResult) -> None:
        print("\n" + "=" * 62)
        print(f"投递副驾 · 填充结果  [{result.job.title} @ {result.job.company}]")
        print(f"申请页：{result.job.url[:80]}    平台：{result.platform}")
        print("=" * 62)
        if result.fields:
            print("\n【已填充字段】")
            for i, f in enumerate(result.fields, 1):
                mark = _SOURCE_MARK.get(f.source, f.source)
                value = f.value if len(f.value) <= 90 else f.value[:90] + "…"
                print(f"  {i:>2}. [{mark}] {f.label}\n       → {value}")
        else:
            print("\n【已填充字段】 无（该页面字段未能自动识别）")

        if result.skipped:
            print("\n【需你手动处理】")
            for item in result.skipped:
                print(f"  · {item}")
        if result.resume_path:
            print(f"\n简历：{result.resume_path}")
        if result.screenshot_path:
            print(f"页面截图：{result.screenshot_path}")
        print()

    # ------------------------------------------------------------------
    def _edit(self, result: FillResult, page) -> None:
        """编辑某一字段的值（同时把新值写回浏览器页面）。"""
        if not result.fields:
            print("  当前没有可编辑的字段。")
            return
        raw = input("  输入要编辑的字段编号 > ").strip()
        if not raw.isdigit():
            print("  请输入数字编号。")
            return
        idx = int(raw) - 1
        if not (0 <= idx < len(result.fields)):
            print("  编号超出范围。")
            return
        field = result.fields[idx]
        new_value = input(f"  新值（原：{field.value[:60]}）> ").strip()
        if not new_value:
            print("  未输入新值，保持不变。")
            return
        field.value = new_value
        field.source = "manual"
        if page is not None:
            try:
                locator = page.locator(
                    "input:visible, select:visible, textarea:visible").nth(idx)
                locator.fill(new_value)
                logger.info(f"[副驾] 已按你的修改更新页面字段：{field.label}")
            except Exception as exc:
                logger.warning(f"[副驾] 回填页面失败（请在浏览器中手动改）：{exc}")
