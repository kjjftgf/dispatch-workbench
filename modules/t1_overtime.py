#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T-1 超时分析（分发版）
链路：CDP 提交 T-1 全天运单导出任务 → 轮询 → OSS 下载 → zipfile 解析 73列运单
      → 8月考核口径复合超时分析 → Desktop/T-1超时分析_日期.xlsx（汇总+明细）
"""
import json, re, subprocess, sys, tempfile, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# Windows 控制台/管道默认 GBK，emoji 会崩；统一 UTF-8 输出
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from modules import cdp_core
except ImportError:
    import cdp_core  # 直接运行时

HOME = Path.home()
OUT_DIR = HOME / 'Desktop'
TMP = Path(tempfile.gettempdir())


def _dl(url, out):
    """OSS URL 可能带 & 签名参数 — 必须 subprocess 列表形式"""
    r = subprocess.run(['curl', '-sL', '-H', 'User-Agent: Mozilla/5.0',
                        str(url).replace('http://', 'https://'), '-o', str(out)],
                       capture_output=True, timeout=300, **cdp_core._no_window_kw())
    if r.returncode != 0 or not out.exists() or out.stat().st_size <= 10000:
        raise RuntimeError(f'{out.name} 下载失败/空文件')


# ═══ 1. 导出 T-1 全天运单 ═══
def submit_export(cdp, team_id, start_ms, end_ms):
    body = {'params': {'request': {
        'trackingFinishStart': start_ms, 'trackingFinishEnd': end_ms,
        'merchantConsumerDistanceStart': '0', 'merchantConsumerDistanceEnd': '999999',
        'payAmountStart': '0', 'payAmountEnd': '999999',
        'weightStart': '0', 'weightEnd': '999999',
        'trackingFinishTimePeriodList': ['0-86400'],
        # teamIds 必须带：不带按会话默认站点导出，多站点账号会导错站（实测 NO_TEAM_ID_PERMISSIONS 校验）
        'teamIds': [int(team_id)]}}}
    # 提交必须单前缀
    r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.exportTracking', body, ver='2.0')
    print('  提交返回:', r[:200])
    if r.startswith('ERR:'):
        return False
    try:
        d = json.loads(r)
        if (d.get('result') or {}).get('code') == 'NO_TEAM_ID_PERMISSIONS':
            print('  ❌ 所选站点当前账号无权限导出，请核对站点/登录账号')
            return False
    except Exception:
        pass
    return True


def wait_task(cdp, now_ms, timeout=600):
    """轮询 queryExportTask(双前缀)，找 exportTime >= 提交时刻-5min 的 status=1 新任务"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(10)
        r = cdp_core.call_api(cdp,
            'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryExportTask',
            {'params': {'request': {'current': 1, 'pageSize': 15}}}, ver='2.0')
        try:
            d = json.loads(r)
            tasks = d.get('result', {}).get('data', {}).get('data', [])
            for t in tasks:
                et = t.get('exportTime', 0) or 0
                if et >= now_ms - 300000 and t.get('status') == 1:
                    print(f'  ✅ 新任务就绪 id={t["id"]}')
                    return t
            pend = [t for t in tasks if t.get('status') != 1]
            print(f'  轮询中: 完成{len(tasks) - len(pend)}/等待{len(pend)}')
        except Exception as e:
            print(f'  轮询异常: {e}')
    return None


def get_oss(cdp, task_id):
    r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryTaskUrl',
                          {'params': {'request': {'taskId': task_id}}}, ver='2.0')
    d = json.loads(r)
    return d.get('result', {}).get('data', '')


# ═══ 2. 解析运单 xlsx ═══
def parse_xlsx(path):
    """zipfile 直读 sheet1.xml；inlineStr 取 <is><t>。返回 (表头列表, 行列表[dict 列字母->值])"""
    import zipfile
    with zipfile.ZipFile(path) as z:
        s = z.read('xl/worksheets/sheet1.xml').decode('utf-8', 'ignore')
    hdrs, rows = [], []
    for m in re.finditer(r'<row r="(\d+)"[^>]*>(.*?)</row>', s, re.S):
        rn = int(m.group(1))
        cells = {}
        for c in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', m.group(2), re.S):
            col, attrs, inner = c.group(1), c.group(2), c.group(3)
            t = re.search(r't="([^"]*)"', attrs)
            t = t.group(1) if t else None
            if t == 'inlineStr':
                mm = re.search(r'<is><t[^>]*>(.*?)</t></is>', inner, re.S)
                cells[col] = mm.group(1) if mm else ''
            else:
                mm = re.search(r'<v>(.*?)</v>', inner, re.S)
                cells[col] = mm.group(1) if mm else ''
        if rn == 1:
            hdrs = [cells.get(chr(65 + i), '') for i in range(26 * 3)]
            hdrs = [h for h in hdrs if h]
        else:
            rows.append(cells)
    return hdrs, rows


# ═══ 3. 超时分析（8月考核口径）═══
def comp_overtime_sec(x):
    """X(秒) → 复合超时时长(秒)：X=送达-期望(预测T)"""
    if x <= 480:
        return 0
    if x <= 900:
        return x - 480
    if x <= 1800:
        return 420 + (x - 900) * 1.5
    return 420 + 1350 + (min(x, 5400) - 1800) * 4


def parse_ts(s):
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def hms_sec(s):
    """'HH:MM:SS' → 秒；'00:07:51' → 471；失败/空 → 0"""
    s = (s or '').strip()
    if not s:
        return 0
    parts = s.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(s)
    except ValueError:
        return 0


def run(cdp, team_id, team_name, date=None):
    """T-1 超时分析主流程。cdp 由调用方提供（已登录 aeolus 上下文）"""
    import zipfile  # noqa: F401  (保持与 parse_xlsx 同文件直觉)
    if not date:
        date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    day = date
    start = datetime.strptime(day + ' 00:00:00', '%Y-%m-%d %H:%M:%S')
    end = datetime.strptime(day + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    out_xlsx = TMP / f'运单_T1_{day.replace("-", "")}.xlsx'
    print(f'=== T-1({day}) 超时分析 · {team_name} ===')

    print('📤 提交运单导出任务...')
    if not submit_export(cdp, team_id, start_ms, end_ms):
        raise RuntimeError('❌ 导出提交失败')
    task = wait_task(cdp, now_ms)
    if not task:
        raise RuntimeError('❌ 未找到新导出任务（可能当日导出任务过多，稍后重试）')
    oss = get_oss(cdp, task['id'])
    if not oss:
        raise RuntimeError('❌ 无下载链接')
    _dl(oss, out_xlsx)
    print(f'✅ 运单下载完成: {out_xlsx.stat().st_size // 1024}KB')

    hdrs, rows = parse_xlsx(out_xlsx)
    print(f'解析完成: {len(rows)} 行')

    # 73列标准列号硬映射（1基 → 列字母）
    def L(n):
        s = ''
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    COL = {
        '运单id': L(7), '订单号': L(8), '骑手id': L(9), '骑手名称': L(10),
        '商家名称': L(12), '运单状态': L(15), '配送标品': L(16),
        '用户下单时间': L(21), '骑手送达时间': L(29), '平台期望时间': L(34),
        '是否超平台期望时间': L(35), '超平台期望送达时长': L(36),
        '配送时长': L(39), '是否二呼单': L(73),
    }

    def g(r, name):
        return r.get(COL[name], '')

    recs = [r for r in rows if g(r, '骑手id')]
    total = len(recs)
    success = [r for r in recs if g(r, '运单状态') == '配送成功']
    over = [r for r in success if g(r, '是否超平台期望时间') == '是']
    over_cnt = len(over)
    print(f'\n=== 汇总 ===')
    print(f'运单总数: {total} | 配送成功: {len(success)} | 超时单: {over_cnt} '
          f'({over_cnt * 100 / len(success):.1f}% 按成功单)' if success else '无成功单')

    # 超时时长分布
    secs = [hms_sec(g(r, '超平台期望送达时长')) for r in over]
    if secs:
        s30 = len([s for s in secs if s > 1800])
        print(f'超时时长: 平均 {sum(secs) / len(secs):.0f}s | 最长 {max(secs):.0f}s '
              f'| >8min {len([s for s in secs if s > 480])} | >15min {len([s for s in secs if s > 900])} | >30min {s30}')

    # 严重程度（复合口径 轻/普/严）
    mild = normal = severe = 0
    for r in over:
        arrive, expect = parse_ts(g(r, '骑手送达时间')), parse_ts(g(r, '平台期望时间'))
        if not arrive or not expect:
            continue
        x = (arrive - expect).total_seconds()
        if x <= 480:
            continue
        if x <= 900:
            mild += 1
        elif x <= 1800:
            normal += 1
        else:
            severe += 1
    print(f'超时程度(按X=送达-预测T): 轻度8~15min {mild} | 普通15~30min {normal} | 严重>30min {severe}')

    # 按骑手聚合：超时单数 / 超时率 / 单均复合超时
    rider = defaultdict(lambda: {'name': '', 'valid': 0, 'over': 0, 'sum_comp': 0.0,
                                 'mild': 0, 'normal': 0, 'severe': 0, 'over_secs': []})
    for r in success:
        rid = g(r, '骑手id')
        if not rid:
            continue
        biaopin, erhu = g(r, '配送标品'), g(r, '是否二呼单')
        if '兜底' in biaopin or erhu == '是':
            continue
        arrive, expect = parse_ts(g(r, '骑手送达时间')), parse_ts(g(r, '平台期望时间'))
        x = (arrive - expect).total_seconds() if arrive and expect else 0
        comp = comp_overtime_sec(x) if x > 0 else 0
        d = rider[rid]
        d['name'] = g(r, '骑手名称')
        d['valid'] += 1
        d['sum_comp'] += comp
        if comp > 0:
            d['over'] += 1
            if x <= 900:
                d['mild'] += 1
            elif x <= 1800:
                d['normal'] += 1
            else:
                d['severe'] += 1
    total_valid = sum(d['valid'] for d in rider.values())
    total_comp = sum(d['sum_comp'] for d in rider.values())
    avg_all = total_comp / total_valid if total_valid else 0
    print(f'\n有效完单(剔兜底/二呼): {total_valid} | 全站单均复合超时: {avg_all:.1f}s ({avg_all / 60:.2f}min)')

    ranking = sorted(rider.items(), key=lambda kv: -kv[1]['sum_comp'] / kv[1]['valid'])
    print(f'\n=== 单均复合超时 TOP15（8月考核口径，有效单≥3）===')
    print(f'{"骑手":<10}{"有效单":>5}{"超时单":>5}{"超时率":>7}{"轻":>4}{"普":>4}{"严":>4}{"单均复合(分)":>12}')
    shown = 0
    for rid, d in ranking:
        if d['valid'] < 3:
            continue
        r_avg = d['sum_comp'] / d['valid']
        print(f'{d["name"]:<10}{d["valid"]:>5}{d["over"]:>5}{d["over"] / d["valid"] * 100:>6.1f}%'
              f'{d["mild"]:>4}{d["normal"]:>4}{d["severe"]:>4}{r_avg / 60:>11.1f}')
        shown += 1
        if shown >= 15:
            break

    # 按商家超时 TOP
    by_mer = Counter(g(r, '商家名称') for r in over)
    print(f'\n=== 超时单按商家 TOP10 ===')
    for name, n in by_mer.most_common(10):
        tot = sum(1 for r in success if g(r, '商家名称') == name)
        print(f'  {n:3d}/{tot:3d}  {name}')

    # 超时单明细
    det_rows = []
    for r in sorted(over, key=lambda r: -hms_sec(g(r, '超平台期望送达时长'))):
        det_rows.append((g(r, '骑手名称'), g(r, '商家名称'), g(r, '运单id'), g(r, '订单号'),
                         g(r, '骑手送达时间'), g(r, '平台期望时间'),
                         g(r, '超平台期望送达时长'), g(r, '配送标品'), g(r, '是否二呼单')))

    # ═══ 4. 输出 xlsx ═══
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws1 = wb.active
    ws1.title = '超时汇总'
    ws1.append(['T-1 超时分析', day])
    ws1.append(['运单总数', total])
    ws1.append(['配送成功', len(success)])
    ws1.append(['超时单数', over_cnt])
    ws1.append(['超时率(按成功单)', f'{over_cnt * 100 / len(success):.1f}%' if success else '-'])
    if secs:
        ws1.append(['平均超时时长(s)', round(sum(secs) / len(secs))])
        ws1.append(['最长超时(s)', round(max(secs))])
    ws1.append(['超时程度: 轻度8~15min', mild])
    ws1.append(['普通15~30min', normal])
    ws1.append(['严重>30min', severe])
    ws1.append([])
    ws1.append(['排名', '骑手名称', '有效完单', '超时单', '超时率%', '轻度', '普通', '严重', '单均复合超时(分)'])
    hd = Font(bold=True)
    fill = PatternFill('solid', fgColor='DDEBF7')
    for c in ws1[1]:
        c.font = hd
    rank = 0
    for rid, d in ranking:
        if d['valid'] < 3:
            continue
        rank += 1
        r_avg = d['sum_comp'] / d['valid']
        ws1.append([rank, d['name'], d['valid'], d['over'], round(d['over'] / d['valid'] * 100, 1),
                    d['mild'], d['normal'], d['severe'], round(r_avg / 60, 1)])
    ws2 = wb.create_sheet('超时明细')
    ws2.append(['骑手名称', '商家名称', '运单号', '订单号', '送达时间', '期望时间(预测T)',
                '超时时长(s)', '配送标品', '是否二呼单'])
    for c in ws2[1]:
        c.font = hd
        c.fill = fill
    for row in det_rows:
        ws2.append(list(row))
    for col, w in zip('ABCDEFGHI', (12, 22, 18, 18, 20, 20, 12, 10, 10)):
        ws2.column_dimensions[col].width = w
    out = OUT_DIR / f'T-1超时分析_{day.replace("-", "")}.xlsx'
    wb.save(out)
    print(f'\n✅ 已输出: {out}')

    # 清理运单临时文件
    try:
        out_xlsx.unlink(missing_ok=True)
    except Exception:
        pass
    return True


if __name__ == '__main__':
    import time as _t
    started = cdp_core.ensure_debug_chrome(headless=False)
    ws, cdp = cdp_core.connect()
    if not ws:
        sys.exit(1)
    try:
        cdp_core.navigate_aeolus(cdp)
        info = None
        for _ in range(6):
            info = cdp_core.fetch_init(cdp)
            if info:
                break
            _t.sleep(5)
        if not info:
            print('❌ 登录态失效，请通过主程序运行')
            sys.exit(1)
        cfg = cdp_core.load_cfg()
        run(cdp, cfg.get('team_id') or info['team_id'], cfg.get('team_name') or info['team_name'])
    finally:
        try:
            ws.close()
        except Exception:
            pass
        cdp_core.cleanup_chrome(started)
