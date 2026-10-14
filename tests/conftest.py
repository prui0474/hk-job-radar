"""pytest 公共配置：把 src/ 加入模块搜索路径（本项目刻意"零安装即用"，无 pyproject）。

这样 `pytest tests/` 与 `python tests/test_xxx.py` 两种方式都能 import 到
hk_job_radar 包，无需 pip install -e .。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
