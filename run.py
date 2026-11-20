#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配送运营工作台 · 启动入口

用法:
    python3 run.py              # 默认 60 秒刷新，服务 http://127.0.0.1:8765/
    python3 run.py 30           # 30 秒刷新

前提:
    1. 已安装 Chrome / Chromium
    2. 首次运行会拉起一个调试浏览器窗口，在里面登录 Aeolus（风神后台）
    3. 站点在窗口里选一次，写入 ~/.dispatch_assistant_config.json
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from modules import realtime_board  # noqa: E402

if __name__ == '__main__':
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    realtime_board.run(None, interval=interval)
