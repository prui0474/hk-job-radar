"""香港岗位雷达 · CLI 入口。

为什么需要 run.py 而不是直接装包后用 `hk_radar` 命令：
本项目刻意保持"零安装即用"——不写 setup.py/pyproject，直接把 src/ 加进
sys.path 即可运行，对学习者最友好（改完代码立即生效，没有安装/卸载环节）。

用法：
    python run.py run              # 启动常驻监控（电脑一直开着时用）
    python run.py once             # 抓一轮就退出并推日报（每天只开机一小会儿时用）
    python run.py report now       # 立即推送今日日报
    python run.py stats            # 查看运行状态
    python run.py login jobsdb     # 首次手动登录/过 Cloudflare
"""
import sys
from pathlib import Path

# 把 src/ 目录加入模块搜索路径（在导入 hk_job_radar 之前完成）
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from hk_job_radar.cli import main  # noqa: E402  (路径注入必须在导入前完成)


if __name__ == "__main__":
    sys.exit(main())
