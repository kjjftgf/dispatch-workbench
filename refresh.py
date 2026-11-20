#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次刷新：跑一轮取数，生成 data/实时看板.html + data/index.html（不起常驻服务）

用法:
    python3 refresh.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from modules import cdp_core, realtime_board  # noqa: E402

out_dir = BASE / 'data'
out_path = out_dir / '实时看板.html'
ok = realtime_board.refresh_once(None, out_path, 60)
print('refresh ok =', ok, '| exists =', out_path.exists())
if ok and out_path.exists():
    (out_dir / 'index.html').write_bytes(out_path.read_bytes())
    print('已发布 index.html', (out_dir / 'index.html').stat().st_size, 'bytes')
