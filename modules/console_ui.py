#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配送运营助手 · 运行控制台窗口（v1.18 新增）
- 显示运行情况：浏览器连接 / Aeolus 登录 / 看板服务 / 数据刷新
- 实时日志滚动（与 运行日志.txt 双写）
- 关闭窗口 / 点「⏹ 退出程序」= 结束进程（清理调试浏览器）

线程模型：tkinter mainloop 在主线程；登录/看板循环在后台线程。
跨线程通信：queue.Queue + root.after 轮询（tkinter 非线程安全，后台线程禁止直接碰控件）。
"""
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog

try:
    from modules import cdp_core
except ImportError:
    import cdp_core

_IS_MAC = sys.platform == 'darwin'
_FONT = ('PingFang SC', 13) if _IS_MAC else ('Microsoft YaHei UI', 10)
_FONT_SM = ('PingFang SC', 11) if _IS_MAC else ('Microsoft YaHei UI', 9)
_MONO = ('Menlo', 11) if _IS_MAC else ('Consolas', 10)

# 语义色（与看板基线一致）
_C_BG = '#1d1e22'
_C_PANEL = '#26272c'
_C_TEXT = '#e8e8ea'
_C_DIM = '#9aa0a6'
_C_LOG_BG = '#141518'


class ConsoleUI:
    """运行控制台窗口。线程安全：update_status()/push_log() 可从任意线程调用。"""

    def __init__(self):
        self.q = queue.Queue(maxsize=5000)
        self.on_exit = None          # 退出回调（main 里设置：清浏览器+结束进程）
        self._open_board_cb = None   # 「打开看板」回调（在调试浏览器里开标签）
        self._browser_choice_cb = None  # 「选择浏览器」回调（弹列表让用户选）
        self._restart_browser_cb = None  # 「▶️ 启动」回调（杀旧浏览器→用所选重启→Aeolus）
        self._pick_ready = threading.Event()  # 启动选择弹窗已关闭（确认或暂不，main._work 等它）

        self.root = tk.Tk()
        self.root.title('配送运营助手 · 运行控制台')
        self.root.geometry('700x560')
        self.root.minsize(580, 440)
        self.root.configure(bg=_C_BG)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self._sv = {
            'browser': tk.StringVar(value='—'),
            'cdp': tk.StringVar(value='● 检测中…'),
            'aeolus': tk.StringVar(value='等待登录…'),
            'server': tk.StringVar(value='未启动'),
            'refresh': tk.StringVar(value='—'),
            'lan': tk.StringVar(value='—'),
            'pub': tk.StringVar(value='未配置'),
        }
        self._build_status_panel()
        # ⚠️ 按钮必须先 pack（占底部）、日志区最后 expand——日志 Frame expand=True
        # 先 pack 会把后 pack 的按钮排挤塌成 1px 高不可见（2026-09-05 Windows 实测：
        # 窗口里没有按钮排，用户问「更新功能放哪里了」）
        self._build_buttons()
        self._build_log_panel()

    # ── 布局 ──
    def _make_button(self, parent, text, command, bg='#33343a', fg=_C_TEXT,
                     hover='#3f4148', padx=14, pady=6):
        """自制按钮：macOS aqua 下 tk.Button 的 bg 被忽略（显示系统白底），
        Frame+Label 的 bg 正常生效，保证深色样式跨平台一致"""
        btn = tk.Frame(parent, bg=bg, cursor='hand2')
        lbl = tk.Label(btn, text=text, bg=bg, fg=fg, font=_FONT)
        lbl.pack(padx=padx, pady=pady)

        def _on_enter(_):
            lbl.configure(bg=hover)
            btn.configure(bg=hover)

        def _on_leave(_):
            lbl.configure(bg=bg)
            btn.configure(bg=bg)

        for w in (btn, lbl):
            w.bind('<Button-1>', lambda _e, c=command: c())
            w.bind('<Enter>', _on_enter)
            w.bind('<Leave>', _on_leave)
        return btn

    def _build_status_panel(self):
        wrap = tk.Frame(self.root, bg=_C_PANEL, padx=14, pady=12)
        wrap.pack(fill='x', padx=12, pady=(12, 6))
        rows = [
            ('🌐 浏览器', 'browser', 'cdp'),
            ('🔑 Aeolus', 'aeolus', 'server'),
        ]
        for r, (label, k1, k2) in enumerate(rows):
            tk.Label(wrap, text=label, bg=_C_PANEL, fg=_C_DIM, font=_FONT).grid(
                row=r, column=0, sticky='w', pady=4)
            tk.Label(wrap, textvariable=self._sv[k1], bg=_C_PANEL,
                     fg=_C_TEXT, font=_FONT).grid(row=r, column=1, sticky='w', padx=(10, 24))
            tk.Label(wrap, textvariable=self._sv[k2], bg=_C_PANEL,
                     fg=_C_DIM, font=_FONT_SM).grid(row=r, column=2, sticky='w')
        # 浏览器行右侧：「✏️ 选择浏览器」+「▶️ 启动」（v1.26：切换浏览器后点启动立即生效，
        # 不必重启软件；启动=杀旧调试浏览器→用当前所选浏览器重新拉起→重进 Aeolus）
        brow_btns = tk.Frame(wrap, bg=_C_PANEL)
        brow_btns.grid(row=0, column=3, sticky='e', padx=(0, 4))
        self._make_button(brow_btns, '▶️ 启动', self._restart_browser,
                          padx=10, pady=2).pack(side='left')
        self._make_button(brow_btns, '✏️ 选择浏览器', self._choose_browser,
                          padx=10, pady=2).pack(side='left', padx=(6, 0))
        tk.Label(wrap, text='📤 数据刷新', bg=_C_PANEL, fg=_C_DIM, font=_FONT).grid(
            row=2, column=0, sticky='w', pady=4)
        tk.Label(wrap, textvariable=self._sv['refresh'], bg=_C_PANEL,
                 fg=_C_TEXT, font=_FONT).grid(row=2, column=1, columnspan=2, sticky='w', padx=(10, 24))
        # 访问方式：局域网（自动检测+复制）/ 公网（手动填写+复制）
        tk.Label(wrap, text='📶 局域网', bg=_C_PANEL, fg=_C_DIM, font=_FONT).grid(
            row=3, column=0, sticky='w', pady=4)
        tk.Label(wrap, textvariable=self._sv['lan'], bg=_C_PANEL,
                 fg=_C_TEXT, font=_FONT).grid(row=3, column=1, columnspan=2, sticky='w', padx=(10, 24))
        self._make_button(wrap, '📋 复制', self._copy_lan,
                          padx=8, pady=1).grid(row=3, column=3, sticky='e', padx=(0, 4))
        tk.Label(wrap, text='🌍 公网', bg=_C_PANEL, fg=_C_DIM, font=_FONT).grid(
            row=4, column=0, sticky='w', pady=4)
        tk.Label(wrap, textvariable=self._sv['pub'], bg=_C_PANEL,
                 fg=_C_TEXT, font=_FONT).grid(row=4, column=1, columnspan=2, sticky='w', padx=(10, 24))
        self._make_button(wrap, '✏️ 填写', self._edit_pub,
                          padx=8, pady=1).grid(row=4, column=2, sticky='e')
        self._make_button(wrap, '📋 复制', self._copy_pub,
                          padx=8, pady=1).grid(row=4, column=3, sticky='e', padx=(0, 4))
        wrap.grid_columnconfigure(1, weight=1)

    def _build_log_panel(self):
        tk.Label(self.root, text='运行日志（与 运行日志.txt 同步）', bg=_C_BG,
                 fg=_C_DIM, font=_FONT_SM, anchor='w').pack(fill='x', padx=14, pady=(6, 2))
        frame = tk.Frame(self.root, bg=_C_BG)
        frame.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        self.txt = tk.Text(frame, bg=_C_LOG_BG, fg=_C_TEXT, font=_MONO,
                           wrap='word', relief='flat', borderwidth=0,
                           insertbackground=_C_TEXT, selectbackground='#33415c',
                           padx=10, pady=8)
        sb = tk.Scrollbar(frame, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.txt.pack(side='left', fill='both', expand=True)
        self.txt.configure(state='disabled')

    def _build_buttons(self):
        bar = tk.Frame(self.root, bg=_C_BG)
        bar.pack(side='bottom', fill='x', padx=12, pady=(0, 12))
        self._make_button(bar, '🖥 打开看板', self._open_board).pack(side='left')
        self._make_button(bar, '🔑 设置密码', self._set_board_pass).pack(side='left', padx=(8, 0))
        self._make_button(bar, '🔄 检测更新', self._check_update).pack(side='left', padx=(8, 0))
        self._make_button(bar, '⏹ 退出程序', self._on_close,
                          bg='#3a1f22', fg='#f2a5a0', hover='#55282c').pack(side='right')

    # ── 看板功能（v1.20：设置密码/检测更新从看板页面挪进运行控制台） ──
    def _set_board_pass(self):
        """「设置密码」：写 data/board_pass.txt（手机/局域网访问看板用，用户名固定 board）"""
        val = simpledialog.askstring(
            '设置看板访问密码', '新密码（4~20位）：\n\n手机/其他设备访问看板时使用。\n本机自己看不需密码。',
            show='*', parent=self.root)
        if val is None:
            return
        val = val.strip()
        if not (4 <= len(val) <= 20):
            self.push_log('❌ 密码需 4~20 位')
            return
        try:
            p = cdp_core.data_dir() / 'board_pass.txt'
            p.write_text(val, encoding='utf-8')
            self.push_log(f'✅ 看板访问密码已更新（board / {val}）')
        except Exception as e:
            self.push_log(f'❌ 写密码失败：{e}')

    def _check_update(self):
        """「检测更新」：查 Gitee 最新版本；打包版发现新版直接弹窗询问一键升级
        （v1.21：不再只给下载地址）"""
        import threading
        self.push_log('🔄 正在检查更新…')

        def work():
            try:
                try:
                    from modules import update_check, realtime_board
                except ImportError:
                    import update_check
                    import realtime_board
                ver = getattr(realtime_board, 'APP_VERSION', '?')
                r = update_check.check_update(ver, timeout=10)
                if not r:
                    self.push_log(f'✅ 当前已是最新版本（{ver}）')
                    return
                tag, url = r
                if getattr(sys, 'frozen', False):
                    # 打包版：弹窗确认 → 一键下载升级
                    def ask():
                        yes = messagebox.askyesno(
                            '发现新版本',
                            f'发现新版本 {tag}（当前 {ver}）\n\n'
                            f'是否现在下载并升级？\n'
                            f'（自动下载约 {tag} 安装包 → 替换 → 重启，'
                            f'全程约 10~30 秒，站点数据不会丢）')
                        if yes:
                            threading.Thread(target=self._do_upgrade,
                                             args=(ver,), daemon=True).start()
                    self.root.after(0, ask)
                else:
                    self.push_log(f'📥 发现新版本 {tag}（当前 {ver}）')
                    self.push_log(f'   下载地址: {url}')
            except Exception as e:
                self.push_log(f'❌ 检查更新失败：{e}')

        threading.Thread(target=work, daemon=True).start()

    def _do_upgrade(self, ver):
        """一键升级（后台线程）：下载→校验→启动升级器→本进程退出。
        升级器等进程退出后替换文件并重启新版本。"""
        import os
        try:
            from modules import update_check
        except ImportError:
            import update_check
        try:
            res = update_check.try_self_upgrade(ver, log_fn=self.push_log)
        except Exception as e:
            self.push_log(f'❌ 升级失败：{e}')
            return
        if isinstance(res, tuple) and res[0] == 'fail':
            self.push_log(f'❌ {res[1]}')
            return
        if isinstance(res, tuple) and res[0] == 'done':
            self.push_log('⏳ 正在退出旧程序，升级完成后会自动打开新版本…')
            self.root.after(2000, lambda: os._exit(0))

    # ── 线程安全入口（后台线程调用） ──
    def update_status(self, **kw):
        """更新状态。键：browser/cdp/aeolus/server/refresh"""
        try:
            self.q.put_nowait(('status', kw))
        except queue.Full:
            pass

    def push_log(self, line):
        try:
            self.q.put_nowait(('log', line))
        except queue.Full:
            pass

    # ── UI 线程消费 ──
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'status':
                    for k in ('browser', 'cdp', 'aeolus', 'server', 'refresh', 'lan', 'pub'):
                        if k in payload:
                            self._sv[k].set(str(payload[k]))
                else:
                    self._append_log(payload)
        except queue.Empty:
            pass
        self._refresh_cdp()
        self.root.after(300, self._poll)

    def _append_log(self, line):
        line = str(line).rstrip('\n')
        if not line:
            return
        self.txt.configure(state='normal')
        self.txt.insert('end', line + '\n')
        # 只保留最近 1500 行，防窗口卡顿
        if float(self.txt.index('end-1c').split('.')[0]) > 1500:
            self.txt.delete('1.0', '100.0')
        self.txt.see('end')
        self.txt.configure(state='disabled')

    def _refresh_cdp(self):
        """每 300ms 探测一次调试浏览器 CDP 存活（本地 localhost，开销极小）"""
        try:
            alive = cdp_core._cdp_alive(timeout=1)
            self._sv['cdp'].set(
                f'● 已连接（端口 {cdp_core.CDP_PORT}）' if alive
                else '○ 未连接（自动重试中）')
        except Exception:
            pass

    # ── 按钮 / 退出 ──
    def set_open_board_cb(self, cb):
        self._open_board_cb = cb

    def set_browser_choice_cb(self, cb):
        self._browser_choice_cb = cb

    def set_restart_browser_cb(self, cb):
        """「▶️ 启动」按钮回调（main 注入：杀旧浏览器→用所选浏览器重启→Aeolus）"""
        self._restart_browser_cb = cb

    def _restart_browser(self):
        """「▶️ 启动」按钮：按当前选择的浏览器重新启动载体"""
        if self._restart_browser_cb:
            try:
                self._restart_browser_cb()
                return
            except Exception:
                pass
        self.push_log('⚠️ 请先选择浏览器后再点启动')

    def _choose_browser(self):
        """「选择浏览器」按钮：弹窗重选（预选当前用的），确认后只保存配置。
        ⚠️ 不自动拉起浏览器（v1.29 用户拍板：「▶️ 启动」的作用=我手动拉起浏览器，
        不是软件强制拉起）——想现在拉就点「▶️ 启动」，或下次启动软件自动用。"""
        picked = self._pick_browser_dialog(pre_select=True)
        if not picked:
            return  # 取消/无浏览器
        p, n = picked
        self._sv['browser'].set(n)
        self.push_log(f'🌐 已切换浏览器：{n}')
        self.push_log('   现在想拉起来就点「▶️ 启动」（手动拉起）；不点就下次启动生效')

    # ── 局域网/公网访问（显示+复制，不做穿透，v1.18）──
    def _copy(self, url):
        if not url or url in ('—', '未配置'):
            self._flash('没有可复制的地址')
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.root.update()
        self._flash('✅ 已复制：' + url)

    def _copy_lan(self):
        self._copy(self._sv['lan'].get())

    def _copy_pub(self):
        self._copy(self._sv['pub'].get())

    def _edit_pub(self):
        """「填写」公网地址：手动填（如有 frp/其他穿透，填自己的地址），存配置文件"""
        cur = ''
        try:
            cfg = cdp_core.load_cfg()
            cur = cfg.get('public_url', '') or ''
        except Exception:
            pass
        val = simpledialog.askstring(
            '公网访问地址', '填写公网看板地址（如 http://39.106.200.1:24490/）\n'
            '没有公网穿透就留空 = 不显示',
            parent=self.root, initialvalue=cur)
        if val is None:
            return  # 取消
        val = val.strip()
        try:
            cfg = cdp_core.load_cfg()
            if val:
                cfg['public_url'] = val
            else:
                cfg.pop('public_url', None)
            cdp_core.save_cfg(cfg)
        except Exception:
            pass
        self._sv['pub'].set(val if val else '未配置')
        self._flash('✅ 已保存' if val else '已清除公网地址')

    def _flash(self, msg):
        """临时提示（显示在「数据刷新」行，下次刷新状态自动覆盖）"""
        self._sv['refresh'].set(msg)

    def _open_board(self):
        if self._open_board_cb:
            try:
                self._open_board_cb()
                return
            except Exception:
                pass
        import webbrowser
        webbrowser.open('http://127.0.0.1:8765/')

    def _on_close(self):
        """关闭窗口 / 点退出：先调 main 的退出逻辑（清浏览器+结束进程）"""
        if self.on_exit:
            try:
                self.on_exit()
            except Exception:
                pass
        try:
            self.root.withdraw()
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.after(300, self._poll)
        self.root.after(150, self._startup_browser_pick)
        self.root.mainloop()

    # ── 浏览器选择对话框（Toplevel 挂主窗，v1.25：选择器统一走这里，
    #    不再新建第二个 Tk——cdp_core._tk_list_choose 的独立 Tk 在主窗已存在时
    #    跨解释器渲染列表会失败，2026-09-05 实测只剩标题空列表）──
    def _pick_browser_dialog(self, pre_select=True):
        """UI 线程：模态浏览器选择窗。返回 (路径, 显示名)；取消/无浏览器返回 None。
        确认会写 cfg['browser_path']。"""
        try:
            from modules import cdp_core as _cdp
        except ImportError:
            import cdp_core as _cdp
        try:
            browsers = _cdp.list_browsers() or []
        except Exception:
            browsers = []
        win = tk.Toplevel(self.root)
        win.title('选择浏览器')
        win.configure(bg=_C_BG)
        win.geometry('560x400')
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text='请选择本软件使用的浏览器（Aeolus 将在这个浏览器里打开）：',
                 bg=_C_BG, fg=_C_TEXT, font=_FONT, anchor='w').pack(fill='x', padx=16, pady=(16, 2))
        tk.Label(win, text='软件用独立配置启动它，不会影响你平时用的浏览器',
                 bg=_C_BG, fg=_C_DIM, font=_FONT_SM, anchor='w').pack(fill='x', padx=16)
        lb = tk.Listbox(win, bg=_C_PANEL, fg=_C_TEXT, font=_FONT, relief='flat',
                        selectbackground='#2f6fdd', selectforeground='#ffffff', height=6,
                        activestyle='none', highlightthickness=0)
        lb.pack(fill='both', expand=True, padx=16, pady=(10, 6))
        if not browsers:
            lb.insert('end', '（未检测到 Chrome / Edge —— 请先安装浏览器后重开本软件）')
            lb.configure(state='disabled')
        else:
            for i, (nm, path) in enumerate(browsers):
                try:
                    shown = nm if len(str(nm)) < 40 else str(nm)[:37] + '…'
                except Exception:
                    shown = str(nm)
                lb.insert('end', shown)
            if pre_select:
                remember = None
                try:
                    remember = (_cdp.load_cfg() or {}).get('browser_path')
                except Exception:
                    pass
                for i, (nm, path) in enumerate(browsers):
                    try:
                        if remember and _cdp._normalize_browser_path(str(path)) == \
                                _cdp._normalize_browser_path(str(remember)):
                            lb.selection_set(i)
                            break
                    except Exception:
                        pass
            try:
                if not lb.curselection():
                    lb.selection_set(0)
            except Exception:
                pass
        result = {}

        def _confirm():
            try:
                sel = lb.curselection()
                if not browsers or not sel:
                    win.destroy()
                    return
                # ⚠️ browsers 每项 = (显示名, 路径)；解包顺序反了会把显示名当路径存进 cfg
                # （v1.30 修复：曾存成 "Microsoft Edge" 裸名 → find_chrome 找不到 → 永远未连接）
                nm, path = browsers[sel[0]]
                cfg = _cdp.load_cfg()
                cfg['browser_path'] = path
                _cdp.save_cfg(cfg)
                result['picked'] = (path, nm)
            except Exception:
                pass
            win.destroy()

        bar = tk.Frame(win, bg=_C_BG)
        bar.pack(fill='x', padx=16, pady=(4, 16))
        self._make_button(bar, '✅ 确认使用', _confirm, padx=24, pady=6).pack(side='right')
        self._make_button(bar, '✖ 暂不选择', win.destroy, bg='#3a1f22', fg='#f2a5a0',
                          hover='#55282c', padx=16, pady=6).pack(side='left')
        win.protocol('WM_DELETE_WINDOW', win.destroy)
        win.grab_set()
        try:
            win.focus_force()
        except Exception:
            pass
        self.root.wait_window(win)
        return result.get('picked')

    # ── 启动引导：选择浏览器并确认（v1.23+，用户拍板：打开先选浏览器→确认→再拉登录页，
    #    不要打开就自动连/反复探测；v1.24：只在第一次/没记住时弹，记住的直接进）──
    def _startup_browser_pick(self):
        """UI 线程：启动引导。
        已记住有效浏览器 → 不弹窗直接放行；
        没记住 → 弹模态选择窗（预选第一个），确认写 cfg 后后台线程自动拉浏览器开 Aeolus。"""
        try:
            from modules import cdp_core as _cdp
        except ImportError:
            import cdp_core as _cdp
        picked = None
        try:
            exe0, name0 = _cdp.check_browser_choice()
            if exe0:
                self._sv['browser'].set(name0 or '已记住')
            else:
                picked = self._pick_browser_dialog(pre_select=True)
        except Exception:
            picked = self._pick_browser_dialog(pre_select=True)
        if picked:
            p, n = picked
            self._sv['browser'].set(n)
            self.push_log(f'🌐 浏览器：{n}')
        try:
            self._pick_ready.set()  # 弹窗已关闭：main._work 可继续（确认→自动拉起浏览器；暂不→按旧配置或等按钮）
        except Exception:
            pass
