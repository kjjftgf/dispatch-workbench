#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配送运营助手 · 桌面入口（源码运行 / 打包成 .app 都用这个）

流程：选浏览器 → 拉起调试浏览器 → 登录 Aeolus → 取站点 → 起看板服务（60 秒刷新）
界面：运行控制台窗口（关闭窗口 = 退出程序）
"""
import sys
import threading
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from modules import cdp_core, realtime_board          # noqa: E402
from modules.console_ui import ConsoleUI             # noqa: E402


def _pick_teams(info):
    """站点：优先用上次选的（~/.dispatch_assistant_config.json）；否则拉账号可见站点。"""
    cfg = cdp_core.load_cfg()
    tid, tname = cfg.get('team_id'), cfg.get('team_name')
    if tid:
        return [(str(tid), tname or str(tid))]
    ws, cdp = cdp_core.connect()
    teams = []
    if ws:
        try:
            teams = cdp_core.list_teams(cdp)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    teams = [(t['team_id'], t['team_name']) for t in teams]
    if len(teams) == 1:
        cfg['team_id'], cfg['team_name'] = teams[0]
        cdp_core.save_cfg(cfg)
    return teams or None


def work(ui, stop_event):
    """后台线程：登录 → 选站 → 看板循环"""
    def log(msg):
        ui.push_log(msg)
        print(msg, flush=True)

    try:
        # ⚠️ 浏览器选择必须在 UI 主线程弹窗（console_ui._startup_browser_pick 负责）；
        #    后台线程里创建 Tk 会直接 SIGSEGV（showRootWindow），2026-11-20 打包实测。
        ui.update_status(browser='● 等待选择浏览器…')
        ui._pick_ready.wait(timeout=300)
        exe, name = cdp_core.check_browser_choice()
        if exe:
            ui.update_status(browser=f'✅ {name}')
        else:
            log('⚠️ 未选择浏览器，尝试用系统默认 Chrome/Edge')
        info, _started = cdp_core.ensure_login()
        if not info:
            ui.update_status(aeolus='❌ 未登录（请在弹出的浏览器里登录后重启）')
            log('❌ 未拿到 Aeolus 登录态，程序停在控制台窗口')
            return
        ui.update_status(aeolus=f"✅ 已登录：{info.get('user_name', '')}")

        teams = _pick_teams(info)
        if not teams:
            ui.update_status(aeolus='⚠️ 拿不到站点列表（账号无可见站点）')
            log('⚠️ 账号下没有可见站点，无法出看板')
            return
        ui.update_status(browser=f"✅ ({info.get('user_name', '')})")
        log(f"📦 站点：{', '.join(n for _, n in teams)}")

        realtime_board.run(teams, interval=60,
                           status_cb=ui.update_status,
                           stop_event=stop_event)
    except Exception as e:
        log(f'❌ 运行异常：{e}')
        ui.update_status(refresh=f'❌ 异常：{e}')


def main():
    stop_event = threading.Event()
    ui = ConsoleUI()

    def _exit():
        stop_event.set()
        try:
            cdp_core.cleanup_chrome(True)
        except Exception:
            pass
        os._exit(0)

    ui.on_exit = _exit
    t = threading.Thread(target=work, args=(ui, stop_event), daemon=True)
    t.start()
    ui.run()


if __name__ == '__main__':
    main()
