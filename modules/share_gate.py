# -*- coding: utf-8 -*-
"""预览分享（对外只读链接）—— 让「发个链接给人看」不等于「把源码送人」。

做三件事：
1. 发链接：生成带 token 的网址 /s/<token>，可设有效期（默认 24h）、可随时撤销；
   对方不需要知道工作台密码，token 过期即失效。
2. 只读：预览会话只能 GET，任何 /api 写操作一律 403。
3. 发出去的页面是混淆构建产物（tools/build_protected.mjs），HTML/CSS 压缩、JS 变量名+字符串混淆，
   抓到手里也看不懂结构；页脚带水印（token 尾号 + 到期时间，泄露可溯源）。

本机（127.0.0.1/localhost）不受影响，照旧看可读版本。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # 程序根目录（脚本版=仓库根）
TOOLS = BASE / 'tools'
BUILD_JS = TOOLS / 'build_protected.mjs'
BUILDER = TOOLS / 'build_protected.py'          # python 薄封装，serve 时统一入口
PAGES = ['workbench.html', 'dash-c2.html', 'kpi-c2.html', 'safety.html', 'index.html']

_lock = threading.Lock()
_last_build = 0.0


def _node() -> str:
    """launchd 环境 PATH 极简，node 不一定在 PATH 里，逐个找。"""
    import shutil
    cands = [shutil.which('node') or '',
             str(Path.home() / '.local/bin/node'),
             '/opt/homebrew/bin/node',
             '/usr/local/bin/node']
    for c in cands:
        if c and Path(c).exists():
            return c
    return 'node'


def data_dir() -> Path:
    return BASE / 'data'


def store_path() -> Path:
    return data_dir() / 'shares.json'


def share_base() -> str:
    """对外链接前缀：data/share_base.txt 里写（默认用 frp 公网地址）。"""
    try:
        v = (data_dir() / 'share_base.txt').read_text(encoding='utf-8').strip()
        if v:
            return v.rstrip('/')
    except Exception:
        pass
    return ''


def protected_dir() -> Path:
    return data_dir() / '_protected'


# ── 令牌存储 ──────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        return json.loads(store_path().read_text(encoding='utf-8'))
    except Exception:
        return {'tokens': []}


def _save(d: dict) -> None:
    p = store_path()
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def create(hours: float = 24, note: str = '') -> dict:
    token = secrets.token_hex(8)
    now = time.time()
    rec = {'token': token, 'note': note, 'created': now,
           'expire': now + hours * 3600 if hours else 0, 'hits': 0}
    with _lock:
        d = _load()
        d.setdefault('tokens', []).append(rec)
        _save(d)
    return rec


def check(token: str):
    """有效返回记录（顺带计数），无效/过期返回 None。"""
    if not token:
        return None
    with _lock:
        d = _load()
        for rec in d.get('tokens', []):
            if rec.get('token') == token:
                exp = rec.get('expire') or 0
                if exp and time.time() > exp:
                    return None
                rec['hits'] = rec.get('hits', 0) + 1
                rec['last'] = time.time()
                _save(d)
                return rec
    return None


def revoke(token: str) -> bool:
    with _lock:
        d = _load()
        before = len(d.get('tokens', []))
        d['tokens'] = [r for r in d.get('tokens', []) if not r.get('token', '').startswith(token)]
        if len(d['tokens']) != before:
            _save(d)
            return True
    return False


def listing() -> list:
    out = []
    for rec in _load().get('tokens', []):
        exp = rec.get('expire') or 0
        out.append({
            'token': rec.get('token'), 'note': rec.get('note', ''),
            'created': time.strftime('%m-%d %H:%M', time.localtime(rec.get('created') or 0)),
            'expire': time.strftime('%m-%d %H:%M', time.localtime(exp)) if exp else '永不过期',
            'alive': (not exp) or time.time() < exp,
            'hits': rec.get('hits', 0),
        })
    return out


# ── 混淆产物 ──────────────────────────────────────────────────────────────
def _stale() -> bool:
    out = protected_dir()
    for f in PAGES:
        src = data_dir() / f
        if not src.exists():
            continue
        dst = out / f
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            return True
    return False


def ensure_protected(force: bool = False, min_interval: float = 45) -> bool:
    """需要时重建混淆产物（节流 min_interval 秒）。返回是否重建。"""
    global _last_build
    with _lock:
        if not force:
            if time.time() - _last_build < min_interval:
                return False
            if not _stale():
                return False
        _last_build = time.time()
    try:
        out = protected_dir()
        out.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([_node(), str(BUILD_JS), str(data_dir()), str(out)] + PAGES,
                           cwd=str(TOOLS), capture_output=True, text=True, timeout=180,
                           env={**os.environ, 'PATH': f"{Path(_node()).parent}:/usr/bin:/bin:/usr/sbin:/sbin"})
        if r.returncode != 0:
            print('⚠️ 混淆构建失败:', (r.stderr or '')[-400:])
            return False
        return True
    except Exception as e:
        print('⚠️ 混淆构建异常:', e)
        return False


def watermark_html(rec: dict, html: str) -> str:
    """在预览页注入水印条（挡住也删不掉，一眼能看出是预览版）。"""
    exp = rec.get('expire') or 0
    exp_txt = time.strftime('%m-%d %H:%M', time.localtime(exp)) if exp else '长期'
    tag = rec.get('token', '')[-4:]
    note = (rec.get('note') or '').replace('"', '')
    tip = f'预览版 · 仅供 {note} 查看 · 有效至 {exp_txt} · #{tag}' if note else \
          f'预览版 · 仅限本人查看 · 有效至 {exp_txt} · #{tag}'
    bar = (
        '<div id="__pv" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
        'pointer-events:none;text-align:center;font:11px/20px -apple-system,BlinkMacSystemFont,'
        '\'PingFang SC\',sans-serif;color:rgba(255,255,255,.55);background:rgba(0,0,0,.35);'
        'letter-spacing:.5px">' + tip + '</div>'
    )
    if '</body>' in html:
        return html.replace('</body>', bar + '</body>', 1)
    return html + bar


# ── CLI ───────────────────────────────────────────────────────────────────
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='工作台预览链接')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('new', help='生成预览链接')
    p.add_argument('--hours', type=float, default=24, help='有效期小时数，0=永久')
    p.add_argument('--note', default='', help='给谁看（会打在水印上）')
    p.add_argument('--host', default='http://39.106.200.1:24490', help='对外地址前缀')
    sub.add_parser('list', help='列出所有链接')
    r = sub.add_parser('revoke', help='撤销链接（可只给前几位）')
    r.add_argument('token')
    sub.add_parser('build', help='立即重建混淆产物')
    a = ap.parse_args(argv)

    if a.cmd == 'new':
        ensure_protected(force=True)
        rec = create(a.hours, a.note)
        print(f"{a.host}/s/{rec['token']}")
        exp = time.strftime('%m-%d %H:%M', time.localtime(rec['expire'])) if rec['expire'] else '永久'
        print(f"有效期至 {exp}  |  撤销: python3 {Path(__file__)} revoke {rec['token'][:8]}")
    elif a.cmd == 'list':
        rows = listing()
        if not rows:
            print('（没有链接）')
        for r in rows:
            print(f"{r['token']}  {'有效' if r['alive'] else '已过期'}  {r['expire']}  "
                  f"{r['hits']}次  {r['note']}")
    elif a.cmd == 'revoke':
        print('已撤销' if revoke(a.token) else '没找到这个 token')
    elif a.cmd == 'build':
        ok = ensure_protected(force=True)
        print('重建完成' if ok else '重建失败，看日志')


if __name__ == '__main__':
    import sys
    _main(sys.argv[1:])
