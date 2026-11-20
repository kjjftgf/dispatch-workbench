#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gitee 全自动更新：启动时检测新版 → 自动下载 → 解压校验 → 备份替换 → 装坏自动回滚。
更新源：https://gitee.com/kjjftgf/dispatch-assistant/releases（公开仓库，匿名可查可下载）
流程：check_update → get_release_asset → download → extract → 校验 → 备份旧版 → 替换 → 写 marker
回滚：首启失败（marker 未清除）→ 下次启动自动恢复备份
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# 更新源（Gitee 公开仓库）：开源版默认关闭，只有自己发版才在环境变量里指定
#   export WORKBENCH_UPDATE_REPO=kjjftgf/dispatch-assistant
GITEE_OWNER, GITEE_REPO = (os.environ.get('WORKBENCH_UPDATE_REPO', '/').split('/') + ['', ''])[:2]
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}


def _ver_gt(a, b):
    """a > b？支持 v1.2 / v1.10 数字版本比较"""
    try:
        def num(s):
            return [int(x) for x in re.sub(r'[^0-9.]', '', s).split('.')]
        return num(a) > num(b)
    except Exception:
        return a > b


def check_update(current_version, timeout=8):
    """检查 Gitee 最新 release。返回 (latest_tag, html_url) 或 None（无新版/失败静默）"""
    if not (GITEE_OWNER and GITEE_REPO):
        return None  # 开源版未配置更新源 → 不做任何联网检查
    try:
        url = ('https://gitee.com/api/v5/repos/%s/%s/releases/latest'
               % (GITEE_OWNER, GITEE_REPO))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8'))
        tag = str(d.get('tag_name', '') or '')
        if tag and _ver_gt(tag, current_version):
            html = str(d.get('html_url', '') or '')
            if not html:
                html = 'https://gitee.com/%s/%s/releases' % (GITEE_OWNER, GITEE_REPO)
            return tag, html
    except Exception:
        pass
    return None


def get_release_asset(tag, want=None, timeout=15):
    """拿指定 tag 的 release 附件 zip 下载 URL。
    want=None→第一个 .zip（旧行为，源码版用）；
    want='win'→附件名含 Windows；want='mac'→附件名含 Mac
    （Gitee 自动归档的 vX.zip/tar.gz 不含平台词，自动被过滤，不会下错包）"""
    try:
        url = ('https://gitee.com/api/v5/repos/%s/%s/releases/tags/%s'
               % (GITEE_OWNER, GITEE_REPO, tag))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8'))
        for a in (d.get('assets') or []):
            name = str(a.get('name', '') or '')
            dl = str(a.get('browser_download_url', '') or '')
            if not (name.lower().endswith('.zip') and dl):
                continue
            if want == 'win' and 'windows' not in name.lower():
                continue
            if want == 'mac' and 'mac' not in name.lower():
                continue
            return dl
    except Exception:
        pass
    return None


def download(url, dest, timeout=300):
    """流式下载到 dest（大文件）。成功返回 True"""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get('Content-Length', 0) or 0)
            got = 0
            with open(dest, 'wb') as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if total and got < total:
                return False
        return dest.stat().st_size > 10000
    except Exception:
        return False


def _extract_zip(zip_path, dest_dir):
    """解压 zip（中文文件名兼容）"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            try:
                name = m.filename.encode('cp437').decode('utf-8')
            except Exception:
                name = m.filename
            target = dest_dir / name
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)


def print_update_hint(current_version):
    """提示式更新（打包版启动时调用；升级动作在控制台窗口完成）。
    失败静默不影响启动"""
    try:
        r = check_update(current_version)
        if r:
            tag, url = r
            print()
            print(f'📢 发现新版本 {tag}（当前 {current_version}）')
            if getattr(sys, 'frozen', False):
                print(f'   点运行控制台的【🔄 检测更新】→ 弹窗选「是」即可一键自动升级')
            else:
                print(f'   下载地址：{url}')
            print()
    except Exception:
        pass


def auto_update(current_version, app_dir, print_fn=print):
    """全自动更新主流程。app_dir=程序根目录（含 main.py / modules/ / data/）。
    返回 True=已更新；False=无更新；None=更新失败（静默不阻塞）"""
    app_dir = Path(app_dir)
    try:
        hit = check_update(current_version)
        if not hit:
            return False
        tag, _ = hit
        print_fn(f'📥 发现新版本 {tag}（当前 {current_version}），正在自动下载更新...')
        dl = get_release_asset(tag)
        if not dl:
            print_fn('⚠️ 获取下载地址失败，跳过更新')
            return None
        tmp_dir = Path(tempfile.gettempdir()) / 'da_update'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / ('update_%s.zip' % tag.replace('v', ''))
        if zip_path.exists():
            zip_path.unlink()
        if not download(dl, zip_path):
            print_fn('⚠️ 下载失败（网络问题），跳过更新')
            return None
        ex_dir = tmp_dir / 'extract'
        shutil.rmtree(ex_dir, ignore_errors=True)
        _extract_zip(zip_path, ex_dir)
        # 找程序目录（zip 内含 程序/ 或平铺）
        src = ex_dir / '程序' if (ex_dir / '程序').exists() else ex_dir
        if not (src / 'main.py').exists() or not (src / 'modules' / 'realtime_board.py').exists():
            print_fn('⚠️ 更新包校验失败（文件不完整），跳过更新')
            return None
        # 备份旧版（回滚用）
        bak = app_dir / 'data' / '.update_backup'
        shutil.rmtree(bak, ignore_errors=True)
        (bak / 'modules').mkdir(parents=True, exist_ok=True)
        for f in ('main.py', '使用说明.txt'):
            if (app_dir / f).exists():
                shutil.copy2(app_dir / f, bak / f)
        for f in (app_dir / 'modules').glob('*.py'):
            shutil.copy2(f, bak / 'modules' / f.name)
        # 替换
        shutil.copy2(src / 'main.py', app_dir / 'main.py')
        for f in (src / 'modules').glob('*.py'):
            shutil.copy2(f, app_dir / 'modules' / f.name)
        if (src / '使用说明.txt').exists():
            shutil.copy2(src / '使用说明.txt', app_dir / '使用说明.txt')
        # 写 marker：本次更新待确认（首启成功清除；失败下次启动回滚）
        (app_dir / 'data').mkdir(parents=True, exist_ok=True)
        (app_dir / 'data' / '.updated').write_text(tag, encoding='utf-8')
        print_fn(f'✅ 更新完成（{tag}），正在重新启动...')
        return True
    except Exception:
        return None


def restore_if_update_failed(app_dir):
    """启动时检测：上次更新后未正常完成（marker 遗留超过5分钟 = 更新后首次启动失败/崩溃）
    → 恢复备份。刚更新完重启（marker 新鲜）不恢复（那是正常首启，成功后会清除 marker）。
    返回 True=已回滚"""
    app_dir = Path(app_dir)
    marker = app_dir / 'data' / '.updated'
    bak = app_dir / 'data' / '.update_backup'
    try:
        if marker.exists() and (bak / 'main.py').exists():
            # marker 写入超过 5 分钟仍存在 → 上次启动没走完（异常/崩溃）→ 回滚
            if time.time() - marker.stat().st_mtime > 300:
                shutil.copy2(bak / 'main.py', app_dir / 'main.py')
                if (bak / '使用说明.txt').exists():
                    shutil.copy2(bak / '使用说明.txt', app_dir / '使用说明.txt')
                for f in (bak / 'modules').glob('*.py'):
                    shutil.copy2(f, app_dir / 'modules' / f.name)
                marker.unlink(missing_ok=True)
                return True
    except Exception:
        pass
    return False


def clear_update_marker(app_dir):
    """首启正常完成 → 清除待确认 marker"""
    try:
        (Path(app_dir) / 'data' / '.updated').unlink(missing_ok=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# 一键在线升级（v1.21，打包版 exe/app 专用）
# 原理：运行中的 exe 不能替换自己 → 主程序下载好新版 zip，生成一个
# 「升级器」脚本（Win=PowerShell / Mac=bash），启动它后立即退出；
# 升级器等旧进程退干净 → 解压新版覆盖（Win 保留 data/）→ 重启新程序。
# ══════════════════════════════════════════════════════════════════

def _zip_has_program(zip_path, plat):
    """粗验下载的 zip 是完整程序包（防坏包/下错包浪费一次替换）。
    plat='win'→需含 配送运营助手.exe 与 _internal/；'mac'→需含 .app/Contents/MacOS/"""
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            if plat == 'win':
                return (any(n.endswith('配送运营助手.exe') for n in names)
                        and any('/_internal/' in n for n in names))
            return any('.app/Contents/MacOS/' in n for n in names)
    except Exception:
        return False


def _ps1_escape(s):
    return str(s).replace("'", "''")


def _write_win_updater(zip_path, dst_dir):
    """生成 Windows 升级器 ps1（路径内嵌，UTF-8 BOM 保存）。
    dst_dir=旧 exe 所在目录。替换策略：解压新版到临时 → 删旧 exe+_internal
    → 新版整拷覆盖（data/ 与用户配置原样保留）。"""
    tmp = Path(tempfile.gettempdir()) / 'da_upgrade'
    tmp.mkdir(exist_ok=True)
    script = tmp / 'da_upgrade.ps1'
    log_f = tmp / 'da_upgrade_log.txt'
    ps = r'''$ErrorActionPreference = 'Continue'
function W($m){{ try {{ $m | Out-File -FilePath $log -Append -Encoding UTF8 }} catch {{}} }}
W '升级开始'
$newZip = '{zip}'
$dst = '{dst}'
$exe = Join-Path $dst '配送运营助手.exe'
$tmp = Join-Path $env:TEMP 'da_upd_extract'
$log = '{log}'
for($i=0; $i -lt 90; $i++){{
  if(-not (Get-Process -Name '配送运营助手' -ErrorAction SilentlyContinue)){{ break }}
  Start-Sleep -Seconds 1
}}
Start-Sleep -Seconds 2
if(Test-Path $tmp){{ Remove-Item $tmp -Recurse -Force }}
try {{
  Expand-Archive -LiteralPath $newZip -DestinationPath $tmp -Force
}} catch {{
  W ('解压失败: ' + $_.Exception.Message)
  exit 1
}}
$src = Join-Path $tmp '配送运营助手'
if(-not (Test-Path (Join-Path $src '配送运营助手.exe'))){{
  $f = Get-ChildItem -Path $tmp -Recurse -Filter '配送运营助手.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if($f){{ $src = $f.DirectoryName }} else {{ W '升级包结构异常(exe缺失)'; exit 1 }}
}}
Remove-Item (Join-Path $dst '_internal') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dst '配送运营助手.exe') -Force -ErrorAction SilentlyContinue
Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
W '替换完成, 准备重启'
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $newZip -Force -ErrorAction SilentlyContinue
Start-Process -FilePath $exe -WorkingDirectory $dst
'''.format(zip=_ps1_escape(zip_path), dst=_ps1_escape(dst_dir), log=_ps1_escape(log_f))
    script.write_text(ps, encoding='utf-8-sig')  # BOM：Windows PowerShell 5.1 认中文
    return script


def _write_mac_updater(zip_path, app_bundle):
    """生成 macOS 升级器 bash（路径内嵌）。app_bundle=旧 .app 完整路径。
    数据在 ~/配送运营助手/，与 .app 分离 → 直接整包替换并沿用旧路径/名字
    （用户快捷方式/记住的位置不失效）。"""
    tmp = Path(tempfile.gettempdir()) / 'da_upgrade'
    tmp.mkdir(exist_ok=True)
    script = tmp / 'da_upgrade.sh'
    sh = r'''#!/bin/bash
LOG="{log}"
echo "升级开始 $(date)" >> "$LOG"
ZIP='{zip}'
OLD='{app}'
TMP="$TMPDIR/da_upd_extract"
# 等旧进程退出（最多90秒）
for i in $(seq 1 90); do
  if ! pgrep -f 'Contents/MacOS/配送运营助手' >/dev/null 2>&1; then break; fi
  sleep 1
done
sleep 2
rm -rf "$TMP"
mkdir -p "$TMP"
if ! unzip -q -o "$ZIP" -d "$TMP" 2>>"$LOG"; then
  echo "解压失败" >> "$LOG"; exit 1
fi
NEW_APP=$(find "$TMP" -maxdepth 2 -name '*.app' -type d | head -1)
if [ -z "$NEW_APP" ]; then echo "升级包结构异常" >> "$LOG"; exit 1; fi
rm -rf "$OLD"
mv "$NEW_APP" "$OLD"
rm -rf "$TMP"
rm -f "$ZIP"
echo "替换完成 $(date)" >> "$LOG"
open "$OLD"
'''.format(log=_ps1_escape(tmp / 'da_upgrade_log.txt'), zip=_ps1_escape(zip_path),
           app=_ps1_escape(app_bundle))
    script.write_text(sh, encoding='utf-8')
    script.chmod(0o755)
    return script


def try_self_upgrade(current_version, log_fn=print):
    """一键升级主流程（仅打包版）。调用方负责在返回 'done' 后退出进程。
    返回: ('done', tag)=升级器已启动，进程即将退出重启；
          ('none', '')=无新版；('fail', 原因)=失败（进程继续运行）"""
    if not getattr(sys, 'frozen', False):
        return 'fail', '仅打包版支持一键升级'
    try:
        hit = check_update(current_version)
        if not hit:
            return 'none', ''
        tag, _ = hit
        plat = 'win' if sys.platform == 'win32' else 'mac'
        if sys.platform == 'darwin' and 'Contents/MacOS' not in str(Path(sys.executable).resolve()):
            return 'fail', '非 .app 形态，不支持一键升级'
        dl = get_release_asset(tag, want=plat)
        if not dl:
            return 'fail', '下载地址获取失败（新版本安装包可能还没传完，稍后再试）'
        log_fn(f'📥 发现新版本 {tag}（当前 {current_version}），正在下载升级包...')
        tmp = Path(tempfile.gettempdir()) / 'da_upgrade'
        tmp.mkdir(exist_ok=True)
        zip_path = tmp / ('da_update_%s.zip' % tag.replace('v', ''))
        if zip_path.exists():
            zip_path.unlink()
        if not download(dl, zip_path):
            return 'fail', '升级包下载失败（网络问题），请稍后重试'
        if not _zip_has_program(zip_path, plat):
            return 'fail', '升级包校验失败（文件不完整），请稍后重试'
        log_fn(f'✅ 升级包下载完成（{zip_path.stat().st_size // 1024 // 1024}MB），准备自动替换...')
        import subprocess
        if plat == 'win':
            exe_dir = Path(sys.executable).resolve().parent
            script = _write_win_updater(zip_path, exe_dir)
            subprocess.Popen(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-WindowStyle', 'Hidden', '-File', str(script)],
                close_fds=True)
        else:
            app_bundle = Path(sys.executable).resolve().parents[2]
            script = _write_mac_updater(zip_path, app_bundle)
            subprocess.Popen(['/bin/bash', str(script)], close_fds=True)
        log_fn(f'🚀 升级进行中（{tag}）：程序将自动替换并重启，约 10~30 秒后新版本生效，请稍候...')
        return 'done', tag
    except Exception as e:
        return 'fail', f'升级失败：{e}'
