#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDP 核心模块：跨平台 Chrome 查找/启动、Aeolus 登录引导、账号站点识别
- macOS: /Applications/Google Chrome.app
- Windows: Chrome 多路径候选，找不到用系统自带 Edge（同为 Chromium，支持调试端口）
"""
import json, os, socket, subprocess, sys, time, urllib.request
from pathlib import Path


def _no_window_kw():
    """Windows windowed 打包下隐藏子进程控制台窗口（v1.32：修复运行中频繁闪烁终端，
    根因=--windowed 程序每调 subprocess/os.system 都会闪一个 cmd 窗，看板每 60s
    curl 下载即闪一次）。凡命令行类 subprocess 调用（下载/杀进程/查防火墙等）必须带
    **此 kwargs；GUI 弹窗类（PowerShell Forms 浏览器选择/图标提取走 stdout 无妨）
    除外。非 Windows 返回空 dict，行为不变。"""
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    return {}


def _pick_free_port(base, tries=10):
    """从 base 起找第一个空闲端口（打包版与本机脚本共存时隔离用，2026-08-30）"""
    for p in range(base, base + tries):
        try:
            s = socket.socket()
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except OSError:
            continue
    return base


# 打包版（PyInstaller exe/app）＝独立实例：自动找空闲调试端口 + 独立浏览器配置目录，
# 与本机 Hermes 看板守护（9223 / ~/.chrome-debug）完全隔离、互不影响
# 源码版（launchd/命令行跑）同样独立实例（2026-09-03 实测互顶修复）：本机守护脚本固定占
# 9223/~/.chrome-debug，源码版再占 9223 会导致两边 ensure 时互相 cleanup+pkill 对方 Chrome，
# 每 60s 互杀重启，查询间歇报"浏览器未连接"、看板数据停更
def _port_in_use_by_dir(cdp_dir):
    """若该 user-data-dir 的调试 Chrome 已在运行，返回其调试端口。
    修复 2026-11-05 端口漂移：进程重启时若原端口仍被自己的 Chrome 占着，
    _pick_free_port 会另挑新端口 → 连不上已登录的旧实例，看板/播报全部判定「登录失效」。
    复用同一 user-data-dir 的既有实例（登录态也一起复用）。"""
    try:
        out = os.popen(f"ps -axo args | grep -F -- '--user-data-dir={cdp_dir}' | grep -v grep").read()
    except Exception:
        return None
    for line in out.splitlines():
        for tok in line.split():
            if tok.startswith('--remote-debugging-port='):
                try:
                    return int(tok.split('=', 1)[1])
                except Exception:
                    pass
    return None


if getattr(sys, 'frozen', False):
    # ⚠️ 2026-10-29 修：起点改 9224，**9223 永远留给本机守护脚本**（aeolus_guard/播报/safetycy）。
    # 原来从 9223 起找空闲口，只要 9223 空着看板就占住它 → 播报的 aeolus_guard 连到看板实例上，
    # 登录探测在 ok/rc=2 之间抖动 → wrapper `|| exit 2` 直接放弃 → 时段预警/达标图/申诉检测漏播。
    CDP_DIR = str(Path.home() / '.dispatch_assistant_app_chrome')
    CDP_PORT = _port_in_use_by_dir(CDP_DIR) or _pick_free_port(9224)
else:
    CDP_DIR = str(Path.home() / '.dispatch_assistant_chrome')
    CDP_PORT = _port_in_use_by_dir(CDP_DIR) or _pick_free_port(9224)
CDP_URL = f'http://127.0.0.1:{CDP_PORT}'
CFG_PATH = Path.home() / '.dispatch_assistant_config.json'
AEOLUS_HOME = 'https://aeolus.ele.me/?targetId=51388'

INIT_URL = 'https://httpizza.ele.me/lpd.meepo.session/aeolus/init'
TEAMS_API = 'xtop.lpd.intelligentConfig.getTeamsAll'


# ── 配置读写（站点选择 + 登录标记） ──
def load_cfg():
    try:
        if CFG_PATH.exists():
            return json.loads(CFG_PATH.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}

def save_cfg(cfg):
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')


# ── 跨平台找 Chrome/Edge ──
def _normalize_browser_path(p):
    """把用户选/填的浏览器路径规整成可执行文件：
    - 已指向可执行文件（含 Contents/MacOS/ 或 chrome.exe 等）→ 原样
    - 指向 .app bundle → 补 Contents/MacOS/<可执行文件>"""
    try:
        p = str(p or '').strip().strip('"')
        if not p:
            return None
        if sys.platform == 'darwin':
            if p.endswith('.app') and os.path.isdir(p):
                exe_dir = os.path.join(p, 'Contents', 'MacOS')
                if os.path.isdir(exe_dir):
                    for f in sorted(os.listdir(exe_dir)):
                        fp = os.path.join(exe_dir, f)
                        if os.path.isfile(fp) and os.access(fp, os.X_OK):
                            return fp
                return None
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        else:
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    return None


def list_browsers():
    """自动扫描本机已安装的 Chromium 系浏览器（Mac=常见 .app，Win=常见安装路径）。
    返回 [(显示名, 可执行文件路径), ...]，按推荐顺序；一个都没检测到返回 []"""
    found = []
    if sys.platform == 'darwin':
        roots = ['/Applications', str(Path.home() / 'Applications')]
        # (显示名, bundle 名) —— Chromium 系都支持 --remote-debugging-port
        cands = [('Google Chrome', 'Google Chrome.app'),
                 ('Microsoft Edge', 'Microsoft Edge.app'),
                 ('Chromium', 'Chromium.app'),
                 ('Brave', 'Brave Browser.app'),
                 ('Arc', 'Arc.app'),
                 ('Vivaldi', 'Vivaldi.app'),
                 ('Opera', 'Opera.app')]
        for name, bundle in cands:
            for root in roots:
                exe = _normalize_browser_path(os.path.join(root, bundle))
                if exe:
                    found.append((name, exe))
                    break
    else:
        base = Path(os.environ.get('LOCALAPPDATA', 'C:/Users/Default/AppData/Local'))
        pf = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        pfx = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
        cands = [('Google Chrome', [pf / 'Google/Chrome/Application/chrome.exe',
                                    pfx / 'Google/Chrome/Application/chrome.exe',
                                    base / 'Google/Chrome/Application/chrome.exe']),
                 ('Microsoft Edge', [pf / 'Microsoft/Edge/Application/msedge.exe',
                                     pfx / 'Microsoft/Edge/Application/msedge.exe']),
                 ('Chromium', [pf / 'Chromium/Application/chrome.exe',
                               base / 'Chromium/Application/chrome.exe']),
                 ('Brave', [pf / 'BraveSoftware/Brave-Browser/Application/brave.exe',
                            base / 'BraveSoftware/Brave-Browser/Application/brave.exe'])]
        for name, paths in cands:
            for p in paths:
                if p.exists():
                    found.append((name, str(p)))
                    break
    return found


def _browser_icon(name, exe):
    """提取浏览器图标 PNG 字节（供 _tk_list_choose 用 PhotoImage(master=弹窗root) 加载）。
    Mac=从 .app/Contents/Resources/*.icns 读最大尺寸转 PNG；Win=从 exe 提取 ico 转 PNG。
    ⚠️ 只返回 bytes 不建 PhotoImage：tk.PhotoImage 默认绑 tkinter._default_root（=主控制台窗口的
    Tk 解释器），在第二个 Tk 弹窗里用会跨解释器报 image doesn't exist → 弹窗列表渲染中断（2026-09-05 实测图2空列表）。
    返回 PNG bytes 或 None（提取失败时界面回退画圆点）"""
    try:
        import tkinter as tk
        import io as _io
        try:
            from PIL import Image
        except Exception:
            return None
        png_bytes = None
        if sys.platform == 'darwin':
            # 从 exe 路径反推 .app bundle，找 Resources 下最大的 icns
            app_dir = Path(exe)
            # exe 形如 .../Google Chrome.app/Contents/MacOS/Google Chrome
            for _ in range(4):
                if app_dir.name.endswith('.app'):
                    break
                app_dir = app_dir.parent
            if app_dir.name.endswith('.app'):
                res = app_dir / 'Contents' / 'Resources'
                icns_cands = sorted([p for p in res.glob('*.icns') if p.exists()],
                                    key=lambda p: p.stat().st_size, reverse=True) if res.is_dir() else []
                if icns_cands:
                    try:
                        with Image.open(icns_cands[0]) as img:
                            sizes = img.info.get('sizes', [])
                        if sizes:
                            # icns 多尺寸：选 64px 附近（含 32/48/64/128）的最大可用
                            best = max((s for s in sizes if s[0] <= 64), key=lambda s: s[0]) \
                                if any(s[0] <= 64 for s in sizes) else min(sizes, key=lambda s: s[0])
                            with Image.open(icns_cands[0]) as img:
                                img = img.resize((32, 32), Image.LANCZOS)
                        else:
                            with Image.open(icns_cands[0]) as img:
                                img = img.resize((32, 32), Image.LANCZOS)
                    except Exception:
                        try:
                            img = Image.open(icns_cands[0])
                        except Exception:
                            img = None
                    if img is not None:
                        buf = _io.BytesIO()
                        img.convert('RGBA').save(buf, format='PNG')
                        png_bytes = buf.getvalue()
        elif sys.platform == 'win32':
            try:
                import ctypes
                from ctypes import wintypes
                # SHDefExtractIcon 或 ExtractIconEx 拿 32x32 图标
                _hmod = ctypes.windll.shell32
                large = (ctypes.c_void_p * 1)()
                small = (ctypes.c_void_p * 1)()
                n = _hmod.ExtractIconExW(exe, 0, large, small, 1)
                if n and large[0]:
                    hicon = large[0]
                    # HICON → PNG 用 PIL 无法直接读 HICON；转 ICO 内存
                    import tempfile
                    tmp = tempfile.NamedTemporaryFile(suffix='.ico', delete=False)
                    tmp.close()
                    # CopyImage 到 32px
                    img32 = ctypes.windll.user32.CopyImage(hicon, 1, 32, 32, 0)  # IMAGE_ICON=1
                    if img32:
                        hicon = img32
                    # 用 GDI+ 保存太复杂，改用 powershell 兜底（见下）
                ctypes.windll.user32.DestroyIcon(hicon)
            except Exception:
                pass
            # Windows 兜底：powershell System.Drawing 提取 exe 图标
            try:
                ps = ("Add-Type -AssemblyName System.Drawing;"
                      "$i=[System.Drawing.Icon]::ExtractAssociatedIcon('" + exe + "');"
                      "$b=$i.ToBitmap();"
                      "$ms=New-Object System.IO.MemoryStream;"
                      "$b.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);"
                      "[Convert]::ToBase64String($ms.ToArray())")
                out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                     capture_output=True, text=True, timeout=60, **_no_window_kw())
                if out.returncode == 0 and out.stdout.strip():
                    import base64
                    png_bytes = base64.b64decode(out.stdout.strip())
            except Exception:
                pass
        if png_bytes:
            return png_bytes
    except Exception:
        pass
    return None


def _tk_list_choose(title, prompt, names, exes=None):
    """tkinter 浏览器选择面板（打包版自带，主选方案）。
    形态：macOS 原生质感卡片——品牌色圆点图标 + 系统蓝选中态 + 蓝色确定按钮 + 紧凑布局。
    返回选中的项名；取消/失败返回 None"""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception:
        return None
    try:
        # 浏览器品牌色（选中态/图标用）
        BRAND = {'Google Chrome': '#4285F4', 'Microsoft Edge': '#00A4EF',
                 'Chromium': '#4285F4', 'Brave': '#FB542B', 'Arc': '#6E56CF',
                 'Vivaldi': '#EF3939', 'Opera': '#FF1B2D'}
        BRAND_ALT = {'Google Chrome': ('#EA4335', '#FBBC05', '#34A853'),  # Chrome 四色
                     'Microsoft Edge': ('#00A4EF', '#35C1F1', '#0078D7')}
        # 兜底显示名（Windows 常见路径下 list_browsers 用的名字）
        def _brand_color(name):
            return BRAND.get(name, '#5B8DEF')

        root = tk.Tk()
        root.title(title)
        root.attributes('-topmost', True)
        root.configure(bg='#F5F6F8')
        try:
            f_title = tkfont.Font(family='PingFang SC', size=14, weight='bold')
            f_sub = tkfont.Font(family='PingFang SC', size=11)
            f_item = tkfont.Font(family='PingFang SC', size=13)
            f_btn = tkfont.Font(family='PingFang SC', size=12)
        except Exception:
            f_title = f_sub = f_item = f_btn = None

        w, h = 400, 336
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f'{w}x{h}+{(sw-w)//2}+{(sh-h)//3}')
        root.resizable(False, False)

        # ── 顶部：标题 + 副标题 ──
        head = tk.Frame(root, bg='#F5F6F8')
        head.pack(fill='x', padx=20, pady=(18, 10))
        tk.Label(head, text='选择浏览器', bg='#F5F6F8', fg='#1D1D1F',
                 font=f_title, anchor='w').pack(anchor='w')
        tk.Label(head, text=f'检测到 {len(names)} 个兼容浏览器，请选择运行载体：',
                 bg='#F5F6F8', fg='#6E6E73', font=f_sub, anchor='w').pack(anchor='w', pady=(3, 0))

        # ── 浏览器列表 ──
        listwrap = tk.Frame(root, bg='white', highlightbackground='#E3E4E8',
                            highlightthickness=1, bd=0)
        listwrap.pack(fill='x', padx=20, pady=(14, 0))
        selected = [0]  # 当前选中下标

        def _draw_item(idx, name, is_sel):
            row = tk.Frame(listwrap, bg=('#E8F0FE' if is_sel else 'white'), bd=0)
            row.pack(fill='x', padx=0, pady=0)
            # 真实浏览器图标（从 .app/.exe 提取，失败才回退品牌色圆点）
            img = _icons[idx] if idx < len(_icons) else None
            if img:
                lbl = tk.Label(row, image=img, bg=('#E8F0FE' if is_sel else 'white'), bd=0)
                lbl.image = img  # 防 GC
                lbl.pack(side='left', padx=(12, 4), pady=6)
                dot = None
            else:
                dot = tk.Canvas(row, width=34, height=34, bg=('#E8F0FE' if is_sel else 'white'),
                                highlightthickness=0)
                dot.pack(side='left', padx=(14, 0), pady=7)
                cx, cy, r = 17, 17, 8
                if name in BRAND_ALT:
                    c1, c2, c3 = BRAND_ALT[name]
                    dot.create_oval(cx-r, cy-r, cx, cy, fill=c1, outline='')
                    dot.create_oval(cx, cy-r, cx+r, cy, fill=c2, outline='')
                    dot.create_oval(cx-r, cy, cx, cy+r, fill=c2, outline='')
                    dot.create_oval(cx, cy, cx+r, cy+r, fill=c3, outline='')
                    dot.create_oval(cx-r//2, cy-r//2, cx+r//2, cy+r//2, fill='white', outline='')
                else:
                    dot.create_oval(cx-r, cy-r, cx+r, cy+r, fill=_brand_color(name), outline='')
            # 名称
            tk.Label(row, text=name, bg=('#E8F0FE' if is_sel else 'white'),
                     fg='#1D1D1F', font=f_item, anchor='w').pack(side='left', padx=(8, 0))
            # 选中勾
            if is_sel:
                chk = tk.Canvas(row, width=30, height=34, bg='#E8F0FE', highlightthickness=0)
                chk.pack(side='right', padx=(0, 14), pady=7)
                chk.create_line(5, 17, 12, 24, fill='#0A5BD3', width=2.4, capstyle='round', joinstyle='round')
                chk.create_line(12, 24, 26, 9, fill='#0A5BD3', width=2.4, capstyle='round', joinstyle='round')
            # 点击选中
            bind_wids = [row] + ([dot] if dot else [])
            for wid in bind_wids:
                wid.bind('<Button-1>', lambda e, i=idx: _select(i))
            row.bind('<Enter>', lambda e, i=idx: _hover(i, True))
            row.bind('<Leave>', lambda e, i=idx: _hover(i, False))
            return row

        # 预提取各浏览器真实图标（返回 PNG bytes；PhotoImage 必须绑定本弹窗 root 防跨解释器）
        _icons = []
        for i, n in enumerate(names):
            exe = exes[i] if exes and i < len(exes) else ''
            b = _browser_icon(n, exe)
            if b:
                try:
                    _icons.append(tk.PhotoImage(master=root, data=b))
                except Exception:
                    _icons.append(None)
            else:
                _icons.append(None)

        rows = []
        for i, n in enumerate(names):
            rows.append(_draw_item(i, n, i == 0))

        def _refresh():
            # 重建列表（简单可靠：销毁重画）
            for r in rows:
                r.destroy()
            rows.clear()
            for i, n in enumerate(names):
                rows.append(_draw_item(i, n, i == selected[0]))

        def _hover(i, on):
            if i != selected[0]:
                rows[i].configure(bg='#F2F4F7' if on else 'white')
                for c in rows[i].winfo_children():
                    try:
                        if isinstance(c, tk.Canvas):
                            c.configure(bg='#F2F4F7' if on else 'white')
                        else:
                            c.configure(bg='#F2F4F7' if on else 'white')
                    except Exception:
                        pass

        def _select(i):
            selected[0] = i
            _refresh()

        def _move(d):
            i = selected[0] + d
            if 0 <= i < len(names):
                selected[0] = i
                _refresh()

        # ── 底部按钮 ──
        btnf = tk.Frame(root, bg='#F5F6F8')
        btnf.pack(fill='x', padx=20, pady=(16, 16))
        result = [None]

        def _confirm(_=None):
            result[0] = names[selected[0]]
            _close()

        def _cancel(_=None):
            _close()

        def _close():
            # ⚠️ tkinter 9.0 实测：只 destroy() 时窗口残留屏幕（Tcl 线程还活着）
            #    → withdraw() 先隐藏（用户立刻看到弹窗消失）+ quit() 停 mainloop + destroy() 收尾
            try:
                root.withdraw()
            except Exception:
                pass
            try:
                root.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

        # 取消（灰）
        cxl = tk.Button(btnf, text='取消', command=_cancel, font=f_btn,
                        bg='#FFFFFF', fg='#1D1D1F', activebackground='#E8EAED',
                        relief='flat', bd=1, highlightbackground='#D0D2D6',
                        highlightthickness=1, padx=18, pady=5, cursor='pointinghand')
        cxl.pack(side='right', padx=(10, 0))
        # 确定（蓝）
        okb = tk.Button(btnf, text='确定', command=_confirm, font=f_btn,
                        bg='#0A5BD3', fg='white', activebackground='#0A4FB8',
                        activeforeground='white', relief='flat', bd=0,
                        padx=22, pady=5, cursor='pointinghand')
        okb.pack(side='right')
        # 提示语（左下）
        tk.Label(btnf, text='↑↓ 选择 · ⏎ 确定 · Esc 取消', bg='#F5F6F8', fg='#86868B',
                 font=f_sub, anchor='w').pack(side='left')

        root.bind('<Return>', _confirm)
        root.bind('<Escape>', _cancel)
        root.bind('<Up>', lambda e: _move(-1))
        root.bind('<Down>', lambda e: _move(1))
        root.protocol('WM_DELETE_WINDOW', _cancel)
        root.lift()
        root.after(150, root.lift)
        root.mainloop()
        return result[0]
    except Exception:
        # 异常时销毁已创建的弹窗，避免空窗口残留屏幕（2026-09-05：图2 只剩标题无列表即此类）
        try:
            root.destroy()
        except NameError:
            pass
        except Exception:
            pass
        return None


def choose_browser_prompt(browsers):
    """弹系统原生列表选择框（Mac=choose from list / Win=列表弹窗）让用户选载体浏览器。
    browsers=[(显示名, 路径), ...]；返回选中项的 (显示名, 路径)；取消返回 None"""
    if not browsers:
        return None
    names = [n for n, _ in browsers]
    exes = [p for _, p in browsers]
    # 首选 tkinter 列表弹窗（Python 自带、随 app 打包，不依赖 osascript 权限/非GUI会话限制）
    picked_name = _tk_list_choose('配送运营助手 - 选择浏览器',
                                  f'检测到您电脑上有 {len(names)} 个浏览器，请选择用哪个作为载体：',
                                  names, exes)
    if picked_name:
        for n, p in browsers:
            if n == picked_name:
                return (n, p)
        return None
    # tkinter 不可用（极端环境）→ 回退 osascript/powershell
    try:
        if sys.platform == 'darwin':
            # osascript 原生列表选择框（choose from list 自带取消按钮，返回 false）
            items = ', '.join('"' + n.replace('"', '\\"') + '"' for n in names)
            script = (f'set chosen to choose from list {{{items}}} '
                      f'with title "配送运营助手" '
                      f'with prompt "检测到您电脑上有 {len(names)} 个浏览器，请选择用哪个作为载体：" '
                      f'default items {{{items}}}')
            out = subprocess.run(['osascript', '-e', script],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                # 弹窗失败（非 GUI 会话/权限限制）——打印原因方便排障，不静默
                print(f'⚠️ 浏览器选择弹窗未弹出（{out.stderr.strip()[:120]}），回退文件选择…')
                return None
            if not out.stdout.strip() or out.stdout.strip().lower() == 'false':
                return None  # 用户取消
            chosen = out.stdout.strip()
            for n, p in browsers:
                if n == chosen:
                    return (n, p)
            return None
        elif sys.platform == 'win32':
            # PowerShell 列表选择框（Windows Forms ListBox 弹窗）
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f=New-Object System.Windows.Forms.Form;"
                "$f.Text='配送运营助手 - 选择浏览器';"
                "$f.Width=360;$f.Height=280;$f.StartPosition='CenterScreen';"
                "$lbl=New-Object System.Windows.Forms.Label;"
                "$lbl.Text='检测到以下浏览器，请选择用哪个作为载体：';"
                "$lbl.SetBounds(15,10,320,25);$f.Controls.Add($lbl);"
                "$lb=New-Object System.Windows.Forms.ListBox;"
                "$lb.SetBounds(15,40,320,150);"
                "$items=@(" + ','.join("'" + n.replace("'", "''") + "'" for n in names) + ");"
                "$lb.Items.AddRange($items);$lb.SelectedIndex=0;$f.Controls.Add($lb);"
                "$btn=New-Object System.Windows.Forms.Button;"
                "$btn.Text='确定';$btn.DialogResult='OK';$btn.SetBounds(120,200,80,30);$f.Controls.Add($btn);"
                "$btn2=New-Object System.Windows.Forms.Button;"
                "$btn2.Text='取消';$btn2.DialogResult='Cancel';$btn2.SetBounds(210,200,80,30);$f.Controls.Add($btn2);"
                "$f.AcceptButton=$btn;$f.CancelButton=$btn2;"
                "if($f.ShowDialog() -eq 'OK'){$lb.SelectedItem}")
            out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            chosen = out.stdout.strip()
            for n, p in browsers:
                if n == chosen:
                    return (n, p)
            return None
    except Exception:
        return None
    return None


def check_browser_choice():
    """只读已记住的浏览器选择（不弹窗）。返回 (exe路径, 显示名) 或 (None, None)"""
    try:
        cfg = load_cfg()
        if cfg.get('browser_path'):
            p = _normalize_browser_path(cfg['browser_path'])
            if p:
                return p, Path(p).stem.replace('.exe', '')
    except Exception:
        pass
    return None, None


def ensure_browser_choice(force=False):
    """启动引导：自动扫描浏览器 → 没记住/路径失效就弹列表让用户选 → 记住 cfg['browser_path']。
    返回 (exe路径, 显示名) 或 (None, None)=用户取消/没浏览器
    force=True：跳过「已记住」直接重新选择（控制台窗口「选择浏览器」按钮用）"""
    # 已记住且路径仍有效 → 直接用（force 时跳过，让用户重选）
    if not force:
        exe, name = check_browser_choice()
        if exe:
            return exe, name
    browsers = list_browsers()
    if not browsers:
        return None, None
    if len(browsers) == 1:
        # 只有一个浏览器：不用弹，直接用（避免打扰）
        cfg = load_cfg()
        cfg['browser_path'] = browsers[0][1]
        save_cfg(cfg)
        return browsers[0][1], browsers[0][0]
    picked = choose_browser_prompt(browsers)
    if not picked:
        # 列表弹窗被取消/非GUI会话弹不出 → 回退文件选择框兜底（2026-08-30）
        fp = choose_browser_gui()
        if fp:
            picked = (os.path.basename(fp), fp)
    if picked:
        cfg = load_cfg()
        cfg['browser_path'] = picked[1]
        save_cfg(cfg)
        return picked[1], picked[0]
    return None, None


def choose_browser_gui():
    """弹系统文件选择框让用户手动选浏览器（Mac=osascript / Win=powershell，零额外依赖）。
    返回规整后的可执行文件路径；用户取消/失败返回 None。"""
    try:
        if sys.platform == 'darwin':
            script = ('POSIX path of (choose file with prompt '
                      '"请选择浏览器程序（Chrome 或 Edge，通常位于 /Applications）" '
                      'default location (path to applications folder))')
            out = subprocess.run(['osascript', '-e', script],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                return None  # 用户取消
            return _normalize_browser_path(out.stdout.strip())
        elif sys.platform == 'win32':
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$f=New-Object System.Windows.Forms.OpenFileDialog;"
                  "$f.Filter='浏览器程序 (chrome.exe;msedge.exe)|chrome.exe;msedge.exe|所有文件|*.*';"
                  "$f.InitialDirectory=$env:ProgramFiles;"
                  "if($f.ShowDialog() -eq 'OK'){$f.FileName}")
            out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            return _normalize_browser_path(out.stdout.strip())
    except Exception:
        return None
    return None


def find_chrome():
    # 用户手动选择的浏览器优先（2026-08-30 加：各机器 Chrome 路径/版本不一，自动探测失败时让使用者自己选）
    try:
        cfg = load_cfg()
        if cfg.get('browser_path'):
            p = _normalize_browser_path(cfg['browser_path'])
            if p:
                return p
    except Exception:
        pass
    if sys.platform == 'darwin':
        # 优先「Dispatcher Chrome」分身（独立 bundle ID，可带 9223 启动；
        # 主 Chrome 已在运行时会吞掉新实例的调试端口参数，导致 9223 绑不上）
        cands = [str(Path.home() / 'Applications/Dispatcher Chrome.app/Contents/MacOS/Google Chrome'),
                 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', 'C:/Users/Default/AppData/Local'))
        cands = [
            str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'),
            str(Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe'),
            str(base / 'Google/Chrome/Application/chrome.exe'),
            # Edge 备选（Windows 自带）
            str(Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'),
            str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe'),
        ]
    else:
        cands = ['/usr/bin/google-chrome', '/usr/bin/chromium']
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def _cdp_alive(timeout=3):
    try:
        urllib.request.urlopen(f'{CDP_URL}/json/version', timeout=timeout).read()
        return True
    except Exception:
        return False


# 调试浏览器启动失败节流（2026-08-30 W11 实测事故：Edge 起不来时 refresh_once 每 60s
# Popen 一个新 Edge → 无限累积 278 个进程 ~8GB。修复：失败后 10 分钟内不再重复拉起，
# 且每次拉起前先杀掉 9223 端口上的残留调试进程）
_last_launch_attempt = 0.0


def ensure_debug_chrome(headless=True, force=False):
    """调试 Chrome 不在才启动。返回 True=本次启动的（退出时应清理）；
    False=尝试过但没起来；None=未尝试（浏览器缺失/节流中）
    force=True：绕过节流（登录引导等用户主动操作，2026-08-30 修复）
    找不到浏览器/启动失败时引导用户手动选择（2026-08-30 加）"""
    global _last_launch_attempt
    if _cdp_alive():
        return False
    exe = find_chrome()
    if not exe:
        print('⚠️ 自动探测未找到浏览器，请手动选择浏览器程序…')
        picked = choose_browser_gui()
        if picked:
            cfg = load_cfg()
            cfg['browser_path'] = picked
            save_cfg(cfg)
            print(f'✅ 已记住浏览器：{picked}')
            exe = picked
        else:
            return None  # 用户取消
    now = time.time()
    if not force and now - _last_launch_attempt < 600:
        return None  # 10 分钟内失败过：节流，绝不重复拉进程（防进程爆炸）
    _last_launch_attempt = now
    cleanup_chrome(True)  # 清掉之前可能残留的调试进程（按 9223 端口杀）
    # 启动参数（2026-10-29 实测）：这些"瘦身"参数只减少后台网络/上报，
    # **不减内存**（实测 402MB → 423MB，持平）。Chrome 156 已把 GPU 变软件渲染、
    # 站点隔离强制，renderer 进程数压不下来。想真省内存只能减少常驻时间，别再调参。
    _slim = [
        '--disable-gpu', '--disable-software-rasterizer',
        '--disable-background-networking', '--disable-component-update',
        '--disable-domain-reliability', '--disable-breakpad', '--no-pings',
        '--disable-sync', '--disable-translate', '--disable-default-apps',
        '--js-flags=--max-old-space-size=512',
        '--disable-features=OptimizationHints,MediaRouter,Translate,InterestFeedContentSuggestions',
        # 静音：2026-11-09 用户投诉——无头实例停在调度台极速版(dispatcher.ele.me iframe)时，
        # 页面自带语音播报「骑手下班」会经蓝牙出声，人没开浏览器也被吓一跳。抓数据不需要声音。
        '--mute-audio',
    ]
    cmd = [exe, f'--remote-debugging-port={CDP_PORT}', f'--user-data-dir={CDP_DIR}',
           '--no-first-run', '--no-default-browser-check', '--new-window'] + _slim
    if headless:
        cmd.insert(1, '--headless=new')
    # Chrome 输出写日志（windowed 打包无控制台，启动失败原因必须落盘，2026-08-30）
    try:
        chrome_log = Path.home() / '配送运营助手' / 'chrome.log'
        try:
            chrome_log.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        with open(chrome_log, 'w') as _flog:
            proc = subprocess.Popen(cmd + ['about:blank'], stdout=_flog, stderr=subprocess.STDOUT)
        print(f'Chrome Popen OK pid={proc.pid} → {chrome_log}')
    except Exception as e:
        print(f'❌ Chrome Popen 异常: {e}')
        return False
    for _ in range(20):
        if _cdp_alive():
            return True
        time.sleep(1)
    # 20 秒仍没起来：windowed 打包静默跳过弹窗（避免卡用户），脚本模式保留 GUI 引导
    if getattr(sys, 'frozen', False):
        print('⚠️ 浏览器 20 秒未能启动（原因见 chrome.log），请检查系统浏览器或杀毒软件拦截')
        return False
    print('⚠️ 浏览器未能正常启动，可尝试手动选择其他浏览器（如 Edge）…')
    picked = choose_browser_gui()
    if picked:
        cfg = load_cfg()
        cfg['browser_path'] = picked
        save_cfg(cfg)
        print(f'✅ 已切换浏览器：{picked}')
        cleanup_chrome(True)
        subprocess.Popen([picked, f'--remote-debugging-port={CDP_PORT}',
                          f'--user-data-dir={CDP_DIR}',
                          '--no-first-run', '--no-default-browser-check', '--new-window',
                          'about:blank'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if _cdp_alive():
                return True
            time.sleep(1)
    # 仍没起来：不要假装成功（调用方会误以为浏览器在，connect 才失败）
    return False


def set_window_state(state: str):
    """设置调试 Chrome 窗口状态：minimized / normal / maximized。
    静默后台模式用：启动有头窗口后立即最小化，不弹到台前（2026-08-22 用户要求）；
    需要使用者登录时恢复 normal。失败静默忽略，不影响功能。"""
    try:
        tabs = json.loads(urllib.request.urlopen(f'{CDP_URL}/json', timeout=5).read())
        tab = next((t for t in tabs if t.get('type') == 'page'), None)
        if not tab:
            return
        import websocket
        ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=5, suppress_origin=True)
        seq = [0]
        def _cdp(method, params=None):
            seq[0] += 1
            mid = seq[0]
            ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get('id') == mid:
                    return msg
        r = _cdp('Browser.getWindowForTarget', {'targetId': tab['id']})
        wid = (r.get('result') or {}).get('windowId')
        if wid:
            _cdp('Browser.setWindowBounds', {'windowId': wid, 'bounds': {'windowState': state}})
        ws.close()
    except Exception:
        pass


def app_base_dir():
    """程序根目录：脚本模式=程序/；PyInstaller 打包后：
    Windows exe=exe 所在目录（可写）；macOS .app=~/配送运营助手/（app 包内不可写）"""
    if getattr(sys, 'frozen', False):
        exe = Path(sys.executable).resolve()
        if sys.platform == 'darwin' and 'Contents/MacOS' in str(exe):
            d = Path.home() / '配送运营助手'
            d.mkdir(parents=True, exist_ok=True)
            return d
        return exe.parent
    return Path(__file__).resolve().parent.parent  # cdp_core.py 在 modules/ 下


def data_dir():
    """可写数据目录（看板/临时文件放这里，打包后放 exe 旁边）"""
    return app_base_dir() / 'data'


def cleanup_chrome(started_by_me):
    """仅清理本脚本启动的调试 Chrome"""
    if not started_by_me:
        return
    # ⚠️ 调试日志（2026-08-30）：谁调用了 cleanup → 谁在杀 Edge
    try:
        print(f'[cleanup_chrome called by_me={started_by_me}]')   # 不打印调用栈（曾刷爆 board.err.log）
    except Exception:
        pass
    if sys.platform == 'win32':
        # 按 9223 端口找 PID 杀（WINDOWTITLE 过滤对 headless 实例不可靠，匹配不到）
        try:
            nr = subprocess.run(['netstat', '-ano'], capture_output=True, text=True, timeout=30, **_no_window_kw())
            out = nr.stdout or ''
            pids = set()
            for line in out.splitlines():
                if 'LISTENING' in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pids.add(parts[-1])
            for p in pids:
                subprocess.run(['taskkill', '/F', '/PID', str(p)], capture_output=True, **_no_window_kw())
        except Exception:
            pass
    else:
        try:
            # 杀整个调试 Chrome 进程组：按 user-data-dir 匹配（Chrome 主进程+子进程都带该参数；
            # 只杀监听端口的进程会残留子进程+SingletonLock 锁 → 重启的新实例起不来，2026-08-30 实测）
            os.system(f"pkill -9 -f '{CDP_DIR}' 2>/dev/null")
            # 兜底：再按端口杀一次（老版本/Edge 命令行可能不带 user-data-dir 顺序差异）
            pids = os.popen(f'lsof -ti:{CDP_PORT} -sTCP:LISTEN 2>/dev/null').read().strip().split()
            for p in pids:
                if p:
                    os.system(f'kill -9 {p} 2>/dev/null')
        except Exception:
            pass
        # 清 Chrome profile 锁（SingletonLock/Socket/Cookie 指向已死进程，残留会让新实例拒绝启动）
        try:
            for lock in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                lp = os.path.join(CDP_DIR, lock)
                if os.path.exists(lp):
                    try:
                        os.remove(lp)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(1)


# ── CDP 连接 ──
def connect():
    """连接调试 Chrome。返回 (ws, cdp函数) 或 (None, None)"""
    try:
        import websocket
    except ImportError:
        print('❌ 缺少 websocket 依赖，请先运行环境自检')
        return None, None
    try:
        tabs = json.loads(urllib.request.urlopen(f'{CDP_URL}/json', timeout=5).read())
    except Exception:
        print('❌ 无法连接调试浏览器')
        return None, None
    tab = None
    for t in tabs:
        if t.get('type') == 'page' and 'aeolus.ele.me' in t.get('url', ''):
            tab = t
            break
    if not tab:
        for t in tabs:
            if t.get('type') == 'page':
                tab = t
                break
    if not tab:
        print('❌ 调试浏览器无可用页面')
        return None, None
    try:
        ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=30, suppress_origin=True)
        ws.settimeout(10)
    except Exception as e:
        print(f'❌ 连接失败: {e}')
        return None, None

    seq = [0]
    def cdp(method, params=None, wait=20):
        seq[0] += 1
        mid = seq[0]
        ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            if msg.get('id') == mid:
                return msg
        return None
    return ws, cdp


def navigate_aeolus(cdp):
    """导航到 Aeolus 首页（确保在 aeolus 上下文，fetch 不被 baxia 拦）"""
    cdp('Page.navigate', {'url': AEOLUS_HOME})
    time.sleep(5)


def call_api(cdp, path, body, ver='1.0', headers=None, timeout=30000):
    """在 aeolus 页面上下文调 httpizza 接口"""
    url = f'https://httpizza.ele.me/xtop/{path}/{ver}'
    hdrs = {'Content-Type': 'application/json'}
    if headers:
        hdrs.update(headers)
    expr = (
        '(async()=>{try{'
        'const r=await fetch("' + url + '",{'
        'method:"POST",headers:' + json.dumps(hdrs) + ','
        'credentials:"include",body:' + json.dumps(json.dumps(body)) + ','
        'signal:AbortSignal.timeout(' + str(timeout) + ')});'
        'return(await r.text());'
        '}catch(e){return "ERR:"+e.message;}})()'
    )
    r = cdp('Runtime.evaluate', {'expression': expr, 'awaitPromise': True, 'returnByValue': True})
    v = None
    if r:
        v = r.get('result', {}).get('result', {}).get('value', '')
    return v or ''


def fetch_init(cdp):
    """直接 fetch init（非 xtop 路径）"""
    expr = ('(async()=>{try{const r=await fetch("' + INIT_URL +
            '",{credentials:"include"});return(await r.text());}'
            'catch(e){return "ERR:"+e.message;}})()')
    r = cdp('Runtime.evaluate', {'expression': expr, 'awaitPromise': True, 'returnByValue': True})
    v = ''
    if r:
        v = r.get('result', {}).get('result', {}).get('value', '')
    if not v or v.startswith('ERR:'):
        return None
    try:
        d = json.loads(v)
    except Exception:
        return None
    if d.get('code') != 200:
        return None
    data = d.get('data') or {}
    user = data.get('user') or {}
    if not user.get('name'):
        return None
    roles = user.get('roles') or []
    info = {
        'user_name': user.get('name', ''),
        'role_name': roles[0].get('role_name', '') if roles else '',
        'org_name': data.get('org_name', ''),
        'team_name': data.get('team_name', ''),
        'team_id': data.get('team_id', ''),
        'agency_id': data.get('agency_id', data.get('org_id', '')),
    }
    return info


# ── 登录态自愈：从本机守护实例迁移 cookie ─────────────────────────────────
GUARD_CDP_PORT = 9223   # 本机守护脚本（播报/申诉/safetycy）固定占用的调试端口


def sync_cookies_from(src_port=GUARD_CDP_PORT):
    """把本机守护实例（默认 9223）的 Aeolus 登录 cookie 复制到本实例浏览器。

    场景：看板/分发实例 headless 常驻，夜间静默后会话过期，醒来落在统一登录中心；
    无头模式没有窗口可人工登录 → 每轮刷新判定「登录已失效」后死等，看板整天停更。
    守护实例的会话由播报脚本持续访问续期，迁移过来即可自愈。
    返回 (成功数, 失败数)；源实例不可用返回 (0, 0)。
    """
    try:
        import websocket
    except ImportError:
        return (0, 0)
    if int(src_port) == int(CDP_PORT):
        return (0, 0)

    def _pick(port):
        try:
            tabs = json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:{port}/json/list', timeout=5).read())
        except Exception:
            return None
        for t in tabs:
            if t.get('type') == 'page' and 'aeolus.ele.me' in t.get('url', ''):
                return t
        for t in tabs:
            if t.get('type') == 'page':
                return t
        return None

    class _C:
        def __init__(self, ws_url):
            self.w = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
            self.w.settimeout(15)
            self.seq = 0

        def call(self, method, params=None, wait=20):
            self.seq += 1
            mid = self.seq
            self.w.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    m = json.loads(self.w.recv())
                except Exception:
                    continue
                if m.get('id') == mid:
                    return m
            return None

        def close(self):
            try:
                self.w.close()
            except Exception:
                pass

    src = _pick(src_port)
    dst = _pick(CDP_PORT)
    if not src or not dst:
        return (0, 0)
    ok = bad = 0
    try:
        s = _C(src['webSocketDebuggerUrl'])
        ck = s.call('Network.getAllCookies')
        s.close()
        cookies = ((ck or {}).get('result') or {}).get('cookies') or []
        if not cookies:
            return (0, 0)
        keep = [c for c in cookies
                if any(d in c.get('domain', '') for d in ('ele.me', 'alibaba', 'taobao'))]
        d = _C(dst['webSocketDebuggerUrl'])
        d.call('Network.enable')
        for c in keep:
            p = {'name': c['name'], 'value': c['value'], 'domain': c['domain'],
                 'path': c.get('path') or '/', 'secure': bool(c.get('secure')),
                 'httpOnly': bool(c.get('httpOnly'))}
            if c.get('expires') and c['expires'] > 0:
                p['expires'] = c['expires']
            if c.get('sameSite') in ('Strict', 'Lax', 'None'):
                p['sameSite'] = c['sameSite']
            r = d.call('Network.setCookie', p)
            if ((r or {}).get('result') or {}).get('success'):
                ok += 1
            else:
                bad += 1
        d.close()
    except Exception as e:
        print(f'⚠️ 登录态迁移异常: {e}')
    return (ok, bad)


def list_teams(cdp):
    """getTeamsAll：当前账号可见站点列表。失败返回空列表"""
    try:
        r = call_api(cdp, TEAMS_API, {'params': {'request': {}}})
        if not r or r.startswith('ERR:'):
            return []
        d = json.loads(r)
        data = d.get('result', {}).get('data', d.get('data'))
        teams = []
        if isinstance(data, list):
            for t in data:
                tid = t.get('teamId') or t.get('team_id') or t.get('id')
                tname = t.get('teamName') or t.get('team_name') or t.get('name')
                if tid:
                    teams.append({'team_id': str(tid), 'team_name': str(tname or tid)})
        elif isinstance(data, dict):
            for key in ('list', 'teams', 'items'):
                for t in (data.get(key) or []):
                    tid = t.get('teamId') or t.get('team_id') or t.get('id')
                    tname = t.get('teamName') or t.get('team_name') or t.get('name')
                    if tid:
                        teams.append({'team_id': str(tid), 'team_name': str(tname or tid)})
        return teams
    except Exception:
        return []


def ensure_login(need_input_wait=True):
    """
    确保已登录 Aeolus。
    流程：启动 Chrome（有头窗口常驻：这就是使用方的 Aeolus 风神后台，单会话不顶号）
          → 导航 Aeolus → 轮询 init 直到拿到登录人信息（最多 5 分钟）
    返回 (info, started_by_me)；失败返回 (None, started_by_me)
    """
    cfg = load_cfg()
    # 分发版：始终有头窗口（本机版的无头静默逻辑在 ~/dispatch-scripts 自己维护，不混入分发版）
    started = ensure_debug_chrome(headless=False)
    if started is None:
        print('❌ 未找到浏览器（Chrome/Edge），请先安装浏览器后重试')
        return None, False

    ws, cdp = connect()
    if not ws:
        return None, started
    try:
        navigate_aeolus(cdp)
        info = fetch_init(cdp)
        if info:
            cfg['logged_in'] = True
            save_cfg(cfg)
            return info, started
        # 未登录 / 会话失效 → 引导（会话掉了也必须给可见窗口，不能只干等）
        print('\n⚠️ 检测到 Aeolus 登录会话失效（可能被其他登录顶掉了）。')
        print('   请确认主浏览器/手机没有同时登录同一账号（单会话会互相顶号）。')
        if getattr(sys, 'frozen', False):
            # ⚠️ windowed 打包（无控制台）绝不能依赖 input()：后台/管道启动时
            #    input() 不会 EOFError 而是永久阻塞 → 程序"卡死桌面"（2026-08-30 实测）。
            #    不等回车，直接进登录引导（下面会弹浏览器窗口让用户登录）。
            print('   正在打开登录窗口，请在弹出的浏览器中完成登录…')
        else:
            try:
                input('   确认已退出其他登录后，按回车重新登录...')
            except EOFError:
                pass  # windowed 打包无控制台：不等回车，直接进入登录引导
        # ⚠️ 2026-08-30 修复"浏览器闪退"：浏览器已经在跑、已经导航到 Aeolus 页面，
        #    绝不能再 cleanup_chrome 杀掉重启（用户看到窗口闪一下就没了，重启还常失败）。
        #    直接复用当前窗口，让用户登录即可。
        navigate_aeolus(cdp)
        print('\n📢 首次使用需要登录 Aeolus 账号：')
        print('   已自动打开浏览器窗口，请在窗口中完成登录（账号密码或短信验证码）')
        print('   登录成功后本程序会自动继续，无需其他操作...')
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(4)
            info = fetch_init(cdp)
            if info:
                cfg['logged_in'] = True
                save_cfg(cfg)
                print('✅ 登录成功')
                return info, started
        print('⏰ 等待登录超时（5 分钟），请重试')
        return None, started
    finally:
        try:
            ws.close()
        except Exception:
            pass
