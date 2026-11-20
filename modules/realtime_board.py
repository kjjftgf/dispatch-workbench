#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时看板模块（分发版）
- 每 N 秒拉一次 Aeolus 实时出勤 + 排班统计
- 计算三大板块：全天达成 / 时段达成 / 档位达成
- 生成 HTML 看板（页面自带定时刷新，浏览器打开即用）
数据源：exportAttendance（同步导出，快）+ listScheduleStatistics（JSON）
"""
import json, math, os, re, shutil, subprocess, sys, time, threading, signal
import http.server
from datetime import datetime, timedelta
from pathlib import Path

try:
    from modules import cdp_core
except ImportError:
    import cdp_core

# ── 时段标准（8月运营方案，10时段，与日报一致）──
SHIFTS = [
    ('凌晨1',     0, 2.0, 1.5, 1, 4),
    ('凌晨2+凌晨3', 2, 6.0, 1.2, 1, 8),
    ('早餐1',     6, 8.0, 1.5, 1, 4),
    ('早餐2',     8, 10.5, 2.0, 1, 4),
    ('午高峰',   10.5, 13.5, 2.5, 5, 30),
    ('下午茶1',  13.5, 15.5, 1.5, 2, 7),
    ('下午茶2',  15.5, 17.5, 1.5, 2, 7),
    ('晚高峰',   17.5, 20.0, 2.0, 3, 20),
    ('夜宵1',    20.0, 22.0, 1.5, 2, 8),
    ('夜宵2',    22.0, 24.0, 1.5, 2, 8),
]
SHIFT_MAP = {s[0]: {'start': s[1], 'end': s[2], 'hours': s[3], 'orders': s[4], 'weight': s[5]} for s in SHIFTS}

# ── 档位标准（11月考核 202611，取低原则；运行期以 BASE_INFO API 直取为准，此仅为 API 失败兜底）──
# 2026-11-01 更新为 11 月官方值：单量 [18,26,32,39,48] / 时长 [6,6,6,7,8]（10月=[18,28,34,41,51]）
# ⚠️ 标准按月可能变，每月初拉 BASE_INFO 核对后同步本兜底（见 dispatcher-level-predict 技能）
LEVELS = [
    {'level': 1, 'orders': 18, 'hours': 6,  'label': '标1'},
    {'level': 2, 'orders': 26, 'hours': 6,  'label': '标2'},
    {'level': 3, 'orders': 32, 'hours': 6,  'label': '标3'},
    {'level': 4, 'orders': 39, 'hours': 7,  'label': '标4'},
    {'level': 5, 'orders': 48, 'hours': 8,  'label': '标5'},
]
BASE_HOURS, BASE_ORDERS = 6, 18  # 全天达成基准（标1）

# ── 站点ID→站点名 登记表（2026-10-06 坑复发修复）──
# 档位速查 handler 里不能用 refresh_once 的循环变量 team_name（handler 作用域没有），
# 看板每轮刷新登记到此，查询时按 team_id 取；取不到填 None = 不做站点过滤（单站点导出的等价行为）
_SITE_NAMES = {}

APP_VERSION = 'v1.34'  # 与 main.py VERSION 同步（检测更新用）
# ⚠️ 页面 CSS/JS 版本戳：改了页面 JS/CSS 必须递增 jsN —— fetchLatest 发现服务端版本与页面版本不一致
#    就自动整页刷新，用户不再需要手动强制刷新（局部刷新只换 body，<script> 里的旧 JS 不会更新）
BOARD_VER = APP_VERSION + '-js8'   # 2026-11-07 尖峰天贡献：明细加搜索框（searchSpike）→ 版本戳 +1 让已开的页面自动重载拿新 JS

# ── 晋升档位（档位达成表「晋升档位」列：标1-标5 逐档分色，CSS 类驱动以适配亮/暗双主题，2026-08-29）──
_LV_NEXT_CLS = {'标1': 'lvn-1', '标2': 'lvn-2', '标3': 'lvn-3',
                '标4': 'lvn-4', '标5': 'lvn-5'}


def next_lv_cls(next_txt):
    """晋升档位文本（'标N'/'—'）→ CSS 类；已满档'—'用 lvn-0（灰）。"""
    return _LV_NEXT_CLS.get(next_txt, 'lvn-0')

# ── 实时工作状态（listKnightAttendance workStatus 1-5，档位速查用，对齐 rider_level_query）──
_STATUS_MAP = {
    1: '🟢 上线',
    2: '🟡 小休',
    3: '🔴 下线',
    4: '🟡 申请小休',
    5: '🔴 申请下线',
}
_STATUS_TEXT = {'上班': 1, '在线': 1, '忙碌': 1, '小休': 2,
                '下班': 3, '申请小休': 4, '申请下班': 5}


def format_status(ws_val):
    """工作状态显示：实时接口 workStatus 1-5 → 上线/小休/下线/申请小休/申请下线。
    兼容出勤表中文值（上班/下班/申请下班...）。"""
    if ws_val is None or ws_val == '':
        return _STATUS_MAP[3]
    if isinstance(ws_val, str):
        v = ws_val.strip()
        if v.isdigit():
            return _STATUS_MAP.get(int(v), _STATUS_MAP[3])
        return _STATUS_MAP.get(_STATUS_TEXT.get(v, 3), _STATUS_MAP[3])
    if isinstance(ws_val, (int, float)):
        return _STATUS_MAP.get(int(ws_val), _STATUS_MAP[3])
    return _STATUS_MAP[3]

def _board_pass():
    """看板访问密码：读 data/board_pass.txt；没有就随机生成一个写回去。

    ⚠️ 不内置默认口令 —— 看板默认绑 0.0.0.0，同一 WiFi 下谁都能打开，
    写死弱口令等于把站点数据摊开。首启随机生成并在终端打印一次。
    """
    f = cdp_core.data_dir() / 'board_pass.txt'
    try:
        p = f.read_text(encoding='utf-8').strip()
        if p:
            return p
    except Exception:
        pass
    import secrets as _secrets
    p = _secrets.token_hex(4)          # 8 位十六进制
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(p, encoding='utf-8')
        print(f'🔑 首次运行已生成看板访问密码：{p}（账号 board，存在 data/board_pass.txt）')
    except Exception:
        pass
    return p

WORK_MAP = {'上班': '上线', '小休': '小休', '下班': '下线'}

def parse_time(t):
    """'7小时42分钟' / '07:42' → 小时数"""
    if not t or t == '-' or t is None:
        return 0.0
    t = str(t).strip()
    h = m = 0.0
    g = re.search(r'(\d+)小时', t)
    if g: h = float(g.group(1))
    g = re.search(r'(\d+)分钟', t)
    if g: m = float(g.group(1))
    if not h and not m:
        g = re.search(r'^(\d+):(\d+)$', t)
        if g: h, m = float(g.group(1)), float(g.group(2))
    return h + m / 60

def fmt_hours(h):
    """小时 → 'X小时Y分钟'"""
    if h is None or h == 0:
        return '0分钟'
    total_m = int(round(h * 60))
    hh, mm = divmod(total_m, 60)
    if hh == 0: return f'{mm}分钟'
    if mm == 0: return f'{hh}小时'
    return f'{hh}小时{mm}分钟'

def sched_left_min(sched_periods, now_h, cutoff_h=24.0):
    """当天还能补的「时段时长」：本人排班时段 ∩ [现在, 截止] 的分钟数。
    例：只排夜宵1(20:00-22:00)、现在 21:55 → 5 分钟；排夜宵2(22:00-24:00) → 2小时。
    差额超过这个数，今天就不可能补满（时段走完就不再计入时段时长）。"""
    tot = 0
    for pn in (sched_periods or []):
        st = SHIFT_MAP.get(pn)
        if not st:
            continue
        lo, hi = max(st['start'], now_h), min(st['end'], cutoff_h)
        if hi > lo:
            tot += int(round((hi - lo) * 60))
    return tot

def _dl_oss(cdp, path, body, ver='1.0', out_path=None):
    res = cdp_core.call_api(cdp, path, body, ver, timeout=60000)
    if not res or res.startswith('ERR:'):
        return None
    try:
        d = json.loads(res)
    except Exception:
        return None
    inner = d.get('result') or {}
    if inner.get('success') is False:
        return None
    data = inner.get('data', d.get('data', ''))
    url = ''
    if isinstance(data, dict):
        url = data.get('url', '') or data.get('ossUrl', '')
    elif isinstance(data, str):
        url = data
    if not url:
        return None
    url = url.replace('http://', 'https://')
    subprocess.run(['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', url, '-o', str(out_path)],
                   capture_output=True, timeout=120, **cdp_core._no_window_kw())
    if out_path.exists() and out_path.stat().st_size > 10000:
        return str(out_path)
    return None

def _att_data_rows(path):
    """数出勤文件数据行（姓名列），用于挑最全的一份"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        hdr = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        ci = next((i + 1 for i, h in enumerate(hdr) if h and '姓名' in str(h)), 1)
        n = sum(1 for r in range(2, ws.max_row + 1) if ws.cell(r, ci).value)
        wb.close()
        return n
    except Exception:
        return 0

def fetch_attendance(cdp, team_id, out_path):
    """同步导出当天实时出勤 → xlsx 路径（数据行过少时重拉一轮）

    ⚠️ 2026-09-28：同一接口的返回量会漂（晚复盘脚本 09-20 记录过连拉两次 54人/104人 = 真·半量坏文件），
    所以行数**异常少（<600 行，正常 88~92 人 ≈ 880~920 行）**时重拉一轮取最全。
    ❗ 但「名单比早些时候少人」不等于是坏文件：骑手**掉站**后后台会立刻把他从站点名单/今日考勤里摘掉
    （实测 22:20 一次掉了 4 人，实时考勤导出/实时考勤页/调度台 maplist 三个接口同步 92→88，排班统计仍是 92）。
    用户 2026-09-28 定稿：**这是正常数据变化，看板就跟后台实时名单走，不做「名单只增不减」缓存**。
    """
    best, best_n = None, -1
    for attempt in (1, 2):
        p = out_path if attempt == 1 else out_path.with_name(
            f'{out_path.stem}_t{attempt}{out_path.suffix}')
        got = _dl_oss(cdp, 'xtop.fs.special.realtimeattendance.exportAttendance',
                      {'params': {'request': {'teamIds': [int(team_id)]}}},
                      out_path=p)
        if not got:
            continue
        n = _att_data_rows(p)
        if n > best_n:
            best, best_n = p, n
        if n >= 600:
            break   # 正常一轮就够（88~92 人 × 10 时段 ≈ 880~920 行）
        print(f'  ⚠️ 出勤第{attempt}轮仅 {n} 行（疑半量文件），重拉一轮…')
    if not best:
        return None
    if best != out_path:
        shutil.copyfile(best, out_path)
    for extra in out_path.parent.glob(f'{out_path.stem}_t*.xlsx'):
        try:
            extra.unlink()
        except Exception:
            pass
    return str(out_path)

def fetch_schedule_stats(cdp, team_id, today):
    """排班统计 JSON → {period: {target, actual, order_predict}, site_total, all_day}"""
    r = cdp_core.call_api(cdp, 'xtop.fs.special.schedule.listScheduleStatistics',
                          {'params': {'request': {'startDate': today, 'endDate': today,
                                                  'teamId': int(team_id)}}})
    out = {'periods': {}, 'site_total': 0, 'all_day': {}}
    try:
        rows = json.loads(r).get('result', {}).get('data', [])
    except Exception:
        return out
    for entry in rows:
        out['site_total'] = entry.get('teamKnightNum', 0) or out['site_total']
        for p in entry.get('timeList', []):
            tname = p.get('timeName', '')
            if tname == '全天':
                out['all_day'] = {
                    'target': int(p.get('predictNum', 0) or 0),
                    'actual': int(p.get('actualNum', 0) or 0),
                }
            elif tname in ('凌晨2', '凌晨3'):
                # 凌晨2+凌晨3 合并成一个时段（对齐日报/监控表）
                key = '凌晨2+凌晨3'
                cur = out['periods'].setdefault(key, {'target': 0, 'actual': 0, 'order_predict': 0})
                cur['target'] += int(p.get('predictNum', 0) or 0)
                cur['actual'] += int(p.get('actualNum', 0) or 0)
                cur['order_predict'] += int(p.get('predictOrder', 0) or 0)
            elif tname and tname in SHIFT_MAP:
                out['periods'][tname] = {
                    'target': int(p.get('predictNum', 0) or 0),
                    'actual': int(p.get('actualNum', 0) or 0),
                    'order_predict': int(p.get('predictOrder', 0) or 0),
                }
    return out

def fetch_knight_number(cdp, team_id):
    """站点人数（静态口径 2026-09-05 用户拍板）：调度台极速版 maplist 的
    knightList 骑士列表行数 = 全站骑士账号总数，只随入职/离职/停用变化，
    不随上下线跳动（knightNumber 三态合计会漏「申请下班」的人且实时跳，
    实测 88→87→90，用户明确不要这种动态数）。
    失败/异常返回 None → 调用方回退排班统计 teamKnightNum。"""
    try:
        r = cdp_core.call_api(cdp, 'xtop.lpdcs.skyeye.knight.maplist',
                              {'metas': {'teamid': int(team_id)},
                               'params': {'knightMapListRequest': {'teamId': int(team_id)}}},
                              headers={'x-city-id': '3', 'x-shard': f'teamid={team_id}'},
                              timeout=20000)
        if not r or r.startswith('ERR:'):
            return None
        d = json.loads(r)
        kl = ((d.get('result') or {}).get('data') or {}).get('knightList') or []
        return len(kl) if kl else None
    except Exception:
        return None

def read_weather(xlsx_path):
    """从运单文件读天气信息（对齐日报口径）→ (weather_info, weather_level)"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        def find_col(targets, blacklist=None):
            for t in targets:
                for i, h in enumerate(headers):
                    if h and t in str(h) and not (blacklist and any(b in str(h) for b in blacklist)):
                        return i + 1
            return None
        lv_col = find_col(['天气等级']) or 40
        w_col = find_col(['天气'], blacklist=['等级']) or 41
        weather_map = {'正常': 0, '轻微恶劣': 1, '恶劣': 2, '极恶劣': 3, '罕见恶劣': 4}
        lv_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        total = 0
        weather_info = '未知'
        for r in range(2, ws.max_row + 1):
            lv_raw = ws.cell(r, lv_col).value
            w_raw = ws.cell(r, w_col).value
            total += 1
            if lv_raw and str(lv_raw).strip() in weather_map:
                lv_counts[weather_map[str(lv_raw).strip()]] += 1
            if r == 2 and w_raw:
                weather_info = str(w_raw).strip()
        wb.close()
        if total == 0:
            return weather_info, '正常天'
        lv1234 = lv_counts[1] + lv_counts[2] + lv_counts[3] + lv_counts[4]
        lv234 = lv_counts[2] + lv_counts[3] + lv_counts[4]
        lv34 = lv_counts[3] + lv_counts[4]
        # 2026-11 口径（方案第七条 场景分层）：6 天气分层 × 2 日期场景
        p34 = lv34 / total * 100
        if lv1234 / total * 100 <= 10:
            level = '正常天'
        elif lv234 / total * 100 <= 10:
            level = '轻微恶天'
        elif p34 <= 20:
            level = '一般恶天'
        elif p34 <= 30:
            level = '严重恶天1'
        elif p34 <= 40:
            level = '严重恶天2'
        else:
            level = '严重恶天3'
        return weather_info, level
    except Exception:
        return '未知', '正常天'

# ── 10月晚复盘「运单明细」派生列口径（2026-10-02 对表抄录，见 dispatcher-night-review 技能）──
# 订单判定(表 G列) = 欺诈/逆向 → '不考核'，否则按配送标品查 指标!A22:B37 得 即时单1/即时单2/不考核
BIAOPIN_CAT = {
    '配送标品': '即时单2', '新零售专送': '即时单2', '全城送': '即时单2', '饿了么-蜂鸟专送': '即时单2',
    '饿了么-蜂鸟快送': '即时单2', '星专送': '即时单1', '淘宝专送': '即时单1', '淘宝全城送': '即时单1',
    '随时下线的标品': '即时单1', '跑腿代购-帮送': '即时单2', '开放平台-优送': '不考核',
    '开放平台-快送-一档': '即时单1', '开放平台-即时送-组织': '即时单1', '开放平台-即时送-快送': '即时单1',
    '蜂鸟商家-组织运力': '即时单1', '蜂鸟商家-众包': '即时单1',
}


def parse_punctual(xlsx_path, site_name=None):
    """运单 → (每骑手当天准时率, 站点级 KPI 原始计数)。
    rider: {骑手id: {'name','valid','over','ot'}}（准时率页，超时=送达比平台期望晚>8分钟）
    site_kpi: 对齐 10月晚复盘 运单明细派生列 ——
      c6=有效完成单(表 C6 = 成功 - 成功&订单判定不考核)、orders=接单数(成功+失败)、
      fake_prep=虚假报备商户出餐慢未完成单(表 E6)、ontime=准时单(表 C14, I=1 或 J=1)、
      highlate=高笔单非准时(表 D14)、ot_all=Σ复合超时时长(表 C12)、noexam_ids/high_ids(运单id 集合，
      供违规数据过滤「是否考核」与标记「高笔单」用)"""
    try:
        import level_analysis_v2 as lv_mod  # noqa: F401 复用同目录解析惯例
    except ImportError:
        pass
    import openpyxl
    try:
        from modules import t1_overtime as t1
    except ImportError:
        import t1_overtime as t1
    # 73列标准列号硬映射（对齐 t1_overtime.COL；1-based → 0-based 取值用 -1）
    COL = {
        'rid': 9, 'name': 10, 'status': 15, 'biaopin': 16,
        'arrive': 29, 'expect': 34, 'erhu': 73,
    }
    C_SITE, C_TYPE, C_FINISH, C_CHECKDATE = 4, 14, 30, 32      # 站点名称/运单类型/运单完成时间/骑手考核时间
    C_FRAUD, C_REVERSE, C_FAKE, C_FAKEITEM = 42, 46, 55, 56    # 欺诈/逆向/虚假报备是否成立/虚假报备项
    C_HIGH, C_ORDERID = 72, 7                                  # 是否高笔单 / 运单id
    rider = {}
    site_kpi = {'succ': 0, 'fail': 0, 'c6': 0, 'orders': 0, 'fake_prep': 0, 'ontime': 0,
                'highlate': 0, 'ot_all': 0.0, 'noexam_ids': [], 'high_ids': []}
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    # ⚠️ 不能用 read_only：导出文件的 dimension 缺失 → read_only 下 iter_rows 只吐 1 个单元格（2026-10-02 实测）
    ws = next((s for s in wb.worksheets
               if (s.cell(1, 1).value or '') != '' and str(s.cell(1, 1).value).strip() == '商id'),
              wb.worksheets[0])
    all_rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r and r[COL['rid'] - 1]]
    wb.close()
    # 站点过滤（导出通常单站点；名称不一致时不过滤，保持历史行为）
    rows = all_rows
    if site_name:
        matched = [r for r in all_rows if len(r) >= C_SITE and str(r[C_SITE - 1] or '').strip() == site_name]
        rows = matched or all_rows

    def g(row, name):
        i = COL[name] - 1
        return row[i] if len(row) > i else None

    for row in rows:
        rid = g(row, 'rid')
        if not rid:
            continue
        status = str(g(row, 'status') or '')
        name = str(g(row, 'name') or '')
        biaopin = str(g(row, 'biaopin') or '')
        erhu = str(g(row, 'erhu') or '')
        arrive = t1.parse_ts(g(row, 'arrive'))
        expect = t1.parse_ts(g(row, 'expect'))
        x = (arrive - expect).total_seconds() if arrive and expect else 0
        comp = t1.comp_overtime_sec(x) if x > 0 else 0
        # ── 每骑手准时率（页面「准时率」页，口径不变：剔兜底/二呼） ──
        if status == '配送成功' and '兜底' not in biaopin and erhu != '是':
            d = rider.setdefault(str(rid), {'name': name, 'valid': 0, 'over': 0, 'ot': 0})
            d['valid'] += 1
            if comp > 0:
                d['over'] += 1
            d['ot'] += comp
        # ── 站点级 KPI 计数（严格照 10月晚复盘 运单明细 A~L 派生列公式） ──
        fraud = str(row[C_FRAUD - 1] or '') if len(row) >= C_FRAUD else ''
        reverse = str(row[C_REVERSE - 1] or '') if len(row) >= C_REVERSE else ''
        judge = '不考核' if (fraud == '是' or reverse == '是') else BIAOPIN_CAT.get(biaopin, '')
        order_id = str(row[C_ORDERID - 1] or '') if len(row) >= C_ORDERID else ''
        if judge == '不考核' and order_id:
            site_kpi['noexam_ids'].append(order_id)
        if str(row[C_HIGH - 1] or '') == '是' and order_id:
            site_kpi['high_ids'].append(order_id)
        if status == '配送成功':
            site_kpi['succ'] += 1
            if judge != '不考核':
                site_kpi['c6'] += 1
        elif status == '配送失败':
            site_kpi['fail'] += 1
        site_kpi['orders'] = site_kpi['succ'] + site_kpi['fail']
        # 虚假报备商户出餐慢未完成单（表 E6 = 配送失败 & 虚假报备成立 & 报备项含商户出餐慢 - 不考核）
        if (status == '配送失败' and judge != '不考核'
                and str(row[C_FAKE - 1] or '') == '是' and '出餐慢' in str(row[C_FAKEITEM - 1] or '')):
            site_kpi['fake_prep'] += 1
        # 复合超时时长（表 F 列 = 不考核 0 / 失败空 / 其余按 K 分档） + 准时标志（表 I/J 列）
        if status == '配送成功' and judge != '不考核' and x > 0:
            site_kpi['ot_all'] += comp
        if status == '配送成功' and judge != '不考核':
            wtype = str(row[C_TYPE - 1] or '')
            ontime = None
            if wtype == '预订单':
                ck = str(row[C_CHECKDATE - 1] or '') if len(row) >= C_CHECKDATE else ''
                ck_ts = t1.parse_ts(ck[-19:]) if ck else None
                if ck_ts and arrive:
                    ontime = 1 if arrive <= ck_ts else 0
            elif judge in ('即时单1', '即时单2'):
                ontime = 1 if ((x <= 480) if judge == '即时单2' else (x <= 0)) else 0
            if ontime == 1:
                site_kpi['ontime'] += 1
            # 高笔单非准时（表 D14 = (I=0 或 J=0) & 高笔单 & 配送成功）
            if ontime == 0 and str(row[C_HIGH - 1] or '') == '是':
                site_kpi['highlate'] += 1
    return rider, site_kpi

def submit_today_export(cdp, team_id, today):
    """主动提交当天运单导出任务（exportTracking，当天0点~现在）。只提交不等待，后续刷新自动拾取"""
    import datetime as _dt
    now = _dt.datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    body = {'params': {'request': {
        'trackingFinishStart': int(day0.timestamp() * 1000),
        'trackingFinishEnd': int(time.time() * 1000),
        'merchantConsumerDistanceStart': '0', 'merchantConsumerDistanceEnd': '999999',
        'payAmountStart': '0', 'payAmountEnd': '999999',
        'weightStart': '0', 'weightEnd': '999999',
        'trackingFinishTimePeriodList': ['0-86400'],
        'teamIds': [int(team_id)]}}}
    try:
        r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.exportTracking', body, ver='2.0')
        d = json.loads(r)
        if (d.get('result') or {}).get('code') == 'NO_TEAM_ID_PERMISSIONS':
            print('  ⚠️ 当前账号无导出权限')
            return False
        return True
    except Exception:
        return False


def fetch_order_stats(cdp, team_id, today, cache_path=None, max_age=120, site_name=None):
    """运单改派/无效/准时率统计（复用今日导出任务，2分钟缓存；任务超缓存周期自动重提）。
    返回 (combined, transfer_only, weather, punctual, site_kpi)：combined=改派+无效总扣减，
    transfer_only=仅改派，weather=(天气, 天气等级)，punctual=每骑手准时率统计，
    site_kpi=站点级 KPI 原始计数（10月晚复盘运单明细派生列口径，2026-10-02 加）
    （2026-09-05 实测：提交→就绪 2.3s，缓存 300s→120s；与订单识别共享 _last_export_submit 全局锁，
    提交间隔 ≥60s 防 Aeolus「请求太频繁」限流——90s 双模块曾实测触发）"""
    global _last_export_submit   # 函数内赋值 → 必须 global，否则 UnboundLocalError（2026-09-05 实测）
    try:
        import level_analysis_v2 as lv_mod
    except ImportError:
        try:
            from modules import level_analysis_v2 as lv_mod
        except ImportError:
            return None, None, None, None, None
    now = time.time()
    stale = None
    if cache_path and cache_path.exists():
        try:
            d = json.loads(cache_path.read_text(encoding='utf-8'))
            if d.get('combined') and d.get('v') == 3:  # v3: punctual 含 ot 秒数 + site_kpi（KPI原始计数）
                got = (d['combined'], d.get('transfer_only', {}), d.get('weather'),
                       d.get('punctual'), d.get('site_kpi'))
                if (now - cache_path.stat().st_mtime) < max_age:
                    return got
                stale = got   # 过期但可用：取不到新任务时兜底（改派/无效不能凭空消失）
        except Exception:
            pass
    day_start_ms = int(time.mktime(time.strptime(today, '%Y-%m-%d')) * 1000)
    cache_dir = cache_path.parent if cache_path else Path.home() / 'Downloads'
    try:
        r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryExportTask',
                              {'params': {'request': {'current': 1, 'pageSize': 20}}}, '2.0')
        tasks = json.loads(r).get('result', {}).get('data', {}).get('data', [])
        done = sorted([t for t in tasks if t.get('status') == 1],
                      key=lambda t: t.get('exportTime', 0), reverse=True)
        sp_ex = _special_cache_tid(cache_dir, team_id)   # 排除订单识别消费过的任务（维度不同，互斥）
        t_cur = next((t for t in done if t.get('exportTime', 0) >= day_start_ms and t.get('id') != sp_ex), None)
        tid = t_cur['id'] if t_cur else None
        t_exp = (t_cur or {}).get('exportTime', 0) or 0
        # 今天无任务 或 任务超过缓存周期 → 主动提交新任务（同周期节流+全局提交锁，不阻塞；下轮刷新自动拾取）
        if not tid or (now * 1000 - t_exp > max_age * 1000):
            _last = cache_path.stat().st_mtime if (cache_path and cache_path.exists()) else 0
            _last = max(_last, _order_submit_ts.get(team_id, 0))   # 失败也计入节流，防提交风暴
            if now - _last > max_age and now - _last_export_submit > 60:
                _order_submit_ts[team_id] = now   # 先记录再提交，成败都算一次尝试
                _last_export_submit = now         # 全局锁：另一模块(订单识别)60s 内不再提交
                if submit_today_export(cdp, team_id, today):
                    print('📤 运单导出任务已超' + str(int(max_age)) + '秒，重新提交（下轮刷新获取最新数据）')
                else:
                    print('⚠️ 运单导出任务提交失败，准时率/改派暂缺')
            # 2026-11-06 修：导出任务生成延迟约 65s，与 120s 阈值错配会让缓存永久为空
            # （每轮都判"超时"→ 只提交不下载 → 改派/无效一直缺）。有旧任务就继续用它下载，
            # 新任务下轮自动生效；实在没有才回兜底缓存。
            if not tid:
                return stale if stale else (None, None, None, None, None)
        r2 = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryTaskUrl',
                               {'params': {'request': {'taskId': tid}}}, '2.0')
        oss = json.loads(r2).get('result', {}).get('data', '')
        if not oss:
            return stale if stale else (None, None, None, None, None)
        oss = oss.replace('http://', 'https://')
        out_dir = cache_path.parent if cache_path else Path.home() / 'Downloads'
        out = out_dir / 'live_运单.xlsx'
        subprocess.run(['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', oss, '-o', str(out)],
                       capture_output=True, timeout=180, **cdp_core._no_window_kw())
        if not out.exists() or out.stat().st_size < 10000:
            return stale if stale else (None, None, None, None, None)
        combined, transfer_only, tdetail = lv_mod.read_transfer_data(str(out_dir), with_detail=True)
        weather = read_weather(str(out))
        punctual, site_kpi = parse_punctual(str(out), site_name=site_name)
        if cache_path:
            cache_path.write_text(json.dumps({'v': 3, 'combined': combined, 'transfer_only': transfer_only,
                                              'weather': weather, 'punctual': punctual, 'tid': tid,
                                              'site_kpi': site_kpi,
                                              # 2026-09-27 加：改派/无效单明细（工作台「档位查询」用；同名骑手才几条）
                                              'transfer_detail': tdetail},
                                             ensure_ascii=False), encoding='utf-8')
        try:
            out.unlink()
        except Exception:
            pass
        return combined, transfer_only, weather, punctual, site_kpi
    except Exception:
        return stale if stale else (None, None, None, None, None)


# ═══ 订单识别（2026-09-03 加入；识别三种特殊订单：蜂鸟商家-众包 / 全城送 / 开放平台-快送-一档）═══

SPECIAL_PRODUCTS = ('蜂鸟商家-众包', '全城送', '开放平台-快送-一档')

_special_submit_ts = {}   # team_id -> 上次提交尝试时间戳（成功失败都记，防提交风暴）
_order_submit_ts = {}     # 准时率/运单模块同款防抖（2026-09-05：与 special 一起修，失败不风暴）
_last_export_submit = 0.0 # 全局 exportTracking 提交锁：order+special 共用（2026-09-05 实测 90s 双模块提交触发 Aeolus「请求太频繁」限流，提交间隔统一 ≥120s 并错开 ≥60s）

def _order_cache_tid(cache_dir, tid):
    """读对方（准时率/订单识别）缓存里最后用过的导出任务 id，用于两套任务互斥。"""
    try:
        p = cache_dir / f'live_order_{tid}.json'
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            return d.get('tid')
    except Exception:
        pass
    return None

def _special_cache_tid(cache_dir, tid):
    try:
        p = cache_dir / f'live_special_{tid}.json'
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            return d.get('tid')
    except Exception:
        pass
    return None


def fetch_special_orders(cdp, team_id, today, cache_path=None, max_age=120):
    """订单识别：当天按【运单生成时间】导出的运单里筛三种特殊配送标品。

    返回 rows 列表或 None。rows 元素 = {knight骑手名称, merchant平台商家名称,
    serial商家订单流水号, otype运单类型, ostate运单状态, product配送标品,
    source订单来源}。2 分钟缓存；与准时率 fetch_order_stats 通过任务 id 互斥，
    各自只消费自己提交的任务（导出任务列表无维度字段，不互斥会互相拿错维度）。"""
    global _last_export_submit   # 函数内赋值 → 必须 global，否则 UnboundLocalError（2026-09-05 实测）
    try:
        import openpyxl
    except Exception:
        return None
    now = time.time()
    if cache_path and cache_path.exists() and (now - cache_path.stat().st_mtime) < max_age:
        try:
            d = json.loads(cache_path.read_text(encoding='utf-8'))
            if d.get('v') == 1 and 'rows' in d:
                return d['rows']
        except Exception:
            pass
    day_start_ms = int(time.mktime(time.strptime(today, '%Y-%m-%d')) * 1000)
    cache_dir = cache_path.parent if cache_path else Path.home() / 'Downloads'
    # 旧缓存（过期但可用）：数据源 tid 与之一致时直接复用，省掉整包下载（导出任务是快照，同 tid 内容不变）
    stale_rows, stale_tid = None, None
    if cache_path and cache_path.exists():
        try:
            _d = json.loads(cache_path.read_text(encoding='utf-8'))
            if _d.get('rows') is not None:
                stale_rows, stale_tid = _d['rows'], _d.get('tid')
        except Exception:
            pass
    try:
        r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryExportTask',
                              {'params': {'request': {'current': 1, 'pageSize': 20}}}, '2.0')
        tasks = json.loads(r).get('result', {}).get('data', {}).get('data', [])
        done = sorted([t for t in tasks if t.get('status') == 1],
                      key=lambda t: t.get('exportTime', 0), reverse=True)
        exclude = _order_cache_tid(cache_dir, team_id)   # 排除准时率任务消费过的任务
        cands = [t for t in done if t.get('exportTime', 0) >= day_start_ms and t.get('id') != exclude]
        t_cur = cands[0] if cands else None
        tid = t_cur['id'] if t_cur else None
        t_exp = (t_cur or {}).get('exportTime', 0) or 0
        # 数据不够新（任务太旧或没有）→ 提交新任务。不当场死等（导出生成实测 2~70s）：
        # 短轮询 12s 拿到新任务就用，拿不到本轮仍用旧任务出数，下轮自动切新 —— 保证每轮都有数、不空窗
        if not tid or (now * 1000 - t_exp > max_age * 1000):
            _last = cache_path.stat().st_mtime if (cache_path and cache_path.exists()) else 0
            _last = max(_last, _special_submit_ts.get(team_id, 0))   # 失败也计入节流，防提交风暴
            if now - _last > max_age and now - _last_export_submit > 30:   # 全局锁（与准时率错开，2026-11-10 60s 刷新：30s 起放行）
                _special_submit_ts[team_id] = now   # 先记录再提交，无论成败都算一次尝试
                _last_export_submit = now
                if _submit_special_export(cdp, team_id, today, day_start_ms):
                    print('📤 订单识别：生成时间维度导出任务已提交')
                    for _ in range(6):
                        time.sleep(2)
                        try:
                            rr = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryExportTask',
                                                   {'params': {'request': {'current': 1, 'pageSize': 20}}}, '2.0')
                            tl = json.loads(rr).get('result', {}).get('data', {}).get('data', [])
                            cand2 = sorted([t for t in tl if t.get('status') == 1 and t.get('id') != exclude
                                            and t.get('exportTime', 0) >= day_start_ms],
                                           key=lambda t: t.get('exportTime', 0), reverse=True)
                            if cand2 and cand2[0]['id'] != tid:
                                tid = cand2[0]['id']
                                break
                        except Exception:
                            break
                else:
                    print('⚠️ 订单识别：导出任务提交失败（' + str(int(max_age)) + '秒内不重试）')
        if not tid:
            return None
        # 无新任务 → 复用缓存行，不重复下载 900KB + 解析
        if tid == stale_tid and stale_rows is not None:
            return stale_rows
        r2 = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.agencyTrackingFs.queryTaskUrl',
                               {'params': {'request': {'taskId': tid}}}, '2.0')
        oss = json.loads(r2).get('result', {}).get('data', '')
        if not oss:
            return None
        oss = oss.replace('http://', 'https://')
        out = cache_dir / f'live_special_{team_id}.xlsx'
        try:
            subprocess.run(['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', oss, '-o', str(out)],
                           capture_output=True, timeout=180, **cdp_core._no_window_kw())
            if not out.exists() or out.stat().st_size < 10000:
                return None
            rows = _parse_special_xlsx(str(out))
        finally:
            try:
                out.unlink()   # 成功失败都清理，防 data/ 目录残留半成品
            except Exception:
                pass
        if rows is None:
            return stale_rows if stale_rows is not None else None
        # 在途单保护（2026-11-10）：导出快照是按"提交时刻"生成的，在途单（待到店/待取餐/待送达）
        # 会在相邻快照之间忽有忽无（改派瞬间/生成截断），页面就"一闪就没了"。
        # 规则：新快照里缺的在途单沿用上一份缓存，直到它出现终态或超 6 小时。
        _INWAY = ('待到店', '待取餐', '待送达')
        _key = lambda x: (x.get('serial'), x.get('merchant'))
        if stale_rows:
            _now = time.time()
            _seen = {_key(r): r.get('_seen') or _now for r in stale_rows}
            have = {_key(r) for r in rows}
            for r in stale_rows:
                st = str(r.get('ostate') or '').strip()
                if st in _INWAY and _key(r) not in have:
                    if _now - (_seen.get(_key(r)) or _now) < 6 * 3600:
                        rr = dict(r)
                        rr['_seen'] = _seen.get(_key(r), _now)
                        rows.append(rr)
        _now2 = time.time()
        for r in rows:
            r.setdefault('_seen', _now2)
        if cache_path:
            cache_path.write_text(json.dumps({'v': 1, 'rows': rows, 'tid': tid}, ensure_ascii=False),
                                  encoding='utf-8')
        return rows
    except Exception as e:
        print(f'⚠️ 订单识别拉取异常: {e}')
        return stale_rows if stale_rows is not None else None


def _submit_special_export(cdp, team_id, today, day_start_ms):
    """提交当天【运单生成时间】维度的导出任务（运单数据(新)页面口径）。"""
    import datetime as _dt
    now = _dt.datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    body = {'params': {'request': {
        'trackingCreatedStart': int(day0.timestamp() * 1000),
        'trackingCreatedEnd': int(time.time() * 1000),
        'merchantConsumerDistanceStart': '0', 'merchantConsumerDistanceEnd': '999999',
        'payAmountStart': '0', 'payAmountEnd': '999999',
        'weightStart': '0', 'weightEnd': '999999',
        'trackingCreatedTimePeriodList': ['0-86400'],
        'teamIds': [int(team_id)]}}}
    try:
        r = cdp_core.call_api(cdp, 'xtop.merchant.agencyTrackingFs.exportTracking', body, ver='2.0')
        d = json.loads(r)
        if (d.get('result') or {}).get('code') == 'NO_TEAM_ID_PERMISSIONS':
            print('  ⚠️ 当前账号无导出权限')
            return False
        msg = (d.get('result') or {}).get('msg') or ''
        if msg:
            print(f'  ⚠️ 导出任务提交被拒: {msg}')
            return False
        return True
    except Exception:
        return False


def _parse_special_xlsx(path):
    """73 列运单 xlsx → 筛三种特殊标品行。列号硬映射（表头名会变）：
    J(10)骑手名称 L(12)平台商家名称 M(13)商家订单流水号 N(14)运单类型
    O(15)运单状态 P(16)配送标品 Q(17)订单来源。
    ⚠️ 运单 xlsx 有 dimension 声明坑，read_only 只读到 1 列 → 用普通模式。"""
    try:
        import openpyxl
    except Exception:
        return None
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = []
        for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True)):
            if not r or len(r) < 17:
                continue
            product = str(r[15]).strip() if r[15] is not None else ''
            if product not in SPECIAL_PRODUCTS:
                continue
            def g(idx):
                v = r[idx] if idx < len(r) else None
                return str(v).strip() if v is not None else ''
            rows.append({
                'knight': g(9), 'merchant': g(11), 'serial': g(12),
                'otype': g(13), 'ostate': g(14), 'product': product,
                'source': g(16),
            })
        wb.close()
        return rows
    except Exception as e:
        print(f'⚠️ 订单识别解析失败: {e}')
        return None


# ═══ 档位速查（单人实时查询，2026-08-27 加入；运单数据与看板同源 fetch_order_stats，2026-08-29 改）═══

def fetch_live_workstatus(cdp, team_id):
    """实时工作状态：listCustomTime 取当前 timeCode → listKnightAttendance 拿全骑手
    workStatus(1-5)，按姓名建映射。失败返回 {}（回退出勤表状态）。"""
    try:
        r = cdp_core.call_api(cdp, 'xtop.fs.realTimeAttendance.listCustomTime',
                              {'params': {'request': {}}})
        d = json.loads(r)
        items = (d.get('result') or {}).get('data') or []
        cur = next((it for it in items if it.get('current')), None)
        if not cur:
            return {}
        r2 = cdp_core.call_api(cdp, 'xtop.fs.realTimeAttendance.listKnightAttendance',
                               {'params': {'request': {
                                   'objectIds': [{'objectType': 9, 'objectId': int(team_id)}],
                                   'objectType': 9, 'isCurrentTime': True,
                                   'timeCode': cur['timeCode'],
                                   'pageNo': 1, 'pageSize': 1000}}})
        d2 = json.loads(r2)
        riders = ((d2.get('result') or {}).get('data') or {}).get('knightDetailVos') or []
        return {r.get('knightName'): r.get('workStatus') for r in riders
                if r.get('knightName') and r.get('workStatus')}
    except Exception:
        return {}


def level_row_for(nm, rv, combined, transfer_only, periods, now_h, LVS):
    """单骑手档位行（与 build_board 档位板块同口径，2026-08-27 提取供档位速查用；
    若改 build_board 档位计算逻辑需同步这里）。"""
    BH, BO = LVS[0]['hours'], LVS[0]['orders']
    eff = max(0, int(rv['day_orders']) - int(combined.get(nm, 0) or 0))
    transfer = int(transfer_only.get(nm, 0) or 0)
    invalid = int(combined.get(nm, 0) or 0) - transfer
    lv = calc_level(rv['day_hours'], eff, LVS)
    # 晋升标准文字（下一档要求；已满档=已满档；未达标=标1要求）
    if lv >= 5:
        std_txt = '已满档'
    else:
        _t = LVS[lv] if lv > 0 else {'level': 1, 'orders': BO, 'hours': BH}
        std_txt = f"{_t['orders']}单/{_t['hours']}h"
    ls = rv.get('last_shift')
    shift_gap_txt = '—'
    if ls:
        srow = next((p for p in periods if p['name'] == nm and p['period'] == ls), None)
        if srow:
            ls_std = SHIFT_MAP.get(ls)
            if ls_std and ls_std['start'] < now_h:
                # 班次达成 = 完单+时长双达标（取低原则，2026-08-27 用户要求）
                sg = max(0, int(srow.get('req_orders') or 0) - int(srow.get('orders') or 0))
                sh = max(0, round(float(srow.get('req_hours') or 0) - float(srow.get('hours') or 0), 2))
                if sg == 0 and sh <= 0:
                    shift_gap_txt = '✓'
                elif sg > 0 and sh > 0:
                    shift_gap_txt = f"差{sg}单+{fmt_hours(sh)}"
                elif sg > 0:
                    shift_gap_txt = f"差{sg}单"
                else:
                    shift_gap_txt = f"差{fmt_hours(sh)}"
    if lv >= 5:
        tag, tcls, next_lv, gap_o, gap_t = '已满档', 'pass', '-', 0, 0
    else:
        tgt = LVS[lv] if lv > 0 else {'level': 1, 'orders': BO, 'hours': BH}
        next_lv = '标' + str(tgt['level'])
        gap_o = max(0, tgt['orders'] - eff)
        gap_t = max(0, tgt['hours'] - rv['day_hours'])
        if lv == 0 and gap_o >= 6:
            tag, tcls = '未达标', 'fail'
        elif gap_o == 0:
            tag, tcls = f'冲{next_lv}·差{fmt_hours(gap_t)}', 'pass'
        elif gap_o <= 2:
            tag, tcls = f'冲{next_lv}·差{gap_o}单', 'pass'
        elif gap_o <= 5:
            tag, tcls = f'升{next_lv}·差{gap_o}单', 'warn'
        else:
            tag, tcls = f'{next_lv}·差{gap_o}单', 'norm'
    return {
        'name': nm, 'work': rv['work'], 'hours': rv['day_hours'],
        'online_hours': rv.get('day_online_hours') or rv['day_hours'],
        'invalid_hours': round(max(0.0, (rv.get('day_online_hours') or rv['day_hours']) - rv['day_hours']), 4),
        'orders': eff, 'raw_orders': int(rv['day_orders']),
        'delivering': rv['delivering'],
        'transfer': transfer, 'invalid': invalid,
        'level': lv,
        'level_label': ('标' + str(lv)) if lv else '未达标',
        'std_txt': std_txt,
        'next': next_lv, 'tag': tag, 'tcls': tcls,
        'gap_o': gap_o, 'gap_t': gap_t,
        'shift_gap': shift_gap_txt,
        'last_shift': ls or '未排班',   # 2026-09-27 用户定稿：无排班记录显示“未排班”，不用笼统的“其他”
    }


# ═══ 服务质量KPI（RiderMonitor 口径，2026-08-26 移植）═══
# 违规类型白名单（违规分类 → 允许计入的违规类型），照抄 RiderMonitor parseAppealCSV
APPEAL_CATEGORY_TYPES = {
    '餐货损': ['提前点送达', '未保持餐品完整', '诱导用户退单', '少餐错餐', '送错位置', '丢餐', '虚假配送',
              '服务态度恶劣', '虚假拒收', '虚假签收', '虚假标记异常', '配送错误', '配送破损', '额外索取费用',
              '联系用户取消', '配送丢失少件'],
    '差评': ['骑士送错餐', '餐品翻洒', '提前点送达', '不送上门', '送达不通知', '服务态度差', '少餐错餐'],
    '投诉': ['提前点送达', '少餐错餐', '服务态度差', '未保持餐品完整', '诱导顾客取消订单', '威胁辱骂', '虚假配送'],
    '取消': ['违规取消'],
}
# 8场景动态目标值（周中/周末 × 4天气等级），照抄 RiderMonitor KPI_TARGETS
KPI_TARGETS = {
    '周中正常天': {'delivery': {'t0': 0.99823, 't60': 0.99959, 't100': 0.99990},
                  't8': {'t0': 0.8390, 't60': 0.9695, 't100': 0.9806},
                  'complaint': {'t0': 0.003092, 't60': 0.000956, 't100': 0.000633},
                  'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周中轻微恶天': {'delivery': {'t0': 0.99822, 't60': 0.99958, 't100': 0.99990},
                    't8': {'t0': 0.8307, 't60': 0.9600, 't100': 0.9709},
                    'complaint': {'t0': 0.003385, 't60': 0.001046, 't100': 0.000693},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周中一般恶天': {'delivery': {'t0': 0.99802, 't60': 0.99938, 't100': 0.99990},
                    't8': {'t0': 0.8232, 't60': 0.9513, 't100': 0.9622},
                    'complaint': {'t0': 0.003998, 't60': 0.001236, 't100': 0.000818},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周中严重恶天': {'delivery': {'t0': 0.99419, 't60': 0.99555, 't100': 0.99618},
                    't8': {'t0': 0.8068, 't60': 0.9323, 't100': 0.9430},
                    'complaint': {'t0': 0.004771, 't60': 0.001475, 't100': 0.000976},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周末正常天': {'delivery': {'t0': 0.99818, 't60': 0.99954, 't100': 0.99990},
                  't8': {'t0': 0.8331, 't60': 0.9628, 't100': 0.9738},
                  'complaint': {'t0': 0.003145, 't60': 0.000972, 't100': 0.000644},
                  'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周末轻微恶天': {'delivery': {'t0': 0.99814, 't60': 0.99950, 't100': 0.99990},
                    't8': {'t0': 0.8260, 't60': 0.9545, 't100': 0.9655},
                    'complaint': {'t0': 0.003492, 't60': 0.001079, 't100': 0.000715},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周末一般恶天': {'delivery': {'t0': 0.99684, 't60': 0.99820, 't100': 0.99883},
                    't8': {'t0': 0.8177, 't60': 0.9450, 't100': 0.9558},
                    'complaint': {'t0': 0.004078, 't60': 0.001260, 't100': 0.000834},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
    '周末严重恶天': {'delivery': {'t0': 0.99419, 't60': 0.99555, 't100': 0.99618},
                    't8': {'t0': 0.8068, 't60': 0.9323, 't100': 0.9430},
                    'complaint': {'t0': 0.005251, 't60': 0.001623, 't100': 0.001074},
                    'overtime': {'t0': 30, 't60': 17, 't100': 8}},
}
KPI_DEFAULT_TARGETS = KPI_TARGETS['周中正常天']
KPI_WEIGHTS = {'t8': 0.30, 'complaint': 0.20, 'delivery': 0.20, 'overtime': 0.15}
# 内置默认快照（恢复默认用；深拷贝防污染）
KPI_TARGETS_DEFAULT = {sc: {g: dict(v2) for g, v2 in v.items()}
                       for sc, v in KPI_TARGETS.items()}


def _kpi_targets_file():
    return cdp_core.data_dir() / 'kpi_targets.json'


def _load_kpi_targets():
    """启动时从 data/kpi_targets.json 加载用户自定义 KPI 目标值（分发版使用者自己维护）。
    文件不存在 = 用内置默认（RiderMonitor 8场景）。运行时修改直接覆盖全局 KPI_TARGETS。"""
    global KPI_TARGETS
    try:
        f = _kpi_targets_file()
        if f.exists():
            d = json.loads(f.read_text(encoding='utf-8'))
            if isinstance(d, dict) and d:
                _grps = ('delivery', 't8', 'complaint', 'overtime')
                merged = {}
                for sc, v in d.items():
                    if not isinstance(v, dict):
                        continue
                    nt = {}
                    for g in _grps:
                        tg = v.get(g)
                        if isinstance(tg, dict) and all(isinstance(tg.get(k), (int, float)) for k in ('t0', 't60', 't100')):
                            nt[g] = {'t0': float(tg['t0']), 't60': float(tg['t60']), 't100': float(tg['t100'])}
                        else:
                            nt[g] = KPI_DEFAULT_TARGETS[g]
                    merged[sc] = nt
                if merged:
                    KPI_TARGETS = merged
                    print(f'⚙ 已加载自定义KPI目标值：{len(merged)}个场景（{f.name}）')
    except Exception:
        pass


def _save_kpi_targets(targets):
    """保存用户自定义 KPI 目标值 → data/kpi_targets.json，并更新内存全局"""
    global KPI_TARGETS
    _grps = ('delivery', 't8', 'complaint', 'overtime')
    clean = {}
    for sc, v in targets.items():
        if not isinstance(v, dict):
            continue
        nt = {}
        for g in _grps:
            tg = v.get(g)
            if isinstance(tg, dict) and all(isinstance(tg.get(k), (int, float)) for k in ('t0', 't60', 't100')):
                nt[g] = {'t0': float(tg['t0']), 't60': float(tg['t60']), 't100': float(tg['t100'])}
        if len(nt) == 4:
            clean[sc] = nt
    if not clean:
        raise ValueError('目标值格式不正确')
    f = _kpi_targets_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding='utf-8')
    KPI_TARGETS = clean
    return len(clean)


def _reset_kpi_targets():
    """恢复内置默认：删除配置文件并重置内存"""
    global KPI_TARGETS
    KPI_TARGETS = {sc: {g: dict(v2) for g, v2 in v.items()}
                   for sc, v in KPI_TARGETS_DEFAULT.items()}
    try:
        f = _kpi_targets_file()
        if f.exists():
            f.unlink()
    except Exception:
        pass


def _parse_kpi_excel(path):
    """解析 KPI 目标值 Excel（照 RiderMonitor upload_kpi_targets 口径）：
    表头含 场景/分层 列 + 4指标（物流妥投/考核准时率/不满意率/复合超时时长）× 0分/60分/100分 列。
    值直接填小数（0.99823=99.823%）；复合超时填秒。返回 {场景: {指标: {t0,t60,t100}}}"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    headers = [str(c.value or '').strip() for c in ws[1]]

    def colidx(keys):
        for i, h in enumerate(headers):
            if all(k in h for k in keys):
                return i
        return None

    sc_idx = colidx(('场景',)) if colidx(('场景',)) is not None else colidx(('分层',))
    if sc_idx is None:
        wb.close()
        raise ValueError('未找到「场景分层」列（表头需含"场景"或"分层"）')
    groups = {}
    for g, keys in (('delivery', ('物流', '妥投')), ('t8', ('准时',)),
                    ('complaint', ('不满意',)), ('overtime', ('超时',))):
        tg = {}
        for k, kk in (('t0', '0'), ('t60', '60'), ('t100', '100')):
            idx = None
            for i, h in enumerate(headers):
                if all(x in h for x in keys + (kk,)):
                    idx = i
                    break
            if idx is not None:
                tg[k] = idx
        groups[g] = tg
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or sc_idx >= len(row):
            continue
        sc = str(row[sc_idx] or '').strip()
        if not sc:
            continue
        nt = {}
        for g, tg in groups.items():
            vals = {}
            for k, idx in tg.items():
                if idx is not None and idx < len(row) and row[idx] is not None:
                    try:
                        vals[k] = float(row[idx])
                    except (TypeError, ValueError):
                        pass
            if len(vals) == 3:
                nt[g] = vals
        if len(nt) == 4:
            out[sc] = nt
    wb.close()
    if not out:
        raise ValueError('Excel 未解析到任何有效场景行（第一列需为场景分层名）')
    return out


def _make_kpi_template():
    """生成 KPI 目标值 Excel 模板（当前目标值预填，13列 × 8场景）→ bytes"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'KPI目标值'
    headers = ['场景分层', '物流妥投0分', '物流妥投60分', '物流妥投100分',
               '考核准时率0分', '考核准时率60分', '考核准时率100分',
               '不满意率0分', '不满意率60分', '不满意率100分',
               '复合超时0分', '复合超时60分', '复合超时100分']
    ws.append(headers)
    for sc in (KPI_TARGETS or KPI_TARGETS_DEFAULT):
        t = KPI_TARGETS.get(sc) or KPI_TARGETS_DEFAULT.get(sc)
        if not t:
            continue
        ws.append([sc, t['delivery']['t0'], t['delivery']['t60'], t['delivery']['t100'],
                   t['t8']['t0'], t['t8']['t60'], t['t8']['t100'],
                   t['complaint']['t0'], t['complaint']['t60'], t['complaint']['t100'],
                   t['overtime']['t0'], t['overtime']['t60'], t['overtime']['t100']])
    for c in ws[1]:
        c.font = openpyxl.styles.Font(bold=True)
    import io
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def fetch_appeal_stats(cdp, team_id, today, cache_path=None, max_age=120, order_flags=None):
    """违规/申诉数据 → 服务质量KPI 计数（violationOrderAeolusCenterApi.download，2分钟缓存）。
    2026-10-02 对齐 10月晚复盘「问题单」派生列：排除申诉成功、排除运单判定不考核、拆分高笔单物流责。
    order_flags={'noexam':[运单id..], 'high':[运单id..]}（来自运单导出的订单判定/是否高笔单）
    返回 {logisticsFail, cancelOrders, earlyConfirm, highLogisticsOrders, complaintOrders,
          badReviewOrders, claimOrders} 或 None"""
    import csv as _csv
    now = time.time()
    if cache_path and cache_path.exists() and (now - cache_path.stat().st_mtime) < max_age:
        try:
            d = json.loads(cache_path.read_text(encoding='utf-8'))
            if 'cancelOrders' in d:      # 旧版缓存无新字段 → 重算（2026-10-02 口径升级）
                return d
        except Exception:
            pass
    try:
        from datetime import datetime as _dt
        day_start = int(_dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        day_end = day_start + 86400000 - 1
        body = {'params': {'request': {'violationStartTime': day_start, 'violationEndTime': day_end}}}
        r = cdp_core.call_api(cdp, 'xtop.lpd.quality.control.violation.violationOrderAeolusCenterApi.download',
                              body, ver='1.0')
        d = json.loads(r) if isinstance(r, str) else (r or {})
        file_url = (d.get('result') or {}).get('data', {}).get('fileUrl') or ''
        if not file_url:
            return None
        out = (cache_path.parent if cache_path else Path.home() / 'Downloads') / 'live_违规.csv'
        subprocess.run(['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', file_url, '-o', str(out)],
                       capture_output=True, timeout=120, **cdp_core._no_window_kw())
        if not out.exists() or out.stat().st_size < 60:
            return None
        raw = out.read_bytes()
        for enc in ('gb18030', 'utf-8'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        rows = list(_csv.reader(text.splitlines()))
        try:
            out.unlink()
        except Exception:
            pass
        # ⚠️ 旧阈值 500 字节在凌晨（当天只有 1~2 条违规）会把正常文件误判为失败 →
        #    改判表头：第一行含「违规编号」即认定为有效导出（2026-10-02）
        if not rows or '违规编号' not in str(rows[0][0] if rows[0] else ''):
            return None
        if len(rows) < 2:
            return {'logisticsFail': 0, 'cancelOrders': 0, 'earlyConfirm': 0, 'highLogisticsOrders': 0,
                    'complaintOrders': 0, 'badReviewOrders': 0, 'claimOrders': 0}
        headers = [str(h).strip() for h in rows[0]]
        idx = {}
        for i, h in enumerate(headers):
            if '违规分类' in h or '违规大类' in h:
                idx['cat'] = i
            if '违规类型' in h:
                idx['type'] = i
            if '团队ID' in h or '团队id' in h:
                idx['team'] = i
            if '运单号' in h:
                idx['order'] = i
            if '违规时间' in h:
                idx['date'] = i
            if ('骑手' in h and ('名称' in h or '姓名' in h or '信息' in h)) or ('骑士' in h and ('名称' in h or '姓名' in h)):
                idx['knight'] = i
            if '申诉状态' in h:
                idx['ap_status'] = i
            if '剩余申诉' in h or '申诉剩余' in h or h == '剩余时间':
                idx['ap_left'] = i
        if 'cat' not in idx or 'type' not in idx:
            return None
        today_str = today.replace('-', '')
        noexam = set(str(x).strip() for x in ((order_flags or {}).get('noexam') or []))
        high_ids = set(str(x).strip() for x in ((order_flags or {}).get('high') or []))
        result = {'logisticsFail': 0, 'cancelOrders': 0, 'earlyConfirm': 0, 'highLogisticsOrders': 0,
                  'complaintOrders': 0, 'badReviewOrders': 0, 'claimOrders': 0,
                  'details': []}
        for row in rows[1:]:
            if len(row) <= max(idx.values()):
                continue
            category = str(row[idx['cat']]).strip()
            vtype = str(row[idx['type']]).strip()
            base = vtype.split('(')[0].split('（')[0].strip()
            if not base:
                continue
            # 站点过滤：团队ID 匹配本站
            if idx.get('team') is not None and team_id:
                try:
                    if str(row[idx['team']]).strip() != str(team_id).strip():
                        continue
                except Exception:
                    pass
            # 当天过滤
            if idx.get('date') is not None:
                vdate = str(row[idx['date']])
                if today_str not in vdate.replace('-', '').replace('/', ''):
                    continue
            # 白名单过滤
            if base not in APPEAL_CATEGORY_TYPES.get(category, []):
                continue
            # 申诉成功 → 不计（10月表：问题单各列 AB="申诉成功" 一律 0）
            ap_status = str(row[idx['ap_status']]).strip() if idx.get('ap_status') is not None and idx['ap_status'] < len(row) else ''
            if ap_status == '申诉成功':
                continue
            # 运单判定「不考核」→ 不计（10月表：是否考核 D=0）
            order_no = str(row[idx['order']]).strip() if idx.get('order') is not None and idx['order'] < len(row) else ''
            if order_no and order_no in noexam:
                continue
            # 不满意单明细收集（照 RiderMonitor：餐货损/投诉/差评 分类，含骑手/运单/申诉信息）
            if category in ('餐货损', '投诉', '差评'):
                result['details'].append({
                    'category': category, 'vType': vtype, 'kname': (row[idx['knight']] if idx.get('knight') is not None and idx['knight'] < len(row) else '') or '',
                    'orderNo': order_no,
                    'vDate': vdate if idx.get('date') is not None else '',
                    'apStatus': ap_status,
                    'apLeft': (row[idx['ap_left']] if idx.get('ap_left') is not None and idx['ap_left'] < len(row) else '') or '',
                })
            # 归类（严格照 10月晚复盘「问题单」派生列）：
            # 1. 违规取消 → 物流责(表 D10)
            if base == '违规取消':
                result['cancelOrders'] += 1
                if order_no and order_no in high_ids:
                    result['highLogisticsOrders'] += 1   # 表 E14：高笔单 且 物流取消
            # 2. 类型含「提前」→ 物流责×2（表 E10/G），不论分类
            if '提前' in base:
                result['earlyConfirm'] += 1
            # 3. 餐货损 → 索赔（表 I，白名单=（违规分类,标签）对）
            if category == '餐货损':
                result['claimOrders'] += 1
            # 4. 投诉 → 不满意(×5)
            if category == '投诉':
                result['complaintOrders'] += 1
            # 5. 差评 → 不满意(×5)
            if category == '差评':
                result['badReviewOrders'] += 1
        # 兼容旧字段：物流责份数 = 物流取消 + 提前点×2 + 高笔单物流（虚假报备由运单侧另计）
        result['logisticsFail'] = (result['cancelOrders'] + result['earlyConfirm'] * 2
                                  + result['highLogisticsOrders'])
        if cache_path:
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    except Exception:
        return None


def fetch_pending_appeals(cdp, team_id, cache_path=None, max_age=300):
    """待申诉 / 申诉失败（不含超时：1001 由 appeal_overtime 脚本自动申诉）→ 工作台「今日待办」。
    返回 {'gen_at': ms, 'pending': [...], 'other': [...], 'overtime': [...], 'failed': [...]} 或 None
    明细项: {vType, category, kname, kid, orderNo, vTime, expire, reason, bonus}
    5 分钟缓存；接口异常返回 None（不阻断看板）。前端静态读 cache_path 同目录 json。"""
    now = time.time()
    if cache_path and cache_path.exists() and (now - cache_path.stat().st_mtime) < max_age:
        try:
            return json.loads(cache_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    try:
        # ⚠️ 接口窗口上限 = 24 小时（47h/48h 都报 20029「查询时间不能超过2天」），
        #    所以按自然日拆两个窗口（今天 + 昨天）分别查，避免漏掉昨天的待申诉单
        now_dt = datetime.now()
        day0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        windows = [(int((day0 - timedelta(days=1)).timestamp() * 1000),
                    int((day0 - timedelta(milliseconds=1)).timestamp() * 1000)),
                   (int(day0.timestamp() * 1000), int(time.time() * 1000))]
        rows = []
        for start_ms, end_ms in windows:
            for st_code in (10, 40):          # 10=待申诉 40=申诉失败
                for pn in (1, 2, 3):
                    body = {'params': {'request': {'teamId': int(team_id),
                                                   'violationStartTime': start_ms,
                                                   'violationEndTime': end_ms,
                                                   'appealStatus': [st_code],
                                                   'pageNo': pn, 'pageSize': 100}}}
                    r = cdp_core.call_api(
                        cdp, 'xtop.lpd.quality.control.violation.violationOrderAeolusCenterApi.queryList', body)
                    d = json.loads(r) if isinstance(r, str) else (r or {})
                    inner = d.get('result') or {}
                    if d.get('code') != 200 or inner.get('success') is False:
                        break
                    lst = (inner.get('data') or {}).get('violationEventList') or []
                    rows += lst
                    if len(lst) < 100:
                        break
        result = {'gen_at': int(time.time() * 1000),
                  'pending': [], 'other': [], 'overtime': [], 'failed': []}
        for e in rows:
            ti = str(e.get('teamInfo') or '')
            if team_id and ti and not ti.startswith(str(team_id)):   # 双保险站点过滤
                continue
            knight = str(e.get('knightInfo') or '')
            kid, _, kname = knight.partition('-')
            item = {'vType': e.get('violationType'), 'category': e.get('violationCategory'),
                    'kid': kid, 'kname': kname or knight, 'orderNo': e.get('trackingId'),
                    'vTime': e.get('violationTime'), 'expire': e.get('appealExpireTime'),
                    'reason': e.get('violationReason'), 'bonus': e.get('bonus')}
            st = e.get('appealStatus')
            if st == 10:
                result['pending'].append(item)
                (result['overtime'] if item['vType'] == 1001 else result['other']).append(item)
            elif st == 40:
                result['failed'].append(item)
        result['pending'].sort(key=lambda x: str(x.get('vTime') or ''), reverse=True)
        result['failed'].sort(key=lambda x: str(x.get('vTime') or ''), reverse=True)
        if cache_path:
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    except Exception:
        return None


def fetch_qc_stats(cdp, team_id, today, cache_path=None, max_age=120):
    """橙色风暴抽检（exportQcList，装备抽检不通过明细，2分钟缓存）。
    返回 {'loaded': bool, 'all': N, 'fails': [明细]} 或 None。
    明细项: {kname, kid, type, status, date, appeal, team}"""
    import base64 as _b64
    now = time.time()
    if cache_path and cache_path.exists() and (now - cache_path.stat().st_mtime) < max_age:
        try:
            return json.loads(cache_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    try:
        from datetime import datetime as _dt
        d0 = int(_dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        d1 = int(_dt.now().replace(hour=23, minute=59, second=59, microsecond=999).timestamp() * 1000)
        body = {'params': {'request': {'teamId': int(team_id), 'hitTimeStart': d0, 'hitTimeEnd': d1}}}
        r = cdp_core.call_api(cdp, 'xtop.lpd.omp.inspection.prod.qcFsRead.exportQcList', body, ver='1.0')
        d = json.loads(r) if isinstance(r, str) else (r or {})
        res = d.get('result') or {}
        b64 = res.get('data') or ''
        if not b64:
            return None
        raw = _b64.b64decode(b64)
        if len(raw) < 500:
            return None
        tmp = (cache_path.parent if cache_path else Path.home() / 'Downloads') / 'live_抽检.xlsx'
        tmp.write_bytes(raw)
        import openpyxl
        wb = openpyxl.load_workbook(str(tmp), data_only=True)
        try:
            tmp.unlink()
        except Exception:
            pass
        ws = wb.active
        headers = [str(c.value or '').strip() for c in ws[1]]
        idx = {}
        for i, h in enumerate(headers):
            if '抽检状态' in h or '状态' in h:
                idx['status'] = i
            if '抽检类型' in h or '类型' in h:
                idx['type'] = i
            if '抽检日期' in h or '日期' in h or '命中时间' in h:
                idx['date'] = i
            if '骑手ID' in h or '骑士ID' in h:
                idx['kid'] = i
            if '骑手姓名' in h or '骑手名称' in h or '骑士姓名' in h:
                idx['kname'] = i
            if '团队' in h and '名称' in h:
                idx['team'] = i
            if '申诉状态' in h:
                idx['appeal'] = i
        wb.close()
        if 'status' not in idx or 'kname' not in idx:
            return {'loaded': True, 'all': 0, 'fails': []}
        today_str = today.replace('-', '')
        all_rows = []
        fails = []
        seen_fail = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue
            def cell(k):
                j = idx.get(k)
                return str(row[j]).strip() if j is not None and j < len(row) and row[j] is not None else ''
            status = cell('status')
            vtype = cell('type')
            vdate = cell('date')
            if not status and not vtype and not vdate:
                continue
            # 当天过滤
            if vdate and today_str not in vdate.replace('-', '').replace('/', '').replace('.', ''):
                continue
            rec = {'kname': cell('kname'), 'kid': cell('kid'), 'type': vtype,
                   'status': status, 'date': vdate, 'appeal': cell('appeal'), 'team': cell('team')}
            all_rows.append(rec)
            # 不通过 且 装备（类型空/含"装备"）
            st_low = status.lower()
            is_fail = ('不通过' in status or 'unqualified' in st_low or 'fail' in st_low)
            if not is_fail:
                continue
            is_equip = (not vtype) or ('装备' in vtype) or ('equipment' in vtype.lower())
            if not is_equip:
                continue
            key = rec['kid'] or rec['kname']
            if key and key in seen_fail:
                continue
            if key:
                seen_fail.add(key)
            fails.append(rec)
        result = {'loaded': True, 'all': len(all_rows), 'fails': fails}
        if cache_path:
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    except Exception:
        return None


def _kpi_score(actual, t0, t60, t100, is_reverse):
    """三档目标线性插值：t0=0分线 / t60=60分线 / t100=100分线"""
    if actual is None or actual != actual:  # NaN 防护
        return 0
    if is_reverse:
        if actual <= t100:
            return 100
        if actual >= t0:
            return 0
        if actual <= t60:
            return 60 + (t60 - actual) / (t60 - t100) * 40
        return (t0 - actual) / (t0 - t60) * 60
    else:
        if actual >= t100:
            return 100
        if actual <= t0:
            return 0
        if actual >= t60:
            return 60 + (actual - t60) / (t100 - t60) * 40
        return (actual - t0) / (t60 - t0) * 60


def calc_service_kpi(valid_orders, total_orders, t8_ontime, ot_sec, appeal, weather_level, now, site_kpi=None):
    """服务质量KPI（10月晚复盘「指标」+「每日数据监控表」同式，2026-10-02 对表重写）
      妥投率 = C6/(C6 + 物流责份数)，物流责份数 = 物流取消 + 提前点×2 + 虚假报备出餐慢 + 高笔单物流（表 C10）
      T8     = (准时单 - 虚假报备 - 提前点×2)/(C6 + 高笔单非准时)（表 B14）
      不满意 = (投诉×5 + 差评×5 + 索赔 + 虚假报备)/接单数（表 B8）
      复合超时 = Σ复合超时时长/C6（表 B12）
      总分 = Σ得分×权重 ÷0.85 → 100 分制（表 A4；权重 妥投20%/T8 30%/不满意20%/复合超时15%）
    appeal: fetch_appeal_stats 计数；site_kpi: parse_punctual 站点级计数（虚假报备/准时/高笔单）"""
    appeal = appeal or {}
    sk = site_kpi or {}
    scenario = ('周中' if now.weekday() < 5 else '周末') + (weather_level or '正常天')
    targets = KPI_TARGETS.get(scenario, KPI_DEFAULT_TARGETS)
    c6 = int(sk.get('c6') or valid_orders or 0)
    orders = int(sk.get('orders') or total_orders or 0)
    early = appeal.get('earlyConfirm', 0) or 0
    fake_prep = int(sk.get('fake_prep') or 0)          # 虚假报备商户出餐慢未完成单（表 E6）
    cancel = appeal.get('cancelOrders', 0) or 0
    high_log = appeal.get('highLogisticsOrders', 0) or 0
    high_late = int(sk.get('highlate') or 0)           # 高笔单非准时（表 D14）
    ontime = int(sk.get('ontime') or t8_ontime or 0)
    ot_all = float(sk.get('ot_all') if sk.get('ot_all') is not None else (ot_sec or 0))
    complaint = appeal.get('complaintOrders', 0) or 0
    bad = appeal.get('badReviewOrders', 0) or 0
    claim = appeal.get('claimOrders', 0) or 0
    logistics_fail = cancel + early * 2 + fake_prep + high_log    # 表 C10
    # 指标（严格对齐 10月表公式）
    delivery_rate = c6 / (c6 + logistics_fail) if (c6 + logistics_fail) else 0
    t8_rate = (ontime - fake_prep - early * 2) / (c6 + high_late) if (c6 + high_late) else 0
    complaint_rate = (complaint * 5 + bad * 5 + claim + fake_prep) / orders if orders else 0
    avg_overtime = ot_all / c6 if c6 else 0
    t8_score = _kpi_score(t8_rate, targets['t8']['t0'], targets['t8']['t60'], targets['t8']['t100'], False)
    complaint_score = _kpi_score(complaint_rate, targets['complaint']['t0'], targets['complaint']['t60'], targets['complaint']['t100'], True)
    delivery_score = _kpi_score(delivery_rate, targets['delivery']['t0'], targets['delivery']['t60'], targets['delivery']['t100'], False)
    overtime_score = _kpi_score(avg_overtime, targets['overtime']['t0'], targets['overtime']['t60'], targets['overtime']['t100'], True)
    # 表 A4：Σ(得分×权重) ÷ 0.85（权重 0.2/0.3/0.2/0.15，合计 0.85）→ 满分 100
    raw = (t8_score * KPI_WEIGHTS['t8'] + complaint_score * KPI_WEIGHTS['complaint']
           + delivery_score * KPI_WEIGHTS['delivery'] + overtime_score * KPI_WEIGHTS['overtime'])
    total = raw / 0.85
    # 星级：单站点固定分档（RiderMonitor 多站时按排名分位）
    if total > 100:
        star, star_cls = 5, '#ffd700'
    elif total > 95:
        star, star_cls = 4, '#1890ff'
    elif total > 78:
        star, star_cls = 3, '#52c41a'
    elif total >= 65:
        star, star_cls = 2, '#fa8c16'
    else:
        star, star_cls = 1, '#ff4d4f'

    # 🛡 容错额度（平台「11月KPI容错单量」口径）：容错单量 = ⌈1÷(1−目标率)⌉（越高越好类）/
    # ⌈1÷目标率⌉（越低越好类）；允许坏单 = ⌊当日单量 ÷ 容错单量⌋；剩余 = 允许 − 已用。
    def _tol(p, reverse=False):
        """单个坏单需要多少总单量才能守住该分值线（=平台容错单量）"""
        try:
            if not p:
                return None
            return math.ceil(1 / p) if reverse else math.ceil(1 / (1 - p))
        except Exception:
            return None

    def _budget(name, rate, score, n_orders, bad_units, t60, t100, reverse, unit):
        tol60, tol100 = _tol(t60, reverse), _tol(t100, reverse)
        allow60 = int(n_orders // tol60) if (tol60 and n_orders) else 0
        allow100 = int(n_orders // tol100) if (tol100 and n_orders) else 0
        return {'name': name, 'rate': rate, 'score': score, 'orders': int(n_orders),
                'bad': bad_units, 'tol60': tol60, 'tol100': tol100,
                'allow60': allow60, 'allow100': allow100,
                'remain60': allow60 - bad_units, 'remain100': allow100 - bad_units,
                'unit': unit}

    t8_bad = max(0, (c6 + high_late) - (ontime - fake_prep - early * 2))
    budget = [
        _budget('物流妥投', delivery_rate, delivery_score, c6 + logistics_fail, logistics_fail,
                targets['delivery']['t60'], targets['delivery']['t100'], False, '单'),
        _budget('T8准时', t8_rate, t8_score, c6 + high_late, t8_bad,
                targets['t8']['t60'], targets['t8']['t100'], False, '单（含高笔单非准时）'),
        _budget('不满意', complaint_rate, complaint_score, orders,
                complaint * 5 + bad * 5 + claim + fake_prep,
                targets['complaint']['t60'], targets['complaint']['t100'], True, '加权坏单元（投诉/差评×5）'),
    ]

    return {
        'scenario': scenario,
        'budget': budget,
        'valid_orders': c6, 'total_orders': orders,
        'delivery_rate': delivery_rate, 'delivery_score': delivery_score,
        't8_rate': t8_rate, 't8_score': t8_score,
        'complaint_rate': complaint_rate, 'complaint_score': complaint_score,
        'avg_overtime': avg_overtime, 'overtime_score': overtime_score,
        'total': total, 'total_raw85': raw, 'star': star, 'star_cls': star_cls,
        'targets': targets, 'logistics_fail': logistics_fail, 'early': early,
        'cancel': cancel, 'fake_prep': fake_prep, 'high_log': high_log, 'high_late': high_late,
    }


def _budget_html(svc):
    """🛡 今日容错额度小表（平台「11月KPI容错单量」口径）"""
    rows = svc.get('budget') or []
    if not rows:
        return ''
    esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')  # noqa: E731

    def _cls(remain):
        return 'down' if remain < 0 else ('warnc' if remain == 0 else 'up')

    trs = []
    for b in rows:
        trs.append(
            f'<tr><td><b>{esc(b["name"])}</b><div style="font-size:11px;color:#A0AEC0">{esc(b["unit"])}</div></td>'
            f'<td>{b["rate"] * 100:.2f}%</td>'
            f'<td>{b["score"]:.1f}</td>'
            f'<td>{b["orders"]}</td>'
            f'<td>{b["tol60"]}</td>'
            f'<td class="{_cls(b["remain60"])}"><b>{b["remain60"]:+d}</b></td>'
            f'<td>{b["bad"]}</td>'
            f'<td class="{_cls(b["remain100"])}">{b["remain100"]:+d}</td></tr>')
    return ('<div class="aggr" style="border-top:1px dashed #E2E8F0;display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
            '<div style="font-weight:700">🛡 今日容错额度</div>'
            '<div style="font-size:12px;color:#718096">容错单量＝每 1 个坏单需多少总单量才守住该分值线'
            '（平台口径 ⌈1÷(1−目标率)⌉；不满意率 ⌈1÷目标率⌉）；允许坏单＝⌊今日单量÷容错单量⌋；'
            '剩余＝允许−已用（+ 仍有额度 / 负 已超）</div></div>'
            '<table><thead><tr><th>指标</th><th>当前值</th><th>得分</th><th>今日单量</th>'
            '<th>60分线容错</th><th>60分线剩余</th><th>已用坏单</th><th>100分线剩余</th></tr></thead><tbody>'
            + ''.join(trs) + '</tbody></table>')


def parse_attendance(xlsx_path):
    """解析实时出勤：每骑手每时段一行 → 骑手聚合 + 时段明细"""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

    def find_col(targets, blacklist=None):
        for t in targets:
            for i, h in enumerate(headers):
                if h and t in str(h) and not (blacklist and any(b in str(h) for b in blacklist)):
                    return i + 1
        return None

    c_name = find_col(['姓名'])
    c_period = find_col(['时段'], blacklist=['排班'])
    c_sched_status = find_col(['排班状态'])
    c_sched_periods = find_col(['排班时段'])
    c_work = find_col(['工作状态'])
    c_day_hours = find_col(['全天有效在线时长'])
    c_day_online = find_col(['全天在线时长'], blacklist=['有效'])   # 2026-11-06 加：档位查询显示无效在线时长
    c_day_orders = find_col(['全天完单量'])
    c_p_hours = find_col(['时段有效在线时长'])
    c_p_orders = find_col(['时段完单量'])
    c_delivering = find_col(['配送中单量'])

    if not c_name or not c_period:
        wb.close()
        return None, None

    # 排班时段（骑手 → 时段列表）
    rider_sched = {}
    for r in range(2, ws.max_row + 1):
        nm = ws.cell(r, c_name).value
        sp = ws.cell(r, c_sched_periods).value if c_sched_periods else None
        if nm and sp and nm not in rider_sched:
            rider_sched[nm] = [p.strip() for p in str(sp).replace('、', ',').split(',') if p.strip()]

    riders = {}   # 姓名 → 全天聚合
    periods = []  # 每骑手每时段明细
    for r in range(2, ws.max_row + 1):
        nm = ws.cell(r, c_name).value
        if not nm:
            continue
        period = ws.cell(r, c_period).value
        if not period or str(period).strip() == '全天':
            continue
        period = str(period).strip()
        raw_period = period
        if period in ('凌晨2', '凌晨3'):
            period = '凌晨2+凌晨3'  # 合并（对齐日报/监控表；但时长要求按 10月方案分段：凌晨2 1.3h / 凌晨3 1.2h）
        if period not in SHIFT_MAP:
            continue
        sched_status = str(ws.cell(r, c_sched_status).value or '') if c_sched_status else ''
        work = str(ws.cell(r, c_work).value or '') if c_work else ''
        p_orders = float(ws.cell(r, c_p_orders).value or 0) if c_p_orders else 0
        p_hours = parse_time(ws.cell(r, c_p_hours).value) if c_p_hours else 0.0
        delivering = float(ws.cell(r, c_delivering).value or 0) if c_delivering else 0
        day_hours = parse_time(ws.cell(r, c_day_hours).value) if c_day_hours else 0.0
        day_online = parse_time(ws.cell(r, c_day_online).value) if c_day_online else 0.0
        day_orders = float(ws.cell(r, c_day_orders).value or 0) if c_day_orders else 0

        is_sched = (sched_status == '排班') and (period in rider_sched.get(nm, []))

        # 全天聚合（取第一次出现，全天值每行相同；工作状态/配送中取最新行）
        if nm not in riders:
            riders[nm] = {
                'name': nm, 'day_hours': day_hours, 'day_online_hours': day_online or day_hours,
                'day_orders': day_orders,
                'work': work, 'delivering': int(delivering),
                'sched_today': bool(rider_sched.get(nm)),
                'last_shift': rider_sched.get(nm, [None])[-1] if rider_sched.get(nm) else None,
                'sched_periods': list(rider_sched.get(nm) or []),   # 当天排班时段全量（尖峰天贡献按时段算剩余可补时长用）
                'periods': {},
            }
        else:
            riders[nm]['work'] = work
            riders[nm]['delivering'] = int(delivering)
        # 时段明细
        std = SHIFT_MAP[period]
        req_hours = 1.3 if raw_period == '凌晨2' else std['hours']   # 10月方案：凌晨2 要求 1.3h（凌晨3=1.2h）
        periods.append({
            'name': nm, 'period': period,
            'hours': round(p_hours, 2), 'orders': p_orders,
            'scheduled': is_sched,
            'req_hours': req_hours, 'req_orders': std['orders'],
            'passed': (p_orders >= std['orders'] and p_hours >= req_hours),
            'work': work, 'delivering': int(delivering),
            'last_shift': rider_sched.get(nm, [None])[-1] if rider_sched.get(nm) else None,
        })
    wb.close()
    return riders, periods

def current_shift(now_h):
    """当前时间属于哪个时段（10时段表）"""
    for s in SHIFTS:
        if s[1] <= now_h < s[2]:
            return s[0]
    return None

def fetch_level_standard(cdp, team_id, today):
    """从「考核数据明细(新)」月度运力标准&单均同步(BASE_INFO)拉官方档位标准。
    返回 {'levels':[{'level','orders','hours','label'}], 'month':'202608', 'meta':{...}} 或 None"""
    try:
        now = datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        body = {'params': {'request': {'queryParam': {
            'ds': [int(month_start.timestamp() * 1000), int(time.time() * 1000)]},
            'planType': 'KPI_AGENCY', 'tabCode': 'BASE_INFO', 'pageIndex': 1, 'pageSize': 50}}}
        r = cdp_core.call_api(cdp, 'xtop.performance.fs.opp.perfDetail.queryDetailList', body)
        d = json.loads(r)
        data = d.get('result', {}).get('data', {})
        rows = data.get('data', [])
        ym = now.strftime('%Y%m')
        row = next((line for line in rows if line and line[0] and line[0].get('content') == ym), None)
        if not row:
            return None

        def col(i):
            try:
                v = row[i]['content']
                return v if v is not None else ''
            except Exception:
                return ''

        levels = []
        for lv in range(5):
            levels.append({'level': lv + 1,
                           'orders': int(float(col(9 + lv))),
                           'hours': int(float(col(14 + lv))),
                           'label': f'标{lv + 1}'})
        meta = {'grade': col(7), 'accident': col(8),
                'star_avg': [col(19 + i) for i in range(5)],
                'rider_avg': [col(24 + i) for i in range(5)],
                'th_coef': col(29), 'period_th': col(30),
                'high_ratio': col(31), 'keep_ratio': col(32)}
        return {'levels': levels, 'month': ym, 'meta': meta}
    except Exception:
        return None


def calc_level(day_hours, day_orders, levels=None):
    """取低原则算当前档位：从标5往下找双达档位；0=未达标"""
    lv = 0
    for L in (levels or LEVELS):
        if day_hours >= L['hours'] and day_orders >= L['orders']:
            lv = L['level']
    return lv

def build_board(riders, periods, sched_stats, now, order_stats=None, std=None, appeal=None, qc=None, special=None):
    """组装三大板块数据"""
    LVS = (std or {}).get('levels') or LEVELS
    BH = LVS[0]['hours']
    BO = LVS[0]['orders']
    now_h = now.hour + now.minute / 60
    combined, transfer_only, weather, punctual, site_kpi = (order_stats or (None, None, None, None, None))
    combined = combined or {}
    transfer_only = transfer_only or {}
    punctual = punctual or {}
    cur_shift = current_shift(now_h)
    # 当前时段必须是有排班的时段，否则取最近的下一个有排班时段
    active_periods = [p for p in SHIFTS if p[0] in sched_stats['periods']]
    if cur_shift and cur_shift in sched_stats['periods']:
        board_shift = cur_shift
    elif active_periods:
        board_shift = next((p[0] for p in active_periods if p[1] <= now_h), active_periods[0][0])
    else:
        board_shift = cur_shift

    # ── 板块1 全天达成 ──
    day_list = []
    for nm, rv in riders.items():
        eff_orders = max(0, int(rv['day_orders']) - int(combined.get(nm, 0) or 0))  # 去改派/无效后有效完单
        passed = rv['day_hours'] >= BH and eff_orders >= BO  # 达标基线=剔后完单≥18（2026-09-02 对齐晚复盘表 A列口径，原用原始单量多算）
        gap_t = max(0, BH - rv['day_hours'])
        gap_o = max(0, BO - eff_orders)
        if rv['day_hours'] == 0 and eff_orders == 0:
            gap = '未出勤'
        elif passed:
            gap = '达标'
        elif gap_o == 0:
            gap = f'差{fmt_hours(gap_t)}'
        elif gap_t == 0:
            gap = f'差{int(gap_o)}单'
        else:
            gap = f'差{int(gap_o)}单+{fmt_hours(gap_t)}'
        day_list.append({
            'name': nm, 'work': rv['work'], 'hours': rv['day_hours'],
            'orders': eff_orders, 'raw_orders': int(rv['day_orders']), 'delivering': rv['delivering'],
            'passed': passed, 'active': eff_orders > 0,  # 活跃=去改派/无效后有效完单>0（2026-09-02 对齐晚复盘表 实时考勤!A列 口径，0单在线不算活跃）
            'gap': gap,
            # 2026-09-27 修：原先漏带 last_shift → 「全天达成 · 未达标明细」的“最后班次”恒显示“其他”
            # （st_fail_rows 读 rv.get('last_shift')，day_list 项没这个键 → 落到 '其他' 兜底）
            'last_shift': rv.get('last_shift'),
        })
    passed_cnt = sum(1 for d in day_list if d['passed'])
    sched_actual = int(sched_stats.get('all_day', {}).get('actual', 0) or 0)
    sched_target = int(sched_stats.get('all_day', {}).get('target', 0) or 0)
    scheduled_cnt = sched_actual if sched_actual > 0 else sum(1 for d in riders.values() if d['sched_today'])
    active_cnt = sum(1 for d in day_list if d['active'])
    # 标2-5人数（对齐日报口径）：原始单量过基线(≥18单/6h)的骑手中，按考核单量(扣改派/无效)算档位≥2
    def _lv_eff(rv):
        return calc_level(rv['day_hours'],
                          max(0, int(rv['day_orders']) - int(combined.get(rv['name'], 0) or 0)), LVS)
    b2b5 = sum(1 for rv in riders.values()
               if max(0, int(rv['day_orders']) - int(combined.get(rv['name'], 0) or 0)) >= BO
               and rv['day_hours'] >= BH
               and _lv_eff(rv) >= 2)
    # 明细：只留活跃且未达成的（休息/未出勤不显示），按完单量少的在前
    day_failed = [d for d in day_list if not d['passed'] and d['active']]
    day_failed.sort(key=lambda x: x['orders'])
    # 场景/绩效行（对齐日报 Sheet1 头部）
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    day_name = weekday_names[now.weekday()]
    weather_info, weather_level = weather if weather else ('未知', '正常天')
    is_bad = weather_level in ('轻微恶天', '一般恶天') or str(weather_level).startswith('严重恶天')
    is_peak = now.weekday() >= 5 or is_bad
    scene = f'🌤 {weather_info}/{weather_level} | {day_name} | {"尖峰场景" if is_peak else "非尖峰场景"}'
    # 标2-5占比：应排分母（日报口径）；加减分只在尖峰场景（周末/恶天）触发
    b2b5_pct = b2b5 / sched_target * 100 if sched_target else 0
    if not is_peak:
        jf_txt = f'当前{b2b5_pct:.1f}% | ➖ 非尖峰场景，不加不扣'
        jf_cls = 'muted'
    else:
        jf_score = 2 if str(weather_level).startswith('严重恶天') else 1
        if b2b5_pct > 70:
            jf_txt = f'当前{b2b5_pct:.1f}% | ✅ 超过70% → 加{jf_score}分'
            jf_cls = 'pass'
        elif b2b5_pct < 30:
            jf_txt = f'当前{b2b5_pct:.1f}% | ❌ 低于30% → 扣{jf_score}分'
            jf_cls = 'fail'
        else:
            need_70 = int(sched_target * 0.70) + 1 if sched_target else 0
            gap_70 = max(0, need_70 - b2b5)
            jf_txt = f'当前{b2b5_pct:.1f}% | ➖ 不加不扣 | 还需{gap_70}人上标2以上→今日加{jf_score}分'
            jf_cls = 'warn'
    transfer_total = sum(transfer_only.values())
    day_summary = {
        'site_total': sched_stats['site_total'] or len(day_list),
        'sched_target': sched_target,
        'scheduled': scheduled_cnt,
        'active': active_cnt,
        'all': len(day_list),
        'passed': passed_cnt,
        'failed': len(day_failed),
        # 出勤率分母＝应排（全天 predictNum），与晚复盘表 D37=A35/A33 及 feebar 达成率同口径
        'rate': (passed_cnt / sched_target * 100) if sched_target else ((passed_cnt / scheduled_cnt * 100) if scheduled_cnt else 0),
        'b2b5': b2b5,
        'b2b5_pct': b2b5_pct,
        'scene': scene,
        'jf': jf_txt,
        'jf_cls': jf_cls,
        'transfer_total': transfer_total,
        'is_peak': is_peak,
    }

    # ── 尖峰天贡献（10小时30分钟 / 30单）· 2026-11-07 用户定稿：放在「全天达成」后面，按需查看 ──
    # 明细的显示方式/表头与「全天达成」未达标明细完全一致（姓名/状态/配送中/剔除后完单/在线时长/差单量/差时长/最后班次），
    # 口径换成：全天在线时长 ≥10小时30分钟 且 剔除后完单 ≥30单；分母=实排（listScheduleStatistics 全天 actualNum）；65%=60分线。
    SPIKE_H, SPIKE_O = 10.5, 30
    sp_tot = sched_actual or sched_target or len(riders)
    _sp_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    sp_left = max(0, int((_sp_end - now).total_seconds() // 60))
    sp_ok, sp_gap, sp_can = [], [], 0
    # 名单=实排（有非休息时段排班的人）；periods 不可用时退回「当天有排班」
    _sp_names = {p['name'] for p in periods if p.get('scheduled')} or None
    for nm, rv in riders.items():
        if _sp_names is not None:
            if nm not in _sp_names:
                continue
        elif not rv.get('sched_today'):
            continue      # 无排班（含纯休息）不计入分母名单
        hrs = float(rv.get('day_online_hours') or 0)
        net = max(0, int(rv['day_orders']) - int(combined.get(nm, 0) or 0))   # 剔除后完单（与全天达成同口径）
        dt_m = max(0, int(round((SPIKE_H - hrs) * 60)))
        do_n = max(0, SPIKE_O - net)
        online = rv.get('work') in ('上班', '配送中', '申请下班')
        # 时段时长：本人排班时段 ∩ [现在, 截止]，决定今天还来不来得及补满
        _sp_list = rv.get('sched_periods') or ([rv['last_shift']] if rv.get('last_shift') else [])
        left24 = sched_left_min(_sp_list, now_h, 24.0)
        left2330 = sched_left_min(_sp_list, now_h, 23.5)
        if not dt_m and not do_n:
            sp_ok.append(nm)
            continue
        if online and (do_n > 0 or dt_m <= left24):
            sp_can += 1       # 在线且今晚剩余的「时段时长」够补（单量缺口另算）
        sp_gap.append({'name': nm, 'work': rv.get('work') or '', 'delivering': int(rv.get('delivering') or 0),
                       'net': net, 'hours': hrs, 'dt_m': dt_m, 'do_n': do_n,
                       'rnd': int(math.ceil(dt_m / 5)) if dt_m else 0,   # 需上下线次数：一次上下线=5分钟，用差时长折算
                       'left24': left24, 'left2330': left2330, 'shift': ', '.join(_sp_list) or '未排班',
                       'active': net > 0, 'last_shift': rv.get('last_shift') or '未排班',
                       'kind': 't' if not do_n else ('o' if not dt_m else 'b')})
    sp_gap.sort(key=lambda x: x['net'])       # 与全天达成明细一致：完单少的在前
    sp_n_t = sum(1 for r in sp_gap if r['kind'] == 't')
    sp_n_o = sum(1 for r in sp_gap if r['kind'] == 'o')
    sp_n_b = sum(1 for r in sp_gap if r['kind'] == 'b')
    sp_line = int(math.ceil(sp_tot * 0.65)) if sp_tot else 0
    spike = {'h': SPIKE_H, 'o': SPIKE_O, 'tot': sp_tot, 'ok': len(sp_ok),
             'listed': len(sp_ok) + len(sp_gap),
             'gap': len(sp_gap), 'n_t': sp_n_t, 'n_o': sp_n_o, 'n_b': sp_n_b,
             'shown': sum(1 for r in sp_gap if r['active']),
             'no_show': sum(1 for r in sp_gap if not r['active']),
             'line': sp_line, 'can': sp_can, 'left': sp_left,
             'pct': (len(sp_ok) / sp_tot * 100) if sp_tot else 0,
             'rows': sp_gap}

    # ── 板块2 时段达成（日报风格：横向达标率表 + 考核判定 + 当前时段未达标明细）──
    # 1) 横向达标率表：达标率 = 达标骑手 / 应排目标（对齐日报/监控表）
    #    未开始的时段（还没到点）达标率显示"—"，不计入加权
    period_vals = {}
    aggr_sum = 0.0
    aggr_w = 0.0
    for pn, ps, pe, _, _, _ in SHIFTS:
        started = ps < now_h
        pd_rows = [p for p in periods if p['period'] == pn]
        sched_n = sum(1 for p in pd_rows if p['scheduled'])
        passed_n = sum(1 for p in pd_rows if p['scheduled'] and p['passed'])
        tgt_n = int(sched_stats['periods'].get(pn, {}).get('target', 0) or 0)
        rate = (passed_n / tgt_n if tgt_n > 0 else 1.0)
        period_vals[pn] = {'tgt': tgt_n, 'sched': sched_n, 'passed': passed_n,
                           'rate': rate, 'started': started}
        # ── 全天加权口径 = 公司晚复盘表「每日数据监控表!A18」的公式（用户 2026-09-26 定稿）：
        #    10 个时段**固定权重全部计入**，未开始时段达标率算 0%（随一天推进自然爬升），
        #    应排=0（本站不排这个班）按公司表 IFERROR(达标率,100%) 算 100%。
        #    ⚠️ 勿改回「只算已开始时段 + 权重重新归一」（2026-09-02 那版）：盘中午时会虚高约 30 个点，
        #       与晚复盘表/结算判定脱节（09-26 用户发现 13:50 显示 70.39%，公司口径应为 40.12%）。
        w = SHIFT_MAP[pn]['weight']
        aggr_sum += min(rate if rate is not None else 1.0, 1.0) * w
        aggr_w += w
    aggr_rate = (aggr_sum / aggr_w * 100) if aggr_w > 0 else 0

    sh_std = SHIFT_MAP[board_shift]
    is_ongoing = (board_shift == cur_shift)
    remain = max(0.0, sh_std['end'] - now_h) if is_ongoing else 0.0
    st = sched_stats['periods'].get(board_shift, {})

    # 2) 考核判定（9月口径：达标人数≥全天排班×规模门槛 95%[BASE_INFO th_coef col29，8月90%→9月95%] 且 时段达标率≥时段门槛 90%[period_th col30 不变]；封顶×110%）
    _m = (std or {}).get('meta') or {}
    try:
        scale_coef = float(_m.get('th_coef') or 0.95)
        period_coef = float(_m.get('period_th') or 0.90)
    except Exception:
        scale_coef, period_coef = 0.95, 0.90
    # ⚠️ 2026-10-02 对表修正：门槛/封顶基数用「应排」(sched_target)，不是排班数(scheduled_cnt)；
    #    门槛向上取整(ROUNDUP)、封顶向下取整(ROUNDDOWN) —— 与公司表 A33/B33/C33 及本页 feebar 同口径。
    #    改前用实排+int() 截断，同一页出现两个门槛（实测 判定栏 76/88 vs 服务费栏 77/89）。
    base_total = sched_target if sched_target > 0 else scheduled_cnt
    baseline_achieved = sum(1 for d in day_list if d['passed'])
    threshold = int(math.ceil(base_total * scale_coef))
    cap = int(math.floor(base_total * 1.10))
    rate_ok = aggr_rate >= period_coef * 100
    achieve_ok = baseline_achieved >= threshold
    # 高峰时段达成率（10月表 F33 / 方案：MIN(午高峰率,1)*0.7 + MIN(晚高峰率,1)*0.3，激励门槛 95%）
    peak_rate = ((min(period_vals.get('午高峰', {}).get('rate', 0.0), 1.0) * 0.7
                  + min(period_vals.get('晚高峰', {}).get('rate', 0.0), 1.0) * 0.3) * 100)
    judge = (f'时段达标率 {aggr_rate:.1f}% ' + (f'✅≥{period_coef*100:.0f}%' if rate_ok else f'❌<{period_coef*100:.0f}%')
             + f' | 达标 {baseline_achieved}人 ' + (f'✅≥门槛{threshold}人' if achieve_ok else f'❌<门槛{threshold}人')
             + f' | 封顶{cap}人' + (f'（已超{baseline_achieved - cap}人）' if baseline_achieved > cap else ''))
    shift_summary = {
        'name': board_shift,
        'req_hours': sh_std['hours'], 'req_orders': sh_std['orders'],
        'ongoing': is_ongoing, 'remain': remain,
        'target': st.get('target', 0), 'actual': st.get('actual', 0),
        'order_predict': st.get('order_predict', 0),
        'period_vals': period_vals,
        'aggr_rate': aggr_rate,
        'peak_rate': peak_rate,
        'judge': judge,
        'judge_ok': rate_ok and achieve_ok,
    }

    # 3) 各时段明细（已开始=未达标/非排班达成明细；未开始=占位提示）
    #    ⚠️ 2026-10-02 用户报「凌晨1明细在时段结束前自己消失」：原实现在「无未达标且无非排班达成」时
    #       直接 continue 不生成块 → 明细块凭空消失、点时段名也没反应（.shift-detail[data-shift] 找不到）。
    #       现在每个时段恒定生成块，无内容时显示「✅ 全部达标」。
    shift_details = {}
    for pn, ps, pe, _, _, _ in SHIFTS:
        if ps >= now_h:
            shift_details[pn] = {'ongoing': False, 'fails': [], 'unsched': [], 'not_started': True}
            continue
        rows = [p for p in periods if p['period'] == pn]
        fails = [p for p in rows if p['scheduled'] and not p['passed']]
        unsched = [p for p in rows if not p['scheduled'] and p['passed']]
        ongoing = ps <= now_h < pe
        remain = max(0.0, pe - now_h) if ongoing else 0.0
        frows = []
        for p in sorted(fails, key=lambda x: x['name']):
            ao, ro = int(p['orders']), int(p['req_orders'])
            ah, rh = p['hours'], p['req_hours']
            go = max(0, ro - ao)
            gh = max(0, round(rh - ah, 2))
            prediction = '-'
            if ongoing:
                t_ok = ah + remain >= rh
                if remain >= 0.5 and p['work'] in ('上班', '上线', '小休'):
                    prediction = '是' if t_ok else '否'
                else:
                    o_ok = (ao + p['delivering']) >= ro
                    prediction = '是' if (o_ok and t_ok) else '否'
            if go > 0 and gh > 0:
                verdict = '双不达标'
            elif go > 0:
                verdict = '完单不达标'
            elif gh > 0:
                verdict = '时长不达标'
            else:
                verdict = '✅达成'
            frows.append({
                'name': p['name'], 'orders': ao, 'hours': ah,
                'req_orders': ro, 'req_hours': rh,
                'gap_o': go, 'gap_h': gh,
                'work': p['work'], 'remain': remain,
                'last_shift': p['last_shift'] or '—',
                'delivering': p['delivering'],
                'prediction': prediction, 'verdict': verdict,
            })
        urows = [{
            'name': p['name'], 'orders': int(p['orders']), 'hours': p['hours'],
            'req_orders': p['req_orders'], 'req_hours': p['req_hours'],
            'gap_o': max(0, int(p['req_orders'] - p['orders'])),
            'gap_h': max(0, round(p['req_hours'] - p['hours'], 2)),
            'last_shift': p['last_shift'] or '—',
        } for p in sorted(unsched, key=lambda x: x['name'])]
        shift_details[pn] = {'ongoing': ongoing, 'fails': frows, 'unsched': urows, 'not_started': False}

    # ── 板块3 档位达成（考核单量口径 + 改派/无效标记 + 差距分级）──
    lv_rows = []
    for nm, rv in riders.items():
        eff = max(0, int(rv['day_orders']) - int(combined.get(nm, 0) or 0))
        transfer = int(transfer_only.get(nm, 0) or 0)
        invalid = int(combined.get(nm, 0) or 0) - transfer
        lv = calc_level(rv['day_hours'], eff, LVS)
        # 晋升标准文字（下一档要求；已满档=已满档；未达标=标1要求）
        if lv >= 5:
            std_txt = '已满档'
        else:
            _t = LVS[lv] if lv > 0 else {'level': 1, 'orders': BO, 'hours': BH}
            std_txt = f"{_t['orders']}单/{_t['hours']}h"
        if lv >= 5:
            tag, tcls, next_lv, gap_o, gap_t = '已满档', 'pass', '-', 0, 0
        else:
            tgt = LVS[lv] if lv > 0 else {'level': 1, 'orders': BO, 'hours': BH}
            next_lv = '标' + str(tgt['level'])
            gap_o = max(0, tgt['orders'] - eff)
            gap_t = max(0, tgt['hours'] - rv['day_hours'])
            if lv == 0 and gap_o >= 6:
                tag, tcls = '未达标', 'fail'
            elif gap_o == 0:
                tag, tcls = f'冲{next_lv}·差{fmt_hours(gap_t)}', 'pass'
            elif gap_o <= 2:
                tag, tcls = f'冲{next_lv}·差{gap_o}单', 'pass'
            elif gap_o <= 5:
                tag, tcls = f'升{next_lv}·差{gap_o}单', 'warn'
            else:
                tag, tcls = f'{next_lv}·差{gap_o}单', 'norm'
        ls = rv.get('last_shift')
        lv_rows.append({
            'name': nm, 'work': rv['work'], 'hours': rv['day_hours'],
            'orders': eff, 'raw_orders': int(rv['day_orders']),
            'delivering': rv['delivering'],
            'transfer': transfer, 'invalid': invalid,
            'level': lv,
            'level_label': ('标' + str(lv)) if lv else '未达标',
            'std_txt': std_txt,
            'next': next_lv, 'tag': tag, 'tcls': tcls,
            'gap_o': gap_o, 'gap_t': gap_t,
            'last_shift': ls or '未排班',
        })
    lv_counts = {i: 0 for i in range(6)}
    for rv in riders.values():
        eff2 = max(0, int(rv['day_orders']) - int(combined.get(rv['name'], 0) or 0))
        lv_counts[calc_level(rv['day_hours'], eff2, LVS)] += 1
    # 明细只留活跃骑手（休息/未出勤不显示）
    lv_rows = [r for r in lv_rows if r['hours'] > 0 or r['orders'] > 0]
    lv_rows.sort(key=lambda x: (x['level'], x['name']))

    # ── 板块4 骑手当天准时率排名（对齐 T-1 超时分析口径）──
    pt_rows = []
    for rid, d in punctual.items():
        rate = (d['valid'] - d['over']) / d['valid'] * 100 if d['valid'] else 0
        pt_rows.append({
            'name': d['name'] or rid, 'valid': d['valid'], 'over': d['over'],
            'ontime': d['valid'] - d['over'], 'rate': rate,
        })
    pt_rows.sort(key=lambda x: (-x['rate'], -x['valid']))
    pt_summary = {
        'riders': len(pt_rows),
        'total_valid': sum(r['valid'] for r in pt_rows),
        'total_over': sum(r['over'] for r in pt_rows),
        'avg_rate': ((sum(r['ontime'] for r in pt_rows) / sum(r['valid'] for r in pt_rows)) * 100
                     if pt_rows and sum(r['valid'] for r in pt_rows) else 0),
    }

    # ── 板块6 服务质量KPI（10月晚复盘「指标」/主表口径：妥投率·T8·不满意率·复合超时）──
    svc = None
    if punctual or site_kpi:
        try:
            valid_orders = sum(d['valid'] for d in punctual.values())
            t8_ontime = sum(d['valid'] - d['over'] for d in punctual.values())
            ot_sec = sum(d.get('ot', 0) for d in punctual.values())
            total_orders = sum(int(rv.get('day_orders') or 0) for rv in riders.values())
            wl = (weather[1] if weather and len(weather) > 1 else '正常天') or '正常天'
            svc = calc_service_kpi(valid_orders, total_orders, t8_ontime, ot_sec,
                                   appeal or {}, wl, now, site_kpi=site_kpi)
        except Exception:
            svc = None

    # ── 板块5 站点维度（RiderMonitor 风格：KPI卡/时段达成表/基础服务费/未达标明细）──
    # KPI 卡
    def _net_o(rv):
        """剔改派/无效后的有效完单"""
        return max(0, int(rv['day_orders']) - int(combined.get(rv['name'], 0) or 0))
    # ⚠️ 6h18单人数/计费口径用剔后完单（2026-09-10 修：原用原始单量 → 与「达标 X/Y」两个数对不上）
    d18_cnt = sum(1 for rv in riders.values() if rv['day_hours'] >= BH and _net_o(rv) >= BO)
    net_orders = max(0, sum(int(rv['day_orders']) for rv in riders.values()) - sum(combined.values()))
    kpi = {
        'active': active_cnt,
        'r5': sum(1 for rv in riders.values() if rv['day_orders'] >= 5),
        'd18': d18_cnt,
        'd18_pct': (d18_cnt / scheduled_cnt * 100) if scheduled_cnt else 0,
        'b2b5': b2b5,
        'b2b5_pct': b2b5_pct,
        'net_orders': net_orders,
        'percap': (net_orders / active_cnt) if active_cnt else 0,
        'transfer_riders': sum(1 for rv in riders.values() if (combined.get(rv['name'], 0) or 0) + (rv.get('invalid', 0) or 0) > 0),
    }
    # 时段达成明细（全天 + 各时段）
    period_st_rows = []
    pnames = [s[0] for s in SHIFTS if s[0] in sched_stats['periods']]
    all_tgt = sum(int(sched_stats['periods'].get(pn, {}).get('target', 0) or 0) for pn in pnames)
    for pn in (['全天'] + pnames):
        pd_rows = periods if pn == '全天' else [p for p in periods if p['period'] == pn]
        sched_n = sum(1 for p in pd_rows if p['scheduled'])
        no_show = [p for p in pd_rows if p['scheduled'] and p['hours'] == 0 and p['orders'] == 0]
        on_fail = [p for p in pd_rows if p['scheduled'] and (p['hours'] > 0 or p['orders'] > 0) and not p['passed']]
        passed_n = sum(1 for p in pd_rows if p['scheduled'] and p['passed'])
        tgt = all_tgt if pn == '全天' else int(sched_stats['periods'].get(pn, {}).get('target', 0) or 0)
        done = sum(int(p['orders']) for p in pd_rows)
        period_st_rows.append({
            'period': pn, 'tgt': tgt, 'sched': sched_n, 'passed': passed_n,
            'att_rate': (passed_n / tgt * 100) if tgt else 0,
            'no_show': len(no_show), 'no_show_list': no_show,
            'on_fail': len(on_fail), 'on_fail_list': on_fail,
            'sched_rate': (sched_n / tgt * 100) if tgt else 0,
            'done': done,
        })
    # 基础服务费：达标人数门槛=应排×规模门槛系数(BASE_INFO th_coef col29，9月0.95)，时段出勤门槛=period_th(col30，0.9不变)；封顶=应排×110%
    _m = (std or {}).get('meta') or {}
    try:
        scale_coef = float(_m.get('th_coef') or 0.95)
        period_coef = float(_m.get('period_th') or 0.90)
    except Exception:
        scale_coef, period_coef = 0.95, 0.90
    # ⚠️ 2026-09-18 用户抓错并修复：全天达成的 应排/门槛/封顶/达成率/计费 分母一律用「应排」
    #    (sched_target=排班统计全天行 predictNum)，不是「排班」(scheduled_cnt=actualNum)。
    #    改前 feebar 显示「应排 90(其实是排班数)」，而同一页时段表「全天」列显示应排 94 → 同页自相矛盾。
    #    门槛向上取整（对齐晚复盘 ROUNDUP）、封顶向下取整（ROUNDDOWN）。
    fee_target = sched_target if sched_target > 0 else scheduled_cnt
    fee = {'sched': fee_target,
           'sched_actual': scheduled_cnt,
           'threshold': int(math.ceil(fee_target * scale_coef)),
           'cap': int(math.floor(fee_target * 1.10)),
           'passed': kpi['d18'],
           'rate': (kpi['d18'] / fee_target * 100) if fee_target else 0,
           'aggr': aggr_rate,
           'th_label': f'{scale_coef * 100:.0f}%',
           'billed': min(kpi['d18'], fee_target)}
    # 体验兜底（10月方案第二章）：未达门槛时，若三项体验得分（妥投/T8/不满意）都≥60分 →
    #   当日基础服务费按各运力标准定价的「半价」结算；三项未同时达标 → 当日不结算。
    exp_ok = bool(svc) and all(
        (svc.get(k) or 0) >= 60 for k in ('delivery_score', 't8_score', 'complaint_score'))
    # ⚠️ 判定文案必须带当前三项得分（用户 2026-10-03 要求：「别只写<60，得告诉我当前是多少」）；
    #    未达 60 的项后缀 <60 直接点出来。三项 = 妥投(=表「物流责」)/准时(=表「准时送达率」)/不满意。
    exp_txt = ''
    if svc:
        _three = (('妥投', svc.get('delivery_score') or 0),
                  ('准时', svc.get('t8_score') or 0),
                  ('不满意', svc.get('complaint_score') or 0))
        exp_txt = '（体验得分 ' + ' / '.join(
            f"{n} {v:.1f}{'&lt;60' if v < 60 else ''}" for n, v in _three) + '）'
    if fee['rate'] >= scale_coef * 100 and fee['aggr'] >= period_coef * 100:
        fee['settle'] = f'✅ 可结算{exp_txt}'
        fee['settle_cls'] = 'pass'
    elif not svc:
        fee['settle'] = '⚠️ 未达门槛（体验数据未就绪，兜底待判）'
        fee['settle_cls'] = 'warn'
    elif exp_ok:
        fee['settle'] = f'⚠️ 未达门槛 → 体验兜底：按半价结算{exp_txt}'
        fee['settle_cls'] = 'warn'
    else:
        fee['settle'] = f'❌ 未达门槛且体验兜底未达标{exp_txt} → 当日不结算'
        fee['settle_cls'] = 'fail'
    fee['exp_ok'] = exp_ok
    # 未达标明细（剔除后完单/时长/差单/差时）
    st_fail_rows = []
    for rv in sorted(day_failed, key=lambda x: x['orders']):
        st_fail_rows.append({
            'name': rv['name'],
            'work': rv['work'],
            'delivering': rv['delivering'],
            'net': int(rv['orders']),  # day_list.orders 已是剔后完单
            'hours': rv['hours'],
            'gap_o': max(0, BO - int(rv['orders'])),
            'gap_h': max(0, BH - rv['hours']),
            'last_shift': rv.get('last_shift') or '未排班',
        })
    station_data = {'kpi': kpi, 'period_rows': period_st_rows, 'fee': fee,
                    'fail_rows': st_fail_rows, 'counts': lv_counts, 'current': board_shift}

    return {
        'now': now.strftime('%H:%M:%S'),
        'date': now.strftime('%Y-%m-%d'),
        'cur_shift': board_shift,
        'day': {'summary': day_summary, 'rows': day_failed},
        'spike': spike,
        'shift': {'summary': shift_summary, 'details': shift_details, 'current': board_shift},
        'level': {'rows': sorted(lv_rows, key=lambda x: (x['level'], x['name'])), 'counts': lv_counts},
        'punctual': {'summary': pt_summary, 'rows': pt_rows},
        'service': svc,
        'qc': qc,
        'appeal_details': (appeal or {}).get('details', []) if appeal else [],
        'special': special,
        'station': station_data,
        'std': std,
    }

def render_html(data, team_name, interval, pfx='s0', tid=0):
    """渲染单个站点的看板 block（页面外壳与全局样式由 render_multi 提供）"""
    bp_var = _board_pass()
    esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    ds, ss, ls, ptd = data['day'], data['shift'], data['level'], data['punctual']
    su = ds['summary']
    std = data.get('std')
    LVS = std['levels'] if std else LEVELS
    BH, BO = LVS[0]['hours'], LVS[0]['orders']
    st_data = data.get('station') or {}
    _m = (std or {}).get('meta') or {}
    try:
        SCALE = float(_m.get('th_coef') or 0.95)      # 规模/达标人数门槛系数（9月 0.95，8月 0.90）
        PERIOD = float(_m.get('period_th') or 0.90)   # 时段出勤门槛（0.90 不变）
    except Exception:
        SCALE, PERIOD = 0.95, 0.90
    # 汇总卡
    rate_cls = 'pass' if su['rate'] >= SCALE * 100 else ('warn' if su['rate'] >= 70 else 'fail')
    b2_cls = 'pass' if su['b2b5_pct'] > 70 else ('warn' if su['b2b5_pct'] >= 30 else 'fail')

    def work_pill(w):
        cls = {'上班': 'on', '上线': 'on', '小休': 'rest', '申请小休': 'rest',
               '下班': 'off', '申请下班': 'off', '下线': 'off'}.get(w, 'off')
        return f'<span class="pill {cls}">{esc(w)}</span>'

    def rows_html(rows, cols, attrs=None):
        trs = []
        for r in rows:
            tds = []
            for c in cols:
                tds.append(c(r))
            trs.append(f'<tr {attrs(r) if attrs else ""}>' + ''.join(tds) + '</tr>')
        return '\n'.join(trs)

    # 全天达成行
    day_rows = rows_html(ds['rows'], [
        lambda r: f'<td>{esc(r["name"])}</td>',
        lambda r: f'<td>{work_pill(r["work"])}</td>',
        lambda r: f'<td>{fmt_hours(r["hours"])}</td>',
        lambda r: f'<td>{r["orders"]}单</td>',
        lambda r: f'<td>{r["delivering"]}</td>',
        lambda r: f'<td><span class="pill {"pass" if r["passed"] else "fail"}">{"✅达标" if r["passed"] else "未达标"}</span></td>',
        lambda r: f'<td class="{"up" if r["passed"] else "down"}">{esc(r["gap"])}</td>',
    ])
    # 站点维度：未达标明细（剔除后完单/时长/差单/差时）
    st_fail_html = '\n'.join(
        f'<tr><td>{esc(r["name"])}</td><td>{work_pill(r["work"])}</td><td>{r["delivering"]}</td>'
        f'<td>{r["net"]}单</td><td>{fmt_hours(r["hours"])}</td>'
        f'<td class="down">{r["gap_o"]}单</td><td class="down">{fmt_hours(r["gap_h"])}</td>'
        f'<td>{esc(r["last_shift"])}</td></tr>'
        for r in st_data.get('fail_rows', []))
    # ── 尖峰天贡献（10小时30分钟 / 30单）· 按需查看：独立 tab，放「全天达成」后面 ──
    # 明细表头/列序与「全天达成 · 未达标骑手明细」完全一致（8 列），只把达标口径换成 10小时30分钟/30单
    spd = data.get('spike') or {}
    _sp_tot = spd.get('tot', 0) or 0
    _sp_ok = spd.get('ok', 0)
    _sp_can = spd.get('can', 0)
    _sp_upper = _sp_ok + _sp_can
    _sp_pct = spd.get('pct', 0)
    _sp_cls = 'pass' if _sp_pct >= 65 else ('warn' if _sp_pct >= 45 else 'fail')
    _sp_line = spd.get('line', 0)
    _sp_up_pct = (_sp_upper / _sp_tot * 100) if _sp_tot else 0
    _sp_need = max(0, _sp_line - _sp_ok)
    sp_rows_html = '\n'.join(
        f'<tr><td>{esc(r["name"])}</td><td>{work_pill(r["work"])}</td><td>{r["delivering"]}</td>'
        f'<td>{r["net"]}单</td><td>{fmt_hours(r["hours"])}</td>'
        f'<td class="down">{r["do_n"]}单</td><td class="down">{fmt_hours(r["dt_m"] / 60)}</td>'
        f'<td>{str(r["rnd"]) + " 次" if r["rnd"] else "—"}</td>'
        f'<td>{esc(r["last_shift"])}</td></tr>'
        for r in (spd.get('rows') or []) if r.get('active')) or \
        '<tr><td colspan="9" style="color:#718096;padding:10px 18px">当前没有未达标的有出勤骑手</td></tr>'

    spike_html = f'''
  <div class="tabpane" data-pane="spike">
  <div class="card">
    <div class="card-hd"><span>⚡ 尖峰天贡献</span><span class="sub">双条件：全天在线 ≥10小时30分钟 且 完单 ≥30单 · 贡献率=双达标/实排 · 65% = 60 分线 · 按需查看</span></div>
    <div class="verdict {_sp_cls}">
      <div class="v-main">贡献率 {_sp_pct:.1f}%<span class="v-tag">{'✅ 已过 65% 线' if _sp_pct >= 65 else f'⚠️ 未达 65%，差 {_sp_need} 人'}</span></div>
      <div class="v-sub">双达标 {_sp_ok} 人 ÷ 实排 {_sp_tot} 人 · 65% 线需 {_sp_line} 人 · 还在线上且能补 {_sp_can} 人 → 上限 {_sp_upper} 人（{_sp_up_pct:.1f}%） · 距 24:00 还有 {fmt_hours(spd.get('left', 0) / 60)}</div>
    </div>
    <div class="gapbar">
      <div class="gapcard"><div class="v up">{_sp_ok}</div><div class="k">已双达标</div></div>
      <div class="gapcard"><div class="v down">{spd.get('gap', 0)}</div><div class="k">缺口</div></div>
      <div class="gapcard"><div class="v info">{_sp_can}</div><div class="k">还能补（在线）</div></div>
      <div class="gapcard"><div class="v">{_sp_upper}<span class="gapn">{_sp_up_pct:.1f}%</span></div><div class="k">理论上限</div></div>
      <div class="gapcard"><div class="v warnc">{spd.get('n_t', 0)} / {spd.get('n_o', 0)} / {spd.get('n_b', 0)}</div><div class="k">只差时长 / 只差单量 / 双不达标</div></div>
    </div>
    <div class="shift-search"><input type="text" class="shift-search-input" placeholder="🔍 搜索骑手姓名 / 状态 / 班次" oninput="searchSpike(this)"></div>
    <div class="faildet">
      <div class="faildet-hd">📋 未达标骑手明细（{spd.get('shown', 0)} 人）· 点表头可升/降序 ⇅</div>
      <table>
        <thead><tr><th>姓名</th><th>状态</th><th>配送中</th><th>剔除后完单</th><th>在线时长</th><th>差单量</th><th>差时长</th><th>上下线次数</th><th>最后班次</th></tr></thead>
        <tbody>
{sp_rows_html}
        </tbody>
      </table>
    </div>
    <div style="padding:8px 18px 14px;font-size:12px;color:#718096">口径：时长取「全天在线时长」（在线状态累计，下线后携在途单不计）；完单取「剔除后完单」（扣改派/无效，与全天达成同口径）；
    达标线 = 全天在线 ≥10小时30分钟 且 完单 ≥30单；分母=实排 {_sp_tot} 人（listScheduleStatistics 全天实排）；65%=60 分线。上下线次数 = 差时长 ÷ 5 分钟（一次上下线按 5 分钟计），向上取整；差时长 0 的显示「—」。
    明细只显示有出勤的未达标骑手（未出勤 {spd.get('no_show', 0)} 人不列，同全天达成）。</div>
  </div>
  </div>
'''
    # 站点维度：时段达成竖表（每时段一行，未出勤/不达标可点弹窗）
    st_period_html = ('<div style="overflow-x:auto"><table>'
                      '<thead><tr><th>时段</th><th>应排</th><th>实排</th><th>达标</th>'
                      '<th>排班未出勤</th><th>出勤不达标</th><th>完成单</th></tr></thead><tbody>')
    for pr in st_data.get('period_rows', []):
        mark = ' ◀' if pr['period'] == st_data.get('current') else ''
        st_period_html += (f'<tr><td><b>{esc(pr["period"])}{mark}</b></td>'
                           + f'<td>{pr["tgt"]}</td><td>{pr["sched"]}</td><td>{pr["passed"]}</td>'
                           + f'<td><a class="rider-link{" warn" if pr["no_show"] else ""}" data-rider="nosh" data-period="{esc(pr["period"])}">{pr["no_show"]}</a></td>'
                           + f'<td><a class="rider-link{" warn" if pr["on_fail"] else ""}" data-rider="fail" data-period="{esc(pr["period"])}">{pr["on_fail"]}</a></td>'
                           + f'<td>{pr["done"]}</td></tr>')
    st_period_html += '</tbody></table></div>'
    # 站点维度：弹窗明细数据（每 block 独立）
    st_json = json.dumps({pr['period']: {
        'nosh': [{'n': p['name'], 'd': p['delivering']} for p in pr['no_show_list']],
        'fail': [{'n': p['name'], 'h': p['hours'], 'o': p['orders'],
                  'rh': p['req_hours'], 'ro': p['req_orders']} for p in pr['on_fail_list']],
    } for pr in st_data.get('period_rows', [])}, ensure_ascii=False)
    # 时段明细行（进行中12列带预测 / 已结束9列带判定）
    # 差单/差时列统一普通色，红绿语义交给「预测」（进行中）和「判定」（已结束）列
    def gap_cell_orders(g):
        return f'<td>{"✓" if g <= 0 else f"{g:.0f}"}</td>'
    def gap_cell_hours(g, recoverable=False):
        return f'<td>{"✓" if g <= 0 else "差" + fmt_hours(g)}</td>'
    def shift_detail_html(pn, det, idx):
        if det.get('not_started'):
            _st = SHIFT_MAP[pn]['start']
            _hhmm = f'{int(_st):02d}:{int(round((_st - int(_st)) * 60)):02d}'
            return ('<div class="shift-detail" data-shift="' + str(idx) + '">'
                    '<div style="padding:12px 18px;color:#718096">⏳ ' + esc(pn)
                    + ' 尚未开始（' + _hhmm + ' 起）</div></div>')
        htmls = []
        if det['fails']:
            if det['ongoing']:
                head = ['骑手', '工作状态', '实完单', '有效时长', '要求单', '要求时', '差单', '差时',
                        '剩余时间', '配送中', '预测']
                rows = []
                for r in det['fails']:
                    recover = det['ongoing'] and r['gap_h'] > 0 and r['gap_h'] < r['remain']
                    rows.append('<tr>'
                        + f'<td>{esc(r["name"])}</td>'
                        + f'<td>{work_pill(r["work"])}</td>'
                        + f'<td>{r["orders"]}单</td>'
                        + f'<td>{fmt_hours(r["hours"])}</td>'
                        + f'<td>{r["req_orders"]}单</td>'
                        + f'<td>{fmt_hours(r["req_hours"])}</td>'
                        + gap_cell_orders(r['gap_o'])
                        + gap_cell_hours(r['gap_h'], recover)
                        + f'<td>{fmt_hours(r["remain"])}</td>'
                        + f'<td>{r["delivering"]}</td>'
                        + f'<td><span class="pill {"pass" if r["prediction"] == "是" else "fail" if r["prediction"] == "否" else "ok"}">{esc(r["prediction"])}</span></td>'
                        + '</tr>')
            else:
                head = ['骑手', '实完单', '有效时长', '要求单', '要求时', '差单', '差时', '判定']
                rows = []
                for r in det['fails']:
                    vcls = {'双不达标': 'fail', '完单不达标': 'fail', '时长不达标': 'warn'}.get(r['verdict'], 'pass')
                    rows.append('<tr>'
                        + f'<td>{esc(r["name"])}</td>'
                        + f'<td>{r["orders"]}单</td>'
                        + f'<td>{fmt_hours(r["hours"])}</td>'
                        + f'<td>{r["req_orders"]}单</td>'
                        + f'<td>{fmt_hours(r["req_hours"])}</td>'
                        + gap_cell_orders(r['gap_o'])
                        + gap_cell_hours(r['gap_h'])
                        + f'<td><span class="pill {vcls}">{esc(r["verdict"])}</span></td>'
                        + '</tr>')
            htmls.append('<div class="subhd">' + esc(pn) + ' · 未达成明细（' + str(len(det['fails'])) + '人）</div>'
                         + '<table><thead><tr>' + ''.join(f'<th>{h}</th>' for h in head) + '</tr></thead>'
                         + '<tbody>' + ''.join(rows) + '</tbody></table>')
        if det['unsched']:
            head = ['骑手', '实完单', '有效时长', '要求单', '要求时', '差单', '差时', '判定']
            rows = []
            for r in det['unsched']:
                rows.append('<tr class="row-unsched">'
                    + f'<td>{esc(r["name"])}</td>'
                    + f'<td>{r["orders"]}单</td>'
                    + f'<td>{fmt_hours(r["hours"])}</td>'
                    + f'<td>{r["req_orders"]}单</td>'
                    + f'<td>{fmt_hours(r["req_hours"])}</td>'
                    + gap_cell_orders(r['gap_o'])
                    + gap_cell_hours(r['gap_h'])
                    + '<td><span class="pill ok">✅达成</span></td>'
                    + '</tr>')
            htmls.append('<div class="subhd unsched-hd">' + esc(pn) + ' · 非排班达成（' + str(len(det['unsched'])) + '人）</div>'
                         + '<table><thead><tr>' + ''.join(f'<th>{h}</th>' for h in head) + '</tr></thead>'
                         + '<tbody>' + ''.join(rows) + '</tbody></table>')
        if not htmls:
            htmls.append('<div class="up" style="padding:12px 18px">✅ '
                         + esc(pn) + ' 全部达标</div>')
        search_box = ('<div class="shift-search"><input type="text" class="shift-search-input" '
                      f'data-shift="{idx}" placeholder="🔍 搜索骑手姓名" oninput="searchShift(this)"></div>'
                      if det['ongoing'] else '')
        return ('<div class="shift-detail" data-shift="' + str(idx) + '"'
                + (' style="display:block"' if det['ongoing'] else '') + '>'
                + search_box
                + ''.join(htmls) + '</div>')

    # 横向时段达标率表（时段名可点击，日报/监控表格式，末尾带「全天」列）
    pv = ss['summary']['period_vals']
    pns = [s[0] for s in SHIFTS
           if pv.get(s[0], {}).get('tgt', 0) > 0 or pv.get(s[0], {}).get('sched', 0) > 0]
    cur_idx = next((i for i, p in enumerate(pns) if p == ss['current']), 0)
    # 全天列（达标率=全天达标/应排，对齐时段口径）
    pv_all = dict(pv)
    pv_all['全天'] = {
        'tgt': su['sched_target'], 'sched': su['scheduled'], 'passed': su['passed'],
        'rate': (su['passed'] / su['sched_target']) if su['sched_target'] else None,
    }
    cols = pns + ['全天']
    # 「进行中」标记按该时段自己的时间窗判断（原来用 ss['current']＝board_shift 的兜底值：
    #  02:00-06:00 本站不排凌晨2/3 时 board_shift 会停在已结束的「凌晨1」→ 已结束时段被标成进行中，
    #  点击走内嵌展开而非弹窗。2026-10-02 修）⚠️ HTML 模板里没有 now_h，用 data['now'] 自己算
    _now_h = float(data['now'].split(':')[0]) + float(data['now'].split(':')[1]) / 60
    ongoing_by_p = {p: (1 if (SHIFT_MAP[p]['start'] <= _now_h < SHIFT_MAP[p]['end']) else 0) for p in pns}
    pv_head = ('<tr><th>项目</th>'
               + ''.join(f'<th><button class="pvlink" data-idx="{i}" data-ongoing="{ongoing_by_p[p]}">{esc(p)}'
                         + ('<span class="ongoing-tag">进行中</span>' if ongoing_by_p[p] else '')
                         + '</button></th>'
                         for i, p in enumerate(pns))
               + '<th class="col-all">全天</th></tr>')
    def pv_row(label, key, fmt):
        return ('<tr><td class="pvlabel">' + label + '</td>'
                + ''.join(f'<td>{fmt(pv_all[p][key])}</td>' for p in cols) + '</tr>')
    pv_rows = (pv_row('应排目标', 'tgt', lambda v: str(v))
               + pv_row('实排目标', 'sched', lambda v: str(v))
               + pv_row('达标骑手', 'passed', lambda v: str(v)))
    # 排班未出勤 / 出勤不达标（横向加行，红色数字可点弹窗；未开始的时段显示—）
    st_by_p = {pr['period']: pr for pr in st_data.get('period_rows', [])}
    now_hh = float(data['now'].split(':')[0]) + float(data['now'].split(':')[1]) / 60
    shift_start = {s[0]: s[1] for s in SHIFTS}
    def st_row(label, key, tkey):
        cells = ['<tr><td class="pvlabel">' + label + '</td>']
        for p in cols:
            pr = st_by_p.get(p)
            v = pr[key] if pr else 0
            if p == '全天':
                cells.append(f'<td class="col-all" class="{"down" if v else "mutedc"}">{v}</td>')
            elif shift_start.get(p, 0) > now_hh:
                cells.append('<td style="color:#333333">—</td>')
            elif v:
                cells.append(f'<td class="down"><a class="rider-link warn" data-rider="{tkey}" data-period="{esc(p)}">{v}</a></td>')
            else:
                cells.append('<td style="color:#333333">0</td>')
        return ''.join(cells) + '</tr>'
    pv_rows += (st_row('排班未出勤', 'no_show', 'nosh')
                + pv_row('达标率', 'rate',
                         lambda v: '—' if v is None else f'{min(v, 1.0) * 100:.0f}%'))
    # 各时段明细（全部渲染，默认显示当前时段，点击时段名切换）
    shift_detail_blocks = []
    for i, pn in enumerate(pns):
        det = ss['details'].get(pn)
        if det:
            shift_detail_blocks.append(shift_detail_html(pn, det, i))
    if not shift_detail_blocks:
        shift_detail_blocks.append('<div class="shift-detail" id="shift-detail-0"><div class="up" style="padding:14px 18px">✅ 已开始时段全部达标</div></div>')
    cur_idx = min(cur_idx, len(pns) - 1)
    shift_detail_blocks_str = ''.join(shift_detail_blocks)
    aggr_ok = ss['summary']['aggr_rate'] >= PERIOD * 100
    _aggr_ok_txt = f'✅ 已达标（≥{PERIOD*100:.0f}%）'
    _aggr_no_txt = f'❌ 未达标（<{PERIOD*100:.0f}%）'
    aggr_html = (f'<div class="aggr">📊 全天时段达标率：'
                 f'<b class="{"up" if aggr_ok else "down"}" style="font-size:20px">{ss["summary"]["aggr_rate"]:.2f}%</b> '
                 f'<span class="{"up" if aggr_ok else "down"}">{_aggr_ok_txt if aggr_ok else _aggr_no_txt}</span>'
                 f'　·　进行中时段明细已展开；已结束时段点击时段名弹窗查看</div>')
    pv_html = (f'<div class="subhd">全天时段达标率</div>'
               f'<div class="pvwrap"><table class="pvtable"><thead>{pv_head}</thead>'
               f'<tbody>{pv_rows}</tbody></table>{aggr_html}</div>')
    # 档位行（表头：姓名/状态/档位/有效时长/全天完单/配送中/档位标准/差单/晋升档位/最后班次）
    def lv_tr_attrs(r):
        if r['level'] >= 5:
            cat = 'man'
        elif r['orders'] < BO:
            cat = 'fail'  # 未达成基本出勤：档位完单没到标1单量
        elif r['gap_o'] <= 2:
            cat = 'chong'
        elif r['gap_o'] <= 5:
            cat = 'sheng'
        else:
            cat = 'gap'  # 过18单但升档差6单以上（仅全部可见）
        red = '1' if (r['transfer'] + r['invalid'] > 0) else '0'
        return f'data-cat="{cat}" data-red="{red}" data-shift="{esc(r["last_shift"])}"'
    lv_rows = rows_html(ls['rows'], [
        lambda r: (f'<td><span class="down">{esc(r["name"])}</span>'
                   f'<span class="flag">改{r["transfer"]}/无{r["invalid"]}</span></td>'
                   if r['transfer'] + r['invalid'] > 0 else f'<td>{esc(r["name"])}</td>'),
        lambda r: f'<td>{work_pill(r["work"])}</td>',
        lambda r: f'<td><span class="pill {"pass" if r["level"] == 5 else "ok" if r["level"] > 0 else "fail"}">{esc(r["level_label"])}</span></td>',
        lambda r: f'<td>{fmt_hours(r["hours"])}</td>',
        lambda r: f'<td>{r["orders"]}单</td>',
        lambda r: f'<td>{r["delivering"]}</td>',
        lambda r: f'<td><span class="{next_lv_cls(r["next"])}">{esc(r["next"])}</span></td>',
        lambda r: (f'<td class="{"up" if r["gap_o"] == 0 else "down" if r["tcls"] == "fail" or r["gap_o"] <= 2 else "warnc" if r["tcls"] == "warn" else "mutedc"}">'
                   f'{("✓" if r["gap_o"] == 0 else r["gap_o"])}</td>'),
        lambda r: f'<td>{esc(r["std_txt"])}</td>',
        lambda r: f'<td>{esc(r["last_shift"])}</td>',
    ], lv_tr_attrs)

    lv_badges = ''.join(
        f'<span class="lvchip">{"标" + str(i) if i else "未达标"} <b>{ls["counts"][i]}</b>人</span>'
        for i in range(6))

    # 准时率行（排名/骑手/有效单/准时单/超时单/准时率）
    def pt_row_html(i, r):
        rate_cls = 'up' if r['rate'] >= 98 else ('warnc' if r['rate'] >= 90 else 'down')
        medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(i + 1, '')
        return ('<tr>'
                + f'<td><b>{i + 1}</b> {medal}</td>'
                + f'<td>{esc(r["name"])}</td>'
                + f'<td>{r["valid"]}</td>'
                + f'<td class="up">{r["ontime"]}</td>'
                + f'<td class="down">{r["over"]}</td>'
                + f'<td class="{rate_cls}">{r["rate"]:.1f}%</td>'
                + '</tr>')
    pt_rows_html = '\n'.join(pt_row_html(i, r) for i, r in enumerate(ptd['rows']))

    # ⭐ 服务质量KPI 面板（10月晚复盘口径：4指标得分 + 加权总分÷0.85=100分制 + 星级）
    def _sc(score):
        return 'up' if score >= 90 else ('warnc' if score >= 60 else 'down')
    svc = data.get('service')
    if svc and svc.get('valid_orders'):
        tg = svc['targets']
        star_txt = '★' * svc['star'] + '☆' * (5 - svc['star'])
        total_cls = 'up' if svc['total'] >= 90 else ('warnc' if svc['total'] >= 60 else 'down')
        star_html = f'<span style="color:{esc(svc["star_cls"])};font-size:22px;letter-spacing:2px">{star_txt}</span>'
        svc_html = f'''<div class="tabpane" data-pane="service">
  <div class="card">
    <div class="card-hd" style="background:linear-gradient(135deg,#722ed1,#b37feb);color:#fff;border-radius:10px 10px 0 0">
      <span>⭐ 服务质量KPI · {esc(svc['scenario'])}</span>
      <span class="sub" style="color:#f3e8ff">满分100（Σ得分×权重÷0.85，晚复盘同式）· 权重：T8准时30% / 不满意20% / 物流妥投20% / 复合超时15% · 目标为60分线（随周中/周末×天气动态）</span>
    </div>
    <div class="sumline">
      <div class="sumitem"><div class="v" class="{_sc(svc['t8_score'])}">{svc['t8_score']:.1f}</div>
        <div class="k">T8准时率<br>{svc['t8_rate'] * 100:.1f}% · 目标 {tg['t8']['t60'] * 100:.2f}%</div></div>
      <div class="sumitem"><div class="v" class="{_sc(svc['complaint_score'])}">{svc['complaint_score']:.1f}</div>
        <div class="k">不满意率<br>{svc['complaint_rate'] * 100:.3f}% · 目标 &lt;{tg['complaint']['t60'] * 100:.3f}%</div></div>
      <div class="sumitem"><div class="v" class="{_sc(svc['delivery_score'])}">{svc['delivery_score']:.1f}</div>
        <div class="k">物流妥投率<br>{svc['delivery_rate'] * 100:.2f}% · 目标 {tg['delivery']['t60'] * 100:.2f}%</div></div>
      <div class="sumitem"><div class="v" class="{_sc(svc['overtime_score'])}">{svc['overtime_score']:.1f}</div>
        <div class="k">复合超时<br>{svc['avg_overtime']:.0f}秒/单 · 目标 &lt;{tg['overtime']['t60']}秒</div></div>
    </div>
    <div class="aggr" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <div>⭐ 服务质量总得分：<b class="{total_cls}" style="font-size:22px">{svc['total']:.1f}</b> / 100 {star_html}</div>
      <div style="font-size:12px;color:#718096">有效完成单 {svc['valid_orders']}（表C6）· 物流责份数 {svc['logistics_fail']}＝取消{svc.get('cancel', 0)}+提前点×2({svc['early']})+虚假报备{svc.get('fake_prep', 0)}+高笔单{svc.get('high_log', 0)} · 高笔单非准时 {svc.get('high_late', 0)} · 复合超时口径：8分钟内不计/8-15分按实/15-30分×1.5/超30分×4封顶</div>
    </div>
    {_budget_html(svc)}
    <div style="padding:8px 18px;font-size:12px;color:#718096">得分 = 指标值在三档目标（0分线/60分线/100分线）间线性插值，恶天自动放宽目标 · 星级：&gt;78=3星 / ≥65=2星 / 其余1星</div>
  </div>
  </div>'''
    else:
        svc_html = ('<div class="tabpane" data-pane="service"><div class="card">'
                    '<div style="padding:22px 18px;text-align:center;color:#888">'
                    '⭐ 服务质量KPI 数据加载中…（运单/违规数据未就绪）</div></div></div>')

    # 🔶 橙色风暴抽检面板（装备抽检不通过明细）
    qc = data.get('qc')
    if qc and qc.get('loaded'):
        qc_fails = qc.get('fails') or []
        qc_html = f'''<div class="tabpane" data-pane="qc">
  <div class="card">
    <div class="card-hd" style="background:linear-gradient(135deg,#fa541c,#ff7a45);color:#fff;border-radius:10px 10px 0 0">
      <span>🔶 橙色风暴抽检 · {esc(data['date'])}</span>
      <span class="sub" style="color:#fff1e8">筛选：抽检状态=不通过 且 抽检类型=装备抽检 且 当天 · 未申诉标红</span>
    </div>
    <div class="sumline">
      <div class="sumitem"><div class="v">{qc.get('all', 0)}</div><div class="k">当天抽检记录</div></div>
      <div class="sumitem"><div class="v" style="color:#fa541c">{len(qc_fails)}</div><div class="k">装备抽检不通过</div></div>
      <div class="sumitem"><div class="v" style="color:#722ed1">{len(qc_fails)}</div><div class="k">已剔除无效达成</div></div>
    </div>
    {('<table><thead><tr><th>#</th><th>骑手</th><th>骑手ID</th><th>抽检类型</th><th>抽检状态</th><th>抽检日期</th><th>申诉状态</th></tr></thead><tbody>'
      + ''.join(f'<tr><td>{i + 1}</td><td><b>{esc(f["kname"])}</b></td><td style="color:#718096">{esc(f["kid"])}</td>'
                f'<td>{esc(f["type"])}</td><td style="color:#fa541c;font-weight:700">{esc(f["status"])}</td>'
                f'<td>{esc(f["date"])}</td>'
                f'<td class="{"down" if "未申诉" in f.get("appeal", "") else "mutedc"}">{esc(f.get("appeal") or "--")}</td></tr>'
                for i, f in enumerate(qc_fails)) + '</tbody></table>')
     if qc_fails else '<div style="padding:18px;text-align:center;color:#718096">✅ 当天无装备抽检不通过记录</div>'}
    <div style="padding:8px 18px;font-size:12px;color:#fa541c">※ 装备抽检不通过且达标（标1及以上）的骑手，考核中作为无效达成剔除</div>
  </div>
  </div>'''
    else:
        qc_html = ('<div class="tabpane" data-pane="qc"><div class="card">'
                   '<div style="padding:22px 18px;text-align:center;color:#888">🔶 橙色风暴抽检 数据加载中…</div></div></div>')

    # 📋 不满意单明细面板（服务质量KPI 不满意率下钻）
    ap_rows = data.get('appeal_details') or []
    if ap_rows:
        claim_n = sum(1 for r in ap_rows if r.get('category') == '餐货损')
        comp_n = sum(1 for r in ap_rows if r.get('category') == '投诉')
        bad_n = sum(1 for r in ap_rows if r.get('category') == '差评')
        fold = claim_n * 1 + (comp_n + bad_n) * 5
        appeal_html = f'''<div class="tabpane" data-pane="appeal">
  <div class="card">
    <div class="card-hd" style="background:linear-gradient(135deg,#f5222d,#ff7875);color:#fff;border-radius:10px 10px 0 0">
      <span>📋 不满意单明细 · {esc(data['date'])}</span>
      <span class="sub" style="color:#fff1f0">KPI口径：餐货损(索赔)×1、投诉×5、差评×5 · 待申诉且剩余&lt;5小时标红置顶</span>
    </div>
    <div class="sumline">
      <div class="sumitem"><div class="v" style="color:#f5222d">{len(ap_rows)}</div><div class="k">明细单数</div></div>
      <div class="sumitem"><div class="v" style="color:#e67e22">{claim_n}</div><div class="k">索赔(餐货损)×1</div></div>
      <div class="sumitem"><div class="v" style="color:#722ed1">{comp_n}</div><div class="k">投诉×5</div></div>
      <div class="sumitem"><div class="v" style="color:#eb2f96">{bad_n}</div><div class="k">差评×5</div></div>
      <div class="sumitem"><div class="v" style="color:#1890ff">{fold}</div><div class="k">折算不满意单</div></div>
    </div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>#</th><th>骑手</th><th>运单号</th><th>分类</th><th>违规类型</th><th>违规时间</th><th>申诉状态</th><th>剩余申诉时间</th><th>折算</th></tr></thead><tbody>
      {''.join(f'<tr><td>{i + 1}</td><td><b>{esc(r.get("kname") or "-")}</b></td>'
               f'<td style="white-space:nowrap">{esc(r.get("orderNo") or "-")}</td>'
               f'<td>{esc(r.get("category") or "")}</td><td>{esc(r.get("vType") or "")}</td>'
               f'<td style="white-space:nowrap;font-size:11px">{esc(r.get("vDate") or "")}</td>'
               f'<td>{esc(r.get("apStatus") or "--")}</td><td>{esc(r.get("apLeft") or "--")}</td>'
               f'<td>{"×1" if r.get("category") == "餐货损" else "×5"}</td></tr>'
               for i, r in enumerate(ap_rows))}
      </tbody></table></div>
    <div style="padding:8px 18px;font-size:12px;color:#718096">已过滤欺诈/逆向单与剔除商户 · 数据源=当日违规明细（与服务质量KPI 同源）</div>
  </div>
  </div>'''
    else:
        appeal_html = ('<div class="tabpane" data-pane="appeal"><div class="card">'
                       '<div style="padding:22px 18px;text-align:center;color:#888">'
                       '✅ 当天无不满意单明细（餐货损/投诉/差评 均为0）</div></div></div>')

    # 🔎 订单识别（2026-09-03 加入；识别三种特殊配送标品，数据=运单数据(新)生成时间维度）
    sp = data.get('special')
    sp_prod_labels = {'蜂鸟商家-众包': '众包', '全城送': '全城送', '开放平台-快送-一档': '开放平台快送'}
    if sp is None:
        sp_html = ('<div class="tabpane" data-pane="special"><div class="card">'
                   '<div style="padding:22px 18px;text-align:center;color:#888">'
                   '🔎 订单识别 数据加载中…（运单生成时间维度导出任务未就绪，下一轮刷新自动获取）</div></div></div>')
    elif not sp:
        sp_html = ('<div class="tabpane" data-pane="special"><div class="card">'
                   '<div class="card-hd" style="background:linear-gradient(135deg,#0D9488,#3DDCFF);color:#fff;border-radius:10px 10px 0 0">'
                   '<span>🔎 订单识别 · 当天特殊标品单</span>'
                   '<span class="sub" style="color:#e6fffa">识别：蜂鸟商家-众包 / 全城送 / 开放平台-快送-一档 · 数据源=运单数据(新)·运单生成时间</span></div>'
                   '<div style="padding:22px 18px;text-align:center;color:#888">'
                   '✅ 当天暂无这三种特殊标品订单</div></div></div>')
    else:
        cnt = {}
        for r in sp:
            p = r.get('product', '')
            cnt[p] = cnt.get(p, 0) + 1
        # 在途优先：待到店/待取餐/待送达 置顶（用户 2026-09-03 强调这两状态点重要），终态沉底
        _state_rank = {'待到店': 0, '待取餐': 1, '待送达': 2, '配送成功': 9, '配送失败': 9}
        _inway = {'待到店', '待取餐', '待送达'}
        _pill_of = {'待到店': 'ok', '待取餐': 'warn', '待送达': 'pass',
                    '配送成功': 'norm', '配送失败': 'fail'}
        sp_sorted = sorted(sp, key=lambda r: _state_rank.get(str(r.get('ostate') or '').strip(), 8))
        sp_trs = ''.join(
            (lambda st: '<tr' + (' class="row-inway"' if st in _inway else '') + '>'
             f'<td><b>{esc(r.get("knight") or "-")}</b></td>'
             f'<td>{esc(r.get("merchant") or "-")}</td>'
             f'<td style="white-space:nowrap">{esc(r.get("serial") or "-")}</td>'
             f'<td>{esc(r.get("otype") or "-")}</td>'
             f'<td><span class="pill {_pill_of.get(st, "norm")}">{esc(st)}</span></td>'
             f'<td><span class="pill pass">{esc(r.get("product") or "-")}</span></td>'
             f'<td>{esc(r.get("source") or "-")}</td></tr>')(str(r.get('ostate') or '').strip())
            for r in sp_sorted)
        inway_n = sum(1 for r in sp if str(r.get('ostate') or '').strip() in _inway)
        sp_html = f'''<div class="tabpane" data-pane="special">
  <div class="card">
    <div class="card-hd" style="background:linear-gradient(135deg,#0D9488,#3DDCFF);color:#fff;border-radius:10px 10px 0 0">
      <span>🔎 订单识别 · 当天特殊标品单</span>
      <span class="sub" style="color:#e6fffa">识别：蜂鸟商家-众包 / 全城送 / 开放平台-快送-一档 · 数据源=运单数据(新)·运单生成时间</span>
    </div>
    <div class="sumline">
      <div class="sumitem"><div class="v" style="color:#0D9488">{len(sp)}</div><div class="k">识别单总数</div></div>
      <div class="sumitem"><div class="v" style="color:#0D9488">{cnt.get("蜂鸟商家-众包", 0)}</div><div class="k">蜂鸟商家-众包</div></div>
      <div class="sumitem"><div class="v" style="color:#0D9488">{cnt.get("全城送", 0)}</div><div class="k">全城送</div></div>
      <div class="sumitem"><div class="v" style="color:#0D9488">{cnt.get("开放平台-快送-一档", 0)}</div><div class="k">开放平台-快送-一档</div></div>
      <div class="sumitem"><div class="v" style="color:#D97706">{inway_n}</div><div class="k">在途（待到店/待取餐/待送达）</div></div>
    </div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>骑手名称</th><th>平台商家名称</th><th>商家订单流水号</th><th>运单类型</th><th>运单状态</th><th>配送标品</th><th>订单来源</th></tr></thead>
      <tbody>
      {sp_trs}
      </tbody></table></div>
    <div style="padding:8px 18px;font-size:12px;color:#718096">口径：配送标品 ∈ 三种特殊标品（与考核剔除口径一致）· <span style="color:#0D9488">在途单（待到店/待取餐/待送达）置顶高亮</span>，终态沉底 · 数据 2 分钟缓存</div>
  </div>
  </div>'''

    js_code = '''<script>
    var cur = 'day';
    try { cur = localStorage.getItem('board_tab') || 'day'; } catch(e) {}
    function show(t) {
      cur = t;
      document.querySelectorAll('.tab').forEach(function(b) {
        b.classList.toggle('active', b.dataset.tab === t);
      });
      ['day','shift','level'].forEach(function(n) {
        var p = document.getElementById('pane-' + n);
        if (p) p.classList.toggle('active', n === t);
      });
      try { localStorage.setItem('board_tab', t); } catch(e) {}
    }
    function showShift(idx) {
      document.querySelectorAll('.shift-detail').forEach(function(d) { d.style.display = 'none'; });
      var el = document.getElementById('shift-detail-' + idx);
      if (el) el.style.display = 'block';
      document.querySelectorAll('.pvlink').forEach(function(b) {
        b.classList.toggle('active', b.dataset.idx == idx);
      });
    }
    var fstate = {cat: 'all', red: 'all', shift: 'all'};
    function applyFilter() {
      var rows = document.querySelectorAll('#pane-level tbody tr');
      rows.forEach(function(tr) {
        var show = true;
        if (fstate.cat !== 'all' && tr.dataset.cat !== fstate.cat) show = false;
        if (show && fstate.red === 'red' && tr.dataset.red !== '1') show = false;
        if (show && fstate.shift !== 'all' && tr.dataset.shift !== fstate.shift) show = false;
        tr.style.display = show ? '' : 'none';
      });
      document.querySelectorAll('.fbtn').forEach(function(b) {
        b.classList.toggle('active', fstate[b.dataset.group] === b.dataset.val);
      });
    }
    show(cur);
    document.querySelectorAll('.tab').forEach(function(b) {
      b.addEventListener('click', function() { show(b.dataset.tab); });
    });
    document.querySelectorAll('.fbtn').forEach(function(b) {
      b.addEventListener('click', function() {
        fstate[b.dataset.group] = b.dataset.val;
        applyFilter();
      });
    });
    document.querySelectorAll('.pvlink').forEach(function(b) {
      b.addEventListener('click', function() { openShiftModal(b.dataset.idx, b.closest('.site-block')); });
    });
    </script>'''.replace('__CUR_IDX__', str(cur_idx))
    return f'''<div class="site-block" data-pfx="{pfx}" data-tid="{tid}" data-curidx="{cur_idx}">
  <script>window.__ST = window.__ST || {{}}; window.__ST['{pfx}'] = {st_json};</script>
  <div class="site-hd"><span class="dt-badge">🕒 数据时间 {data['date']} {data['now']}</span><span class="meta2">当前时段：{esc(data['cur_shift'])}</span></div>

  <div class="tabs">
    <button class="tab active" data-tab="day">📊 全天达成</button>
    <button class="tab" data-tab="spike">⚡ 尖峰天贡献</button>
    <button class="tab" data-tab="special">🔎 订单识别</button>
    <button class="tab" data-tab="shift">⏱ 时段达成</button>
    <button class="tab" data-tab="level">🏅 档位达成</button>
    <button class="tab" data-tab="punctual">🕐 准时率</button>
    <button class="tab qc-tab" data-tab="qc">🔶 橙色风暴抽检</button>
    <button class="tab appeal-tab" data-tab="appeal">📋 不满意单明细</button>
    <button class="tab svc-tab" data-tab="service">⭐ 服务质量KPI</button>
  </div>

  <div class="tabpane active" data-pane="day">
  <div class="card">
    <div class="card-hd"><span>📊 全天达成</span><span class="sub">基准 {BH}h/{BO}单 · 出勤率≥{SCALE*100:.0f}% 为达标</span></div>
    <div class="verdict {rate_cls}">
      <div class="v-main">出勤率 {su['rate']:.1f}%<span class="v-tag">{f'✅ 出勤达标（≥{SCALE*100:.0f}%）' if su['rate'] >= SCALE * 100 else '⚠️ 出勤率不足' if su['rate'] >= 70 else '❌ 出勤未达标'}</span></div>
      <div class="v-sub">应排 {st_data.get('fee', {}).get('sched', 0)} 人 / 实排 {st_data.get('fee', {}).get('sched_actual', 0)} 人 · 达标 {su['passed']}/{st_data.get('fee', {}).get('sched', 0)} 人 · 达成率 {st_data.get('fee', {}).get('rate', 0):.1f}% · 加权出勤率 {st_data.get('fee', {}).get('aggr', 0):.1f}% · 计费 {st_data.get('fee', {}).get('billed', 0)} 人</div>
    </div>
    <div class="gapbar">
      <div class="gapcard"><div class="v info">{su['site_total']}</div><div class="k">站点人数</div></div>
      <div class="gapcard"><div class="v">{su['scheduled']}</div><div class="k">当天排班人数</div></div>
      <div class="gapcard"><div class="v">{su['sched_target']}<span class="gapn {'up' if su['sched_target'] - su['active'] <= 0 else 'down'}">缺口 {max(0, su['sched_target'] - su['active'])}</span></div><div class="k">应排人数</div></div>
      <div class="gapcard"><div class="v up">{su['active']}</div><div class="k">活跃人数</div></div>
    </div>
    <div class="kpirow">
      <div class="kpicard"><div class="v">{st_data.get('kpi', {}).get('percap', 0):.2f}</div><div class="k">人效（单/人）</div></div>
      <div class="kpicard"><div class="v">{st_data.get('kpi', {}).get('d18', '-')}<span class="sub2">({st_data.get('kpi', {}).get('d18_pct', 0):.1f}%)</span></div><div class="k">{BH}h{BO}单人数</div></div>
      <div class="kpicard"><div class="v" class="{'up' if st_data.get('kpi', {}).get('b2b5_pct', 0) > 70 else 'warnc' if st_data.get('kpi', {}).get('b2b5_pct', 0) >= 30 else 'down'}">{st_data.get('kpi', {}).get('b2b5_pct', 0):.1f}%</div><div class="k">标2-5占比（{st_data.get('kpi', {}).get('b2b5', 0)}人）</div></div>
      <div class="kpicard"><div class="v">{st_data.get('kpi', {}).get('net_orders', '-')}</div><div class="k">全天完成单（剔改派）</div></div>
      <div class="kpicard"><div class="v" class="down">{st_data.get('kpi', {}).get('transfer_riders', '-')}</div><div class="k">改派影响人数</div></div>
    </div>
    <div class="feebar">
      <span>💰 基础服务费：应排 <b>{st_data.get('fee', {}).get('sched', 0)}</b>｜门槛 <b>{st_data.get('fee', {}).get('threshold', 0)}</b>（{st_data.get('fee', {}).get('th_label', '95%')}）｜封顶 <b>{st_data.get('fee', {}).get('cap', 0)}</b>｜达标 <b>{st_data.get('fee', {}).get('passed', 0)}</b>｜达成率 <b class="{'up' if st_data.get('fee', {}).get('rate', 0) >= SCALE * 100 else 'down'}">{st_data.get('fee', {}).get('rate', 0):.1f}%</b>｜加权出勤率 <b class="{'up' if st_data.get('fee', {}).get('aggr', 0) >= PERIOD * 100 else '#B91C1C'}">{st_data.get('fee', {}).get('aggr', 0):.1f}%</b>｜计费 <b>{st_data.get('fee', {}).get('billed', 0)}</b>人｜高峰达成率 <b class="{'up' if ss.get('peak_rate', 0) >= 95 else 'down'}">{ss.get('peak_rate', 0):.1f}%</b>（门槛95%）｜<span class="pill {st_data.get('fee', {}).get('settle_cls', '')}">{st_data.get('fee', {}).get('settle', '')}</span></span>
    </div>
    <div class="scene-line">{esc(su['scene'])}</div>
    <div class="jf-line jf-{su['jf_cls']}">{esc(su['jf'])}</div>
    <div class="tf-line">📋 改派扣减合计：{su['transfer_total']}单</div>
    <div class="faildet">
      <div class="faildet-hd">📋 未达标骑手明细（{len(st_data.get('fail_rows', []))} 人）· 点表头可升/降序 ⇅</div>
      <table>
        <thead><tr><th>姓名</th><th>状态</th><th>配送中</th><th>剔除后完单</th><th>在线时长</th><th>差单量</th><th>差时长</th><th>最后班次</th></tr></thead>
        <tbody>
{st_fail_html}
        </tbody>
      </table>
    </div>
  </div>
  </div>

{spike_html}

  {sp_html}

  <div class="tabpane" data-pane="shift">
  <div class="card">
    <div class="card-hd"><span>⏱ 时段达成</span>
      <span class="sub">当前时段：{esc(ss['summary']['name'])} · 要求 {ss['summary']['req_hours']}h/{ss['summary']['req_orders']}单 · {'⏳进行中，剩' + str(int(ss['summary']['remain'] * 60)) + '分钟' if ss['summary']['ongoing'] else '已结束'}</span></div>
    {pv_html}
    <div class="judge" class="judge {'ok' if ss['summary']['judge_ok'] else 'bad'}">🏁 考核判定：{esc(ss['summary']['judge'])}</div>
    {shift_detail_blocks_str}
  </div>
  </div>

  <div class="tabpane" data-pane="level">
  <div class="card">
    <div class="card-hd"><span>🏅 档位达成</span><span class="sub">标准 {BH}h/{BO}单起 · 取低原则 · {'来源：月度运力标准 ' + std['month'] if std else '内置默认标准'}</span></div>
    <div class="sumline"><div style="padding:6px 2px">{lv_badges}</div></div>
    <div class="lqbar">
      <input class="lq-input" id="lq-input-{pfx}" placeholder="输入骑手姓名，实时查询档位（数据与看板同步）" onkeydown="if(event.key==='Enter')levelQuery(this)" />
      <button class="lq-btn" onclick="levelQuery(this)">🔍 实时查询</button>
      <span class="lq-hint">与看板同一数据源 · 秒出结果</span>
    </div>
    <div class="lq-result" id="lq-result-{pfx}"></div>
    <div class="filterbar">
      <span class="flabel">差距</span>
      <button class="fbtn active" data-group="cat" data-val="all">全部</button>
      <button class="fbtn" data-group="cat" data-val="chong">🟢 冲刺（差1-2单）</button>
      <button class="fbtn" data-group="cat" data-val="sheng">🟡 有机会（差3-5单）</button>
      <button class="fbtn" data-group="cat" data-val="fail">⚪ 未达成基本出勤（&lt;18单）</button>
      <span class="flabel">红名</span>
      <button class="fbtn active" data-group="red" data-val="all">全部</button>
      <button class="fbtn" data-group="red" data-val="red">🔥 改派/无效</button>
      <span class="flabel">班次（可多选）</span>
      <button class="fbtn active" data-group="shift" data-val="all">全部</button>
      <button class="fbtn" data-group="shift" data-val="晚高峰">晚高峰</button>
      <button class="fbtn" data-group="shift" data-val="夜宵1">夜宵1</button>
      <button class="fbtn" data-group="shift" data-val="夜宵2">夜宵2</button>
    </div>
    <div style="padding:2px 18px 8px;font-size:12px;color:#718096">图例：<span class="up">绿=差1-2单冲刺</span> · <span class="warnc">黄=差3-5单有机会</span> · <span class="down">红=基本未达成</span> · <span class="down">红名</span>=有改派/无效单</div>
    <table>
      <thead><tr><th>姓名</th><th>状态</th><th>档位</th><th>有效时长</th><th>全天完单</th><th>配送中</th><th>晋升档位</th><th>差单</th><th>晋升标准</th><th>最后班次</th></tr></thead>
      <tbody>
{lv_rows}
      </tbody>
    </table>
    <div style="padding:8px 18px;font-size:12px;color:#718096">全天完单=考核口径（已扣改派/无效）· <span class="down">红名</span>=有改派/无效单 · 差单 <span class="up">绿=差1-2单冲刺</span> <span class="warnc">黄=差3-5单</span> <span class="down">红=基本未达成</span> · 晋升档位：<span class="lvn-1">标1</span> <span class="lvn-2">标2</span> <span class="lvn-3">标3</span> <span class="lvn-4">标4</span> <span class="lvn-5">标5</span>（颜色=晋升目标档位）</div>
  </div>
  </div>

  <div class="tabpane" data-pane="punctual">
  <div class="card">
    <div class="card-hd"><span>🕐 骑手当天准时率排名</span><span class="sub"><span class="pt-info">每6分钟自动更新 · <span class="pt-cd" data-gen="{ptd.get('gen_at', 0)}">--:--</span></span> · 口径：配送成功剔兜底/二呼 · 送达晚于平台期望8分钟以上算超时</span></div>
    <div class="sumline">
      <div class="sumitem"><div class="v">{ptd['summary']['riders']}</div><div class="k">上榜骑手</div></div>
      <div class="sumitem"><div class="v">{ptd['summary']['total_valid']}</div><div class="k">有效单</div></div>
      <div class="sumitem"><div class="v" class="down">{ptd['summary']['total_over']}</div><div class="k">超时单</div></div>
      <div class="sumitem"><div class="v" class="{'up' if ptd['summary']['avg_rate'] >= 90 else 'warnc'}">{ptd['summary']['avg_rate']:.1f}%</div><div class="k">全站准时率</div></div>
    </div>
    <table>
      <thead><tr><th>排名</th><th>骑手</th><th>有效单</th><th>准时单</th><th>超时单</th><th>准时率</th></tr></thead>
      <tbody>
{pt_rows_html}
      </tbody>
    </table>
    <div style="padding:8px 18px;font-size:12px;color:#718096">准时率 <span class="up">绿=≥98%</span> <span class="warnc">黄=90-98%</span> <span class="down">红=&lt;90%</span> · 数据与 T-1 超时分析同口径</div>
  </div>
  </div>

  {qc_html}
  {appeal_html}
  {svc_html}
</div>'''

def lan_ip():
    """本机局域网 IP（手机/其他电脑访问用）"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip if ip and not ip.startswith('127.') else '127.0.0.1'
    except Exception:
        return '127.0.0.1'

def start_http_server(directory, port=8765):
    """启动本地 HTTP 服务（动态刷新用）：页面 fetch 最新看板，局部更新不整页重载。
    绑定 0.0.0.0，局域网内手机/其他电脑可访问；除本机外访问需输入密码
    （用户名 board，密码 BOARD_PASS，见使用说明）"""
    import base64
    from modules import share_gate as sg

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)
        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()
        def log_message(self, *a):
            pass
        def _pw_token(self):
            """密码 → cookie 令牌（不存明文密码；换密码后旧 cookie 立即失效）"""
            import hashlib
            return hashlib.sha256(('board-pw:' + _board_pass()).encode()).hexdigest()[:32]

        def _login_page(self, err: str = '') -> bytes:
            """只有密码框的登录页（2026-11-01 用户要求：不再让浏览器弹「账号+密码」）"""
            tip = f'<div class="err">{err}</div>' if err else ''
            return f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>配送运营助手 · 登录</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#eef2f7;font-family:"PingFang SC","Microsoft YaHei",-apple-system,sans-serif;color:#1f2937}}
.card{{width:min(360px,90vw);background:#fff;border-radius:16px;padding:30px 26px;
box-shadow:0 10px 30px rgba(15,23,42,.12)}}
h1{{font-size:19px;font-weight:700;margin-bottom:6px}}
p{{font-size:13px;color:#64748b;margin-bottom:20px}}
input{{width:100%;padding:13px 14px;font-size:16px;border:1px solid #cbd5e1;border-radius:10px;outline:none}}
input:focus{{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}}
button{{width:100%;margin-top:14px;padding:13px;font-size:16px;font-weight:600;color:#fff;
background:#2563eb;border:0;border-radius:10px;cursor:pointer}}
button:active{{background:#1d4ed8}}
.err{{margin-top:12px;font-size:13px;color:#dc2626}}
</style></head><body><form class="card" method="post" action="/login">
<h1>配送运营助手</h1><p>请输入访问密码</p>
<input type="password" name="pw" placeholder="密码" autofocus autocomplete="current-password" required>
<button type="submit">进入</button>{tip}</form></body></html>'''.encode('utf-8')

        def _send_login(self, status: int = 200, err: str = ''):
            body = self._login_page(err)
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_login(self):
            """/login：只校验密码，成功后写 cookie 并回首页"""
            from urllib.parse import parse_qs
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                pw = (parse_qs(raw.decode('utf-8', 'replace')).get('pw') or [''])[0]
            except Exception:
                pw = ''
            if pw and pw == _board_pass():
                self.send_response(302)
                self.send_header('Location', '/')
                self.send_header('Set-Cookie',
                                 f'board_pw={self._pw_token()}; Path=/; Max-Age=2592000; SameSite=Lax')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return self._send_login(401, '密码不对，再试一次')

        def _authed(self):
            """本机免密；非本机可用 cookie（/login 输入密码）或旧 Basic（board:密码）。
            密码实时读文件，页面改密码后立即生效（2026-08-29）。"""
            host = self.headers.get('Host', '')
            if host.startswith('127.0.0.1') or host.startswith('localhost'):
                return True
            import re
            m = re.search(r'board_pw=([^;]+)', self.headers.get('Cookie', '') or '')
            if m and m.group(1) == self._pw_token():
                return True
            auth = self.headers.get('Authorization', '')
            expect = 'Basic ' + base64.b64encode(f'board:{_board_pass()}'.encode()).decode()
            return auth == expect
        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def _handle_kpi_api(self):
            """GET/POST /api/kpi_targets：读取/保存/恢复 KPI 目标值（分发版使用者自己维护）"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.command == 'GET':
                self._send_json({'ok': True, 'targets': KPI_TARGETS})
                return
            # POST：body = {"targets": {...}} 保存 或 {"reset": true} 恢复默认
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                d = json.loads(raw.decode('utf-8')) if raw else {}
                if d.get('reset'):
                    _reset_kpi_targets()
                    self._send_json({'ok': True, 'msg': '已恢复内置默认目标值'})
                    return
                targets = d.get('targets')
                if not isinstance(targets, dict) or not targets:
                    self._send_json({'ok': False, 'err': '缺少 targets 数据'}, 400)
                    return
                n = _save_kpi_targets(targets)
                self._send_json({'ok': True, 'msg': f'已保存 {n} 个场景目标值'})
            except Exception as e:
                self._send_json({'ok': False, 'err': str(e)}, 400)
        def _handle_kpi_upload(self):
            """POST /api/kpi_targets/upload：上传 KPI 目标值 Excel（xlsx，raw body）→ 解析 → 保存"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                if len(raw) < 100:
                    self._send_json({'ok': False, 'err': '文件为空或不是有效的 xlsx'}, 400)
                    return
                tmp = Path(directory) / '_kpi_upload_tmp.xlsx'
                tmp.write_bytes(raw)
                try:
                    parsed = _parse_kpi_excel(str(tmp))
                finally:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                n = _save_kpi_targets(parsed)
                self._send_json({'ok': True, 'msg': f'✅ 解析成功，已保存 {n} 个场景目标值',
                                 'count': n})
            except Exception as e:
                self._send_json({'ok': False, 'err': str(e)}, 400)
        def _handle_kpi_template(self):
            """GET /api/kpi_template：下载 KPI 目标值 Excel 模板（当前目标值预填）"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                data = _make_kpi_template()
                import urllib.parse as _up
                self.send_response(200)
                self.send_header('Content-Type',
                                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                # 中文文件名必须 RFC5987 编码，否则响应头含非ASCII字节导致下载失败
                self.send_header('Content-Disposition',
                                 "attachment; filename=\"kpi_targets_template.xlsx\"; "
                                 "filename*=UTF-8''" + _up.quote('KPI目标值模板.xlsx'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({'ok': False, 'err': str(e)}, 500)
        def _handle_level_query(self):
            """POST /api/level_query {"name":"骑手名","team_id":123}
            档位速查：实时拉出勤/排班/运单（与看板同源，运单走2分钟缓存）/实时工作状态 → 算档位 → 返回单骑手行。
            数据源与 refresh_once 刷新循环完全一致（exportAttendance + listScheduleStatistics + fetch_order_stats），
            共用 9223 Chrome（CDP 多连接安全）；不强制新建运单导出任务（2026-08-29 用户要求改看板方式）。"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                d = json.loads(raw.decode('utf-8')) if raw else {}
                name = str(d.get('name', '')).strip()
                team_id = int(d.get('team_id') or 0)
                if not name:
                    return self._send_json({'ok': False, 'err': '请输入骑手姓名'}, 400)
                if not team_id:
                    return self._send_json({'ok': False, 'err': '缺少站点参数'}, 400)
            except Exception as e:
                return self._send_json({'ok': False, 'err': '请求格式错误: ' + str(e)}, 400)
            import shutil
            import tempfile
            # 档位查询需要实时连 9223 Chrome：先确保浏览器在（静默无头），再连接
            cdp_core.ensure_debug_chrome(headless=True)
            ws, cdp = cdp_core.connect()
            if not ws:
                return self._send_json({'ok': False, 'err': '浏览器未连接，请稍后重试'})
            tmp = Path(tempfile.mkdtemp(prefix='lvq_'))
            try:
                _ensure_aeolus_page(cdp)
                today = time.strftime('%Y-%m-%d')
                # 1. 实时出勤
                att = tmp / '出勤.xlsx'
                if not fetch_attendance(cdp, team_id, att):
                    return self._send_json({'ok': False, 'err': '出勤数据拉取失败'})
                riders, periods = parse_attendance(att)
                if name not in riders:
                    return self._send_json({'ok': False, 'err': f'未找到骑手「{name}」（不在出勤名单）'})
                # 2. 实时排班
                sched = fetch_schedule_stats(cdp, team_id, today)
                # 3. 月度运力标准（失败用内置默认）
                std = fetch_level_standard(cdp, team_id, today)
                LVS = (std or {}).get('levels') or LEVELS
                # 4. 运单改派/无效统计——与看板刷新同一数据源（复用今日导出任务+2分钟缓存，
                #    不强制新建导出任务等待；看板每60秒刷新也在写同一份缓存，秒回）
                order = fetch_order_stats(cdp, team_id, today,
                                          cache_path=Path(directory) / f'live_order_{team_id}.json',
                                          max_age=120, site_name=_SITE_NAMES.get(int(team_id)))
                # 5. 实时工作状态（失败回退出勤表状态）
                live_ws = fetch_live_workstatus(cdp, team_id)
                now = datetime.now()
                now_h = now.hour + now.minute / 60
                # 2026-11-06：改派/无效明细必须能显示，运单数据缺失时明说（缓存过期瞬间 combined 会为空）
                combined_d = (order or (None,) * 5)[0] or {}
                transfer_d = (order or (None,) * 5)[1] or {}
                row = level_row_for(name, riders[name], combined_d, transfer_d,
                                    periods, now_h, LVS)
                row['work'] = format_status(live_ws.get(name, row['work']))
                return self._send_json({'ok': True, 'row': row,
                                        'gen_at': now.strftime('%H:%M:%S'),
                                        'order_note': '' if combined_d else '运单数据暂缺（改派/无效未计入）'})
            except Exception as e:
                return self._send_json({'ok': False, 'err': '查询失败: ' + str(e)}, 500)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                shutil.rmtree(tmp, ignore_errors=True)
        def _handle_board_pass(self):
            """POST /api/board_pass {"pass":"新密码"}：改看板访问密码（手机/局域网访问用）"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                d = json.loads(raw.decode('utf-8')) if raw else {}
                newp = str(d.get('pass', '')).strip()
                if not (4 <= len(newp) <= 20):
                    return self._send_json({'ok': False, 'err': '密码长度需 4~20 位'}, 400)
                (Path(directory) / 'board_pass.txt').write_text(newp, encoding='utf-8')
                self._send_json({'ok': True, 'msg': '✅ 看板密码已更新，手机/其他设备请用新密码访问'})
            except Exception as e:
                self._send_json({'ok': False, 'err': str(e)}, 400)
        def _handle_check_update(self):
            """GET /api/check_update：查 Gitee 最新版本（看板页面「检测更新」按钮）"""
            if not self._authed():
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Board"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            try:
                from modules import update_check
                r = update_check.check_update(APP_VERSION, timeout=10)
                if r:
                    tag, url = r
                    self._send_json({'ok': True, 'current': APP_VERSION, 'latest': tag, 'url': url})
                else:
                    self._send_json({'ok': True, 'current': APP_VERSION, 'latest': None})
            except Exception as e:
                self._send_json({'ok': False, 'err': str(e)}, 500)

        # ── 预览分享（对外只读链接 /s/<token>）──────────────────────────
        def _share_token(self):
            import re
            m = re.search(r'board_share=([0-9a-f]{8,})', self.headers.get('Cookie', '') or '')
            return m.group(1) if m else None

        def _share_deny(self, code=403, msg='链接已失效'):
            body = (f'<!DOCTYPE html><meta charset="utf-8"><div style="font:15px/1.7 '
                    f'-apple-system,\'PingFang SC\',sans-serif;color:#64748b;padding:60px;'
                    f'text-align:center">{msg}</div>').encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_share_api(self):
            """/api/share：生成/列出/撤销「预览链接」（只读、限时、可溯源）
            GET  → {ok, links:[...], base}
            POST → {action:'new', hours, note} | {action:'revoke', token}
            """
            if not self._authed():
                self.send_response(401)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            import time as _t
            if self.command == 'GET':
                return self._send_json({'ok': True, 'links': sg.listing(), 'base': sg.share_base()})
            try:
                ln = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(ln) if ln else b''
                d = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception as e:
                return self._send_json({'ok': False, 'err': str(e)}, 400)
            act = d.get('action') or 'new'
            if act == 'revoke':
                ok = sg.revoke(str(d.get('token') or ''))
                return self._send_json({'ok': ok, 'msg': '已撤销' if ok else '没找到这条链接'})
            hours = float(d.get('hours') or 24)
            note = str(d.get('note') or '')[:20]
            sg.ensure_protected(force=True, min_interval=0)
            rec = sg.create(hours, note)
            exp = _t.strftime('%m-%d %H:%M', _t.localtime(rec['expire'])) if rec['expire'] else '永久'
            return self._send_json({'ok': True, 'url': f"{sg.share_base()}/s/{rec['token']}",
                                    'token': rec['token'], 'expire': exp})

        def _handle_share_entry(self, tok):
            """GET /s/<token>：校验 → 发预览 cookie → 跳工作台"""
            rec = sg.check(tok)
            if not rec:
                return self._share_deny(410, '链接已失效或已被撤销')
            left = int((rec.get('expire') or 0) - __import__('time').time()) if rec.get('expire') else 86400
            self.send_response(302)
            self.send_header('Location', '/workbench.html')
            self.send_header('Set-Cookie',
                             f'board_share={tok}; Path=/; Max-Age={max(left,60)}; SameSite=Lax')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def _serve_preview(self, rec, path):
            """预览会话：只给混淆产物 + 只读；原始 json/图片照给，报表类一律不给"""
            import os as _os
            if path == '/api/kpi_targets' and self.command == 'GET':
                # KPI 目标值只是考核门槛数字，预览里让「质量目标」页正常显示（只读）
                return self._send_json({'ok': True, 'targets': KPI_TARGETS})
            if path.startswith('/api/'):
                return self._share_deny(403, '预览模式只读')
            rel = (path.lstrip('/') or 'workbench.html')
            rel = _os.path.normpath(rel)
            if rel.startswith('..') or rel.startswith('_protected'):
                return self._share_deny(404, '没有这个页面')
            low = rel.lower()
            if low.startswith('reports/') or low.endswith(('.xlsx', '.xls', '.csv', '.zip')):
                return self._share_deny(403, '预览模式不提供文件下载')
            sg.ensure_protected()          # 有源码更新时重建混淆版（内部节流）
            pf = sg.protected_dir() / rel
            if rel in sg.PAGES and pf.exists():
                try:
                    body = pf.read_text(encoding='utf-8')
                    body = sg.watermark_html(rec, body)
                except Exception:
                    body = None
                if body is not None:
                    raw = body.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(raw)
                    return
            return super().do_GET()

        def do_GET(self):
            path = self.path.split('?')[0]
            if path.startswith('/s/'):
                return self._handle_share_entry(path[3:].strip('/'))
            _tok = self._share_token()
            if _tok:
                _rec = sg.check(_tok)
                if _rec:
                    return self._serve_preview(_rec, path)
            if path == '/api/kpi_targets':
                return self._handle_kpi_api()
            if path == '/api/kpi_template':
                return self._handle_kpi_template()
            if path == '/api/check_update':
                return self._handle_check_update()
            if path == '/api/share':
                return self._handle_share_api()
            if not self._authed():
                # 2026-11-01：不再回 Basic 401（浏览器会弹「账号+密码」框），改发只有密码框的登录页
                if path.startswith('/api/'):
                    self.send_response(401)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                return self._send_login(200)
            # 2026-09-27 工作台取代看板作为唯一入口：根路径 / 裸 index.html 一律跳工作台。
            # 必须只跳「不带查询串」的请求——工作台内嵌 iframe 走 index.html?embed=1、
            # 前端解析走 index.html?t=…，那些要继续拿到看板页面本身。
            if path in ('/', '/index.html', '/index.htm') and '?' not in self.path \
                    and (Path(self.directory) / 'workbench.html').exists():
                self.send_response(302)
                self.send_header('Location', '/workbench.html')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return super().do_GET()
        def do_POST(self):
            path = self.path.split('?')[0]
            if path == '/login':
                return self._handle_login()
            if path == '/api/kpi_targets/upload':
                return self._handle_kpi_upload()
            if path == '/api/kpi_targets':
                return self._handle_kpi_api()
            if path == '/api/level_query':
                return self._handle_level_query()
            if path == '/api/board_pass':
                return self._handle_board_pass()
            if path == '/api/share':
                return self._handle_share_api()
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
    for p in range(port, port + 5):
        try:
            httpd = http.server.ThreadingHTTPServer(('0.0.0.0', p), H)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            bp_var = _board_pass()
            ip = lan_ip()
            print(f'🌐 本机访问：http://127.0.0.1:{p}/（动态刷新）')
            print(f'🔑 看板账号：board ｜ 密码：{bp_var}')
            if p != port:
                print(f'⚠️⚠️ 端口 {port} 被占用，服务跑在 {p}！其他设备会连不上，'
                      f'请先释放 {port} 端口再重启看板')
            if ip != '127.0.0.1':
                print(f'📱 局域网访问（手机/其他电脑连同一个WiFi）：http://{ip}:{p}/')
            return httpd, p
        except OSError:
            continue
    return None, None

def render_multi(sites, interval):
    """组装完整看板页：sites={team_id: {'name':..., 'data':...}}"""
    bp_var = _board_pass()
    esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    blocks = []
    for i, (tid, s) in enumerate(sites.items()):
        blocks.append(render_html(s['data'], s['name'], interval, f's{i}', tid))
    first = next(iter(sites.values()))['data']
    # 站点选择器：下拉式（默认「全部站点」；选单个站点只看该站）
    site_opts = ['<option value="-1">全部站点</option>'] + [
        f'<option value="{i}">{esc(s["name"])}</option>' for i, (tid, s) in enumerate(sites.items())
    ]
    site_bar = ('<div class="sitebar">'
                + '<span class="slabel">站点</span>'
                + '<select class="ssel" id="site-select">'
                + ''.join(site_opts)
                + '</select>'
                + '</div>')
    js = '''<script>
// ── 暗色模式：苹果雾面玻璃（body[data-theme]，localStorage 记住；局部刷新后按钮文字由 fetchLatest 同步）──
function applyTheme(t) {
  var dark = (t === 'dark');
  document.body.setAttribute('data-theme', dark ? 'dark' : 'light');
  var b = document.getElementById('theme-btn');
  if (b) b.textContent = dark ? '☀️ 亮色' : '🌙 暗色';
}
function toggleTheme() {
  var dark = document.body.getAttribute('data-theme') === 'dark';
  var next = dark ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('board_theme', next); } catch(e) {}
}
var _th = null;
try { _th = localStorage.getItem('board_theme'); } catch(e) {}
applyTheme(_th || 'dark');   // 默认暗色（苹果雾面玻璃）；用户手动切过则记住选择
// ── 看板功能：设置密码 / 检测更新（2026-08-29 从菜单挪进页面）──
function openBoardPass() {
  var t = document.getElementById('modal-title');
  var b = document.getElementById('modal-body');
  if (!t || !b) return;
  t.textContent = '🔑 设置看板访问密码';
  b.innerHTML = '<div style="font-size:13px;color:#9AABC4;margin-bottom:10px">手机/其他设备访问看板用（用户名固定 board）。本机自己看不需密码。</div>'
    + '<input id="bp-input" class="lq-input" type="text" placeholder="输入新密码（4~20位）" style="width:280px;max-width:100%" />'
    + '<div style="margin-top:12px;display:flex;align-items:center;gap:8px">'
    + '<button class="fbtn" onclick="saveBoardPass()" style="font-weight:700">保存</button>'
    + '<button class="fbtn" onclick="closeModal()">取消</button>'
    + '<span id="bp-msg" style="font-size:12px"></span></div>';
  document.getElementById('modal-mask').style.display = 'flex';
  setTimeout(function(){ var i = document.getElementById('bp-input'); if (i) i.focus(); }, 50);
}
function saveBoardPass() {
  var inp = document.getElementById('bp-input');
  var msg = document.getElementById('bp-msg');
  if (!inp || !msg) return;
  var v = inp.value.trim();
  if (!(v.length >= 4 && v.length <= 20)) { msg.textContent = '❌ 需 4~20 位'; msg.style.color = '#B91C1C'; return; }
  fetch('/api/board_pass', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pass: v})})
    .then(function(r){ return r.json(); })
    .then(function(d){
      msg.textContent = d.ok ? d.msg : ('❌ ' + (d.err || '失败'));
      msg.style.color = d.ok ? '#3DDCFF' : '#B91C1C';
      if (d.ok) setTimeout(closeModal, 1600);
    })
    .catch(function(){ msg.textContent = '❌ 网络错误'; msg.style.color = '#B91C1C'; });
}
function checkUpdate() {
  var t = document.getElementById('modal-title');
  var b = document.getElementById('modal-body');
  if (!t || !b) return;
  t.textContent = '🔄 检测版本更新';
  b.innerHTML = '<div id="cu-msg" style="font-size:14px;padding:6px 0">正在检查…</div>';
  document.getElementById('modal-mask').style.display = 'flex';
  fetch('/api/check_update', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      var m = document.getElementById('cu-msg');
      if (!d.ok) { m.textContent = '❌ ' + (d.err || '检查失败'); return; }
      if (!d.latest || d.latest === d.current) {
        m.innerHTML = '✅ 当前已是最新版本（' + d.current + '）';
      } else {
        m.innerHTML = '📥 发现新版本 <b>' + d.latest + '</b>（当前 ' + d.current + '）<br>'
          + '<span style="font-size:12px;color:#9AABC4">启动程序时会自动更新；也可重新下载新版压缩包</span>';
      }
    })
    .catch(function(){ var m = document.getElementById('cu-msg'); if (m) m.textContent = '❌ 网络错误'; });
}
// ── 站点选择器：下拉式（默认全部站点；选单个站点只看该站，localStorage 记住）──
var siteCount = document.querySelectorAll('.site-block').length;
var selSites = [];
try {
  var _s = JSON.parse(localStorage.getItem('board_sites') || 'null');
  if (Array.isArray(_s)) selSites = _s.filter(function(x) { return x >= 0 && x < siteCount; });
} catch(e) {}
// 旧版多选状态只认单选；其余一律按「全部站点」
if (selSites.length !== 1) selSites = [];
function isSel(i) { return selSites.length === 0 || selSites.indexOf(i) >= 0; }
function paintSites() {
  document.querySelectorAll('.site-block').forEach(function(b, bi) {
    b.style.display = isSel(bi) ? '' : 'none';
  });
  var sel = document.getElementById('site-select');
  if (sel) sel.value = (selSites.length === 1 ? String(selSites[0]) : '-1');
  try { localStorage.setItem('board_sites', JSON.stringify(selSites)); } catch(e) {}
}
document.addEventListener('change', function(e) {
  if (e.target && e.target.id === 'site-select') {
    var v = parseInt(e.target.value, 10);
    selSites = (v === -1) ? [] : [v];
    paintSites();
  }
});
// 板块 tab（事件委托，作用域=最近站点块）
document.addEventListener('click', function(e) {
  var tab = e.target.closest('.tab');
  if (tab) {
    var block = tab.closest('.site-block');
    var t = tab.dataset.tab;
    block.querySelectorAll('.tab').forEach(function(b) {
      b.classList.toggle('active', b.dataset.tab === t);
    });
    block.querySelectorAll('.tabpane').forEach(function(p) {
      p.classList.toggle('active', p.dataset.pane === t);
    });
    try { localStorage.setItem('board_tab', t); } catch(e) {}
  }
});
// 时段名点击：进行中时段 → 内嵌展开/收起（明细在时段表下方）；已结束时段 → 弹窗
document.addEventListener('click', function(e) {
  var l = e.target.closest('.pvlink');
  if (l) {
    var block = l.closest('.site-block');
    var idx = l.dataset.idx;
    block.querySelectorAll('.pvlink').forEach(function(b) {
      b.classList.toggle('active', b.dataset.idx == idx);
    });
    if (l.dataset.ongoing === '1') {
      var el = block.querySelector('.shift-detail[data-shift="' + idx + '"]');
      if (el) {
        var showing = el.style.display !== 'none';
        block.querySelectorAll('.shift-detail').forEach(function(d) { d.style.display = 'none'; });
        if (!showing) el.style.display = 'block';
      }
    } else {
      openShiftModal(idx, block);
    }
  }
});
// 档位多条件筛选（每站点块独立状态；班次组支持多选）
function blockF(block) {
  if (!block._f) block._f = {cat: 'all', red: 'all', shift: []};
  return block._f;
}
function paintF(block, f) {
  block.querySelectorAll('.fbtn').forEach(function(x) {
    if (x.dataset.group === 'shift') {
      x.classList.toggle('active', x.dataset.val === 'all' ? f.shift.length === 0 : f.shift.indexOf(x.dataset.val) >= 0);
    } else {
      x.classList.toggle('active', f[x.dataset.group] === x.dataset.val);
    }
  });
}
function applyF(block, f) {
  block.querySelectorAll('table tbody tr[data-cat]').forEach(function(tr) {
    var show = true;
    if (f.cat !== 'all' && tr.dataset.cat !== f.cat) show = false;
    if (show && f.red === 'red' && tr.dataset.red !== '1') show = false;
    if (show && f.shift.length > 0 && f.shift.indexOf(tr.dataset.shift) < 0) show = false;
    tr.style.display = show ? '' : 'none';
  });
  // P2-5 空状态占位行已按用户要求移除（2026-09-26）：筛选后 0 行不再显示「无匹配骑手，试试清空筛选」。
  // 原因：旧逻辑在时段明细/准时率等没有 tr[data-cat] 的表上把行数误判成 0，每次自动刷新都往表尾插这一行，
  // 再点表头排序还会被排到最前面 —— 用户判定为无用提示，直接清理。
  // 这里只做兜底：把历史上可能残留在 DOM 里的那一行占位删掉（搜索框自己那行「🔍 无匹配骑手」不在此列，保留）
  block.querySelectorAll('table tbody tr.empty-row').forEach(function(r) {
    if (r.textContent.indexOf('试试清空筛选') >= 0) r.remove();
  });
}
document.addEventListener('click', function(e) {
  var b = e.target.closest('.fbtn');
  if (!b) return;
  var block = b.closest('.site-block');
  var f = blockF(block);
  var g = b.dataset.group;
  if (g === 'shift') {
    if (b.dataset.val === 'all') { f.shift = []; }
    else {
      var ix = f.shift.indexOf(b.dataset.val);
      if (ix >= 0) { f.shift.splice(ix, 1); } else { f.shift.push(b.dataset.val); }
    }
  } else {
    f[g] = b.dataset.val;
  }
  paintF(block, f);
  applyF(block, f);
});
// 页面加载：恢复上次的页签选择（整页重载后不丢）
var savedTab = 'day';
try { savedTab = localStorage.getItem('board_tab') || 'day'; } catch(e) {}
document.querySelectorAll('.site-block').forEach(function(block) {
  block.querySelectorAll('.tab').forEach(function(x) { x.classList.toggle('active', x.dataset.tab === savedTab); });
  block.querySelectorAll('.tabpane').forEach(function(p) { p.classList.toggle('active', p.dataset.pane === savedTab); });
});
paintSites();
// 时段明细弹窗（点击时段名打开；detail 内容保留在页内作为数据源）
function openShiftModal(idx, block) {
  var det = block ? block.querySelector('.shift-detail[data-shift="' + idx + '"]') : null;
  if (!det) return;
  var link = block.querySelector('.pvlink[data-idx="' + idx + '"]');
  var title = link ? link.textContent.trim() : '时段明细';
  document.getElementById('modal-title').textContent = '⏱ ' + title + ' · 明细';
  document.getElementById('modal-body').innerHTML = det.innerHTML;
  try { mobileTables(); } catch (e) {}
  document.getElementById('modal-mask').style.display = 'flex';
}
function closeModal() {
  document.getElementById('modal-mask').style.display = 'none';
}
// 站点维度弹窗：排班未出勤/出勤不达标明细
function openRiderModal(period, type, block) {
  var d = (window.__ST || {})[(block && block.dataset.pfx) || ''];
  var rows = d && d[period] ? d[period][type] : null;
  var title = (type === 'nosh' ? '排班未出勤' : '出勤不达标') + ' · ' + period;
  var html;
  if (!rows || !rows.length) {
    html = '<div style="padding:20px;color:#718096">✅ 该时段无' + title.split(' · ')[0] + '骑手</div>';
  } else {
    html = '<table><thead><tr>' + (type === 'nosh' ? '<th>骑手</th><th>配送中单量</th>' : '<th>骑手</th><th>在线时长</th><th>要求在线</th><th>完单</th><th>要求完单</th><th>差距</th>') + '</tr></thead><tbody>';
    rows.forEach(function(r) {
      if (type === 'nosh') {
        html += '<tr><td>' + r.n + '</td><td>' + r.d + '</td></tr>';
      } else {
        var gap = r.o < r.ro ? '差' + (r.ro - r.o) + '单' : (r.h < r.rh ? '差' + (r.rh - r.h) + 'h' : '✓');
        html += '<tr><td>' + r.n + '</td><td>' + r.h + '</td><td>' + r.rh + '</td><td>' + r.o + '</td><td>' + r.ro + '</td><td>' + gap + '</td></tr>';
      }
    });
    html += '</tbody></table>';
  }
  document.getElementById('modal-title').textContent = '⏱ ' + title + '（' + (rows ? rows.length : 0) + '人）';
  document.getElementById('modal-body').innerHTML = html;
  try { mobileTables(); } catch (e) {}
  document.getElementById('modal-mask').style.display = 'flex';
}
document.addEventListener('click', function(e) {
  var rl = e.target.closest('.rider-link');
  if (rl) { openRiderModal(rl.dataset.period, rl.dataset.rider, rl.closest('.site-block')); return; }
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeModal();
});
// 初始化每个站点块：默认时段高亮（明细走弹窗，不再内嵌展开）
document.querySelectorAll('.site-block').forEach(function(block) {
  var ci = parseInt(block.dataset.curidx || '0', 10);
  block.querySelectorAll('.pvlink').forEach(function(x) {
    x.classList.toggle('active', x.dataset.idx == ci);
  });
  initSort(block);
});
// ── 动态刷新：fetch 最新看板局部更新（保留筛选/站点/页签状态），失败退回整页刷新 ──
function pad2(n) { return n < 10 ? '0' + n : '' + n; }
function nowTs() { var d = new Date(); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds()); }
function showFeedback(txt) {
  var el = document.getElementById('fb');
  if (!el) return;
  el.textContent = txt;
  el.style.opacity = 1;
  el.style.background = '';
  el.style.color = '';
  clearTimeout(el._t);
  el._t = setTimeout(function() { el.style.opacity = 0; }, 3000);
}
// P0 数据时效告警：fetch 连续失败时红横幅常驻（显示的是旧数据，防误判），3 次升级醒目
var failCount = 0;
var lastOkTs = '';
function showStale() {
  var el = document.getElementById('fb');
  if (!el) return;
  clearTimeout(el._t);
  var n = failCount;
  el.textContent = (n >= 3 ? '‼️ ' : '⚠️ ') + '数据更新失败×' + n + ' · 当前显示 ' + (lastOkTs || '旧') + ' 的数据，自动重试中';
  el.style.background = (n >= 3) ? '#B91C1C' : '#C05621';
  el.style.color = '#fff';
  el.style.opacity = 1;
  el.style.padding = '2px 10px';
  el.style.borderRadius = '999px';
  el.style.fontWeight = '700';
}
function saveUI() {
  var st = {sites: selSites.slice(), tabs: {}, shifts: {}, filters: {}, sorts: {}};
  document.querySelectorAll('.site-block').forEach(function(b, bi) {
    var tab = b.querySelector('.tab.active');
    st.tabs[bi] = tab ? tab.dataset.tab : 'day';
    var f = {};
    b.querySelectorAll('.fbtn.active').forEach(function(x) {
      if (x.dataset.group === 'shift') {
        if (x.dataset.val !== 'all') {  // '全部' 表示无选择，不收集
          if (!f.shift) f.shift = [];
          f.shift.push(x.dataset.val);
        }
      } else {
        f[x.dataset.group] = x.dataset.val;
      }
    });
    if (!f.shift) f.shift = [];
    st.filters[bi] = f;
    // 排序状态：每张可排序列当前 表头索引+方向（刷新后恢复，解决排序老失效）
    var sorts = [];
    b.querySelectorAll('table:not(.pvtable)').forEach(function(tbl) {
      var cur = {th: -1, dir: 1};
      tbl.querySelectorAll('thead th').forEach(function(x, xi) {
        if (x.dataset.dir) { cur.th = xi; cur.dir = (x.dataset.dir === 'asc') ? 1 : -1; }
      });
      sorts.push(cur);
    });
    st.sorts[bi] = sorts;
  });
  return st;
}
function restoreUI(st) {
  if (st && st.sites) { selSites = st.sites.filter(function(x) { return x >= 0 && x < siteCount; }); }
  if (selSites.length !== 1) selSites = [];   // 下拉单选：非单选一律视为全部站点
  document.querySelectorAll('.site-block').forEach(function(b, bi) {
    var t = (st.tabs && st.tabs[bi]) || 'day';
    b.querySelectorAll('.tab').forEach(function(x) { x.classList.toggle('active', x.dataset.tab === t); });
    b.querySelectorAll('.tabpane').forEach(function(p) { p.classList.toggle('active', p.dataset.pane === t); });
    var f = (st.filters && st.filters[bi]) || {cat: 'all', red: 'all', shift: []};
    f.shift = (f.shift === 'all' ? [] : (Array.isArray(f.shift) ? f.shift : [f.shift])).filter(function(v) { return v !== 'all'; });
    paintF(b, f);
    applyF(b, f);
    initSort(b);
    // 恢复排序状态（initSort 之后逐表应用，防刷新后排序丢失）
    var sorts = (st.sorts && st.sorts[bi]) || [];
    b.querySelectorAll('table:not(.pvtable)').forEach(function(tbl, ti) {
      var s = sorts[ti];
      if (s && s.th >= 0) applySort(tbl, s.th, s.dir);
    });
  });
  paintSites();
  try { mobileTables(); } catch (e) { console.warn('手机端卡片化失败：', e); }
}
function fetchLatest(manual) {
  return fetch(location.pathname + '?t=' + Date.now(), {cache: 'no-store', credentials: 'include'})
    .then(function(r) { return r.text(); })
    .then(function(html) {
      if (html.indexOf('site-block') < 0) return false;  // 只认看板页本体，防目录列表/错误页误替换
      var m = html.match(/<body[^>]*>([\\s\\S]*?)<\\/body>/i);
      if (!m) return false;
      // 版本戳校验：服务端 JS/CSS 版本 ≠ 当前页面版本 → 整页刷新，让新 JS 立即生效
      var _bv = (html.match(/data-board-ver="([^"]*)"/) || [])[1] || '';
      var _cv = document.body.getAttribute('data-board-ver') || '';
      if (_bv && _cv && _bv !== _cv) { location.reload(); return true; }
      serverMode = true;   // fetch 成功 → 本地服务模式，绝不整页刷新
      // P1-4 刷新不打断：弹窗打开时跳过本次替换，只更新时间（避免正在看的明细被关掉）
      var mask = document.getElementById('modal-mask');
      if (mask && mask.style.display === 'flex') {
        countdown = CD_SEC;
        showFeedback('⏸ 弹窗打开中，本次刷新已跳过');
        return true;
      }
      var scrollY = window.scrollY;
      // UI 应用单独 try/catch：UI 异常只 console.warn，不再被外层 .catch 吞成「⚠️ 数据更新失败」红条
      try {
        var st = saveUI();
        document.body.innerHTML = m[1];
        restoreUI(st);
        applyTheme(document.body.getAttribute('data-theme'));   // 刷新后重建 titlebar，按钮文字跟随当前主题
        window.scrollTo(0, scrollY);
      } catch (e) { console.warn('UI 应用异常（数据其实已更新）：', e); }
      failCount = 0;                       // 刷新成功：清零失败计数
      lastOkTs = nowTs();                  // 记录本次成功时间（供失败时提示数据新旧）
      showFeedback((manual ? '✓ 已刷新 ' : '✓ 已更新 ') + nowTs());
      countdown = CD_SEC;
      return true;
    })
    .catch(function() {
      if (serverMode === null) {
        serverMode = false;   // file:// 直开确认：fetch 被 CORS 拦
        // 退回整页定时刷新（唯一会整页刷新的场景；服务模式下绝不整页刷新）
        setInterval(function () { location.reload(); }, CD_SEC * 1000);
      } else if (serverMode === true) {
        // 服务模式失败：红横幅常驻提示显示的是旧数据（P0 防误判），继续自动重试
        failCount++;
        showStale();
        countdown = CD_SEC;
      }
      return false;
    });
}
function manualRefresh() {
  fetchLatest(true).then(function(ok) { if (!ok) location.reload(); });
}
// ── 进行中时段明细：搜索骑手姓名（输入即过滤，空状态提示）──
function searchShift(inp) {
  var div = inp.closest('.shift-detail');
  if (!div) return;
  var kw = inp.value.trim();
  div.querySelectorAll('table tbody').forEach(function(tb) {
    var shown = 0;
    tb.querySelectorAll('tr').forEach(function(tr) {
      var td = tr.querySelector('td');
      var name = td ? td.textContent.trim() : '';
      var ok = !kw || name.indexOf(kw) >= 0;
      tr.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    var empty = tb.querySelector('tr.empty-row');
    if (shown === 0) {
      if (!empty) {
        empty = document.createElement('tr');
        empty.className = 'empty-row';
        var colspan = tb.querySelector('tr') ? tb.querySelector('tr').children.length : 8;
        empty.innerHTML = '<td colspan="' + colspan + '" style="padding:16px;color:#718096">🔍 无匹配骑手</td>';
        tb.appendChild(empty);
      }
    } else if (empty) {
      empty.remove();
    }
  });
}
// ── 尖峰天贡献明细：搜索（姓名/状态/班次/差单量，输入即过滤）──
function searchSpike(inp) {
  var card = inp.closest('.card');
  if (!card) return;
  var kw = inp.value.trim().toLowerCase();
  var tb = card.querySelector('.faildet table tbody');
  if (!tb) return;
  var shown = 0;
  tb.querySelectorAll('tr').forEach(function(tr) {
    if (tr.classList.contains('empty-row')) return;
    var ok = !kw || (tr.textContent || '').toLowerCase().indexOf(kw) >= 0;
    tr.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  var empty = tb.querySelector('tr.empty-row');
  if (shown === 0) {
    if (!empty) {
      empty = document.createElement('tr');
      empty.className = 'empty-row';
      var colspan = (tb.querySelector('tr') ? tb.querySelector('tr').children.length : 9);
      empty.innerHTML = '<td colspan="' + colspan + '" style="padding:16px;color:#718096">🔍 无匹配骑手</td>';
      tb.appendChild(empty);
    }
    empty.style.display = '';
  } else if (empty) {
    empty.style.display = 'none';
  }
}
// ── 表头排序持久化（2026-09-11：状态存「表头签名」，不存位置——刷新后表的数量/顺序会变）──
// ⚠️ localStorage 键必须写字面量 'board_sorts'，别用变量（页面初始化早于变量赋值 → 读成 undefined 静默失效）
function tableKey(tbl) {
  var ths = tbl.querySelectorAll('thead th');
  var names = [];
  for (var i = 0; i < ths.length; i++) {
    names.push((ths[i].textContent || '').replace(/[⇅▲▼]/g, '').trim());
  }
  return names.join('|');
}
function loadSorts() {
  try { return JSON.parse(localStorage.getItem('board_sorts') || '{}') || {}; } catch (e) { return {}; }
}
function persistSorts() {
  var o = {};
  document.querySelectorAll('.site-block').forEach(function(b, bi) {
    b.querySelectorAll('table:not(.pvtable)').forEach(function(tbl) {
      var ths = tbl.querySelectorAll('thead th');
      for (var i = 0; i < ths.length; i++) {
        if (ths[i].dataset.dir) { o[bi + '::' + tableKey(tbl)] = {th: i, dir: ths[i].dataset.dir}; break; }
      }
    });
  });
  try { localStorage.setItem('board_sorts', JSON.stringify(o)); } catch (e) {}
}
function restoreSorts() {
  var o = loadSorts();
  document.querySelectorAll('.site-block').forEach(function(b, bi) {
    b.querySelectorAll('table:not(.pvtable)').forEach(function(tbl) {
      var s = o[bi + '::' + tableKey(tbl)];
      if (s && typeof s.th === 'number') {
        try { applySort(tbl, s.th, s.dir === 'asc' ? 1 : -1); } catch (e) { console.warn(e); }
      }
    });
  });
}
// ── 表头升降序排序（事件委托；作用于所有非横向表，含时段明细/档位/准时率）──
function cellVal(td) {
  if (!td) return '';
  var txt = td.textContent.trim();
  var m = txt.match(/(\\d+)小时(?:( \\d+)分钟)?/);
  if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2] || '0', 10);  // 时长→分钟
  var n = parseFloat(txt.replace(/[^0-9.\\-]/g, ''));
  return isNaN(n) ? txt : n;
}
function initSort(block) {
  block.querySelectorAll('table:not(.pvtable) thead th').forEach(function(th) {
    th.setAttribute('data-sort', '1');
    th.title = th.title || '点击表头排序';
    var sp = th.querySelector('.srt');
    if (!sp) {
      th.insertAdjacentHTML('beforeend', ' <span class="srt">⇅</span>');   // 图标常显，刷新后仍在
    } else if (!sp.textContent) {
      sp.textContent = '⇅';
    }
  });
}
function applySort(table, thIdx, dir) {
  try {
  var ths = table.querySelectorAll('thead th');
  if (thIdx < 0 || thIdx >= ths.length) return;
  var tbody = table.querySelector('tbody');
  if (!tbody) return;
  // ⚠️ 2026-09-26 修：“无匹配骑手”占位行(tr.empty-row)不参与排序，排序后强制置底。
  //    旧逻辑会把它当普通行排序（它只有一个 colspan 单元格→取值空串），一点表头就把它排到最前面。
  var empties = [];
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr')).filter(function(r) {
    if (r.classList.contains('empty-row')) { empties.push(r); return false; }
    return true;
  });
  rows.sort(function(a, b) {
    var va = cellVal(a.children[thIdx]), vb = cellVal(b.children[thIdx]);
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    if (va === vb) return 0;
    return String(va).localeCompare(String(vb), 'zh') * dir;
  });
  rows.forEach(function(r) { tbody.appendChild(r); });
  empties.forEach(function(r) { tbody.appendChild(r); });   // 占位行留在最后
  ths.forEach(function(x) {
    delete x.dataset.dir;
    var sp = x.querySelector('.srt');
    if (sp) { sp.textContent = '⇅'; sp.classList.remove('on'); }   // 未排序=灰色 ⇅
  });
  var th = ths[thIdx];
  th.dataset.dir = (dir === 1) ? 'asc' : 'desc';
  var sp = th.querySelector('.srt');
  if (sp) { sp.textContent = (dir === 1) ? '▲' : '▼'; sp.classList.add('on'); }
  } catch (e) { console.warn('applySort 跳过：', e); }
}
document.addEventListener('click', function(e) {
  var th = e.target.closest('th[data-sort]');
  if (!th) return;
  var table = th.closest('table');
  var idx = Array.prototype.indexOf.call(table.querySelectorAll('thead th'), th);
  var dir = (th.dataset.dir === 'asc') ? -1 : 1;  // asc→降序，无/desc→升序
  applySort(table, idx, dir);
  persistSorts();
});
// 倒计时驱动动态刷新：每秒走表，归零自动更新（本地服务模式生效，只换数据不整页刷新）
var serverMode = null;   // null=未知 true=本地服务(fetch可用) false=file直开(fetch被CORS拦)
var CD_SEC = __INTERVAL_SEC__;
var countdown = CD_SEC;
var autoOn = true;
function tick() {
  if (!autoOn) return;   // 暂停时不走表不刷新
  countdown--;
  if (countdown <= 0) {
    fetchLatest(false);
    countdown = CD_SEC;
  }
  var el = document.getElementById('cd');
  if (el) el.textContent = pad2(Math.floor(countdown / 60)) + ':' + pad2(countdown % 60);
}
function toggleAuto() {
  autoOn = !autoOn;
  var b = document.getElementById('auto-btn');
  if (b) b.textContent = autoOn ? '▶ 自动刷新' : '▶ 继续刷新';
}
setInterval(tick, 1000);
// 头部时钟：每秒走针（2026-09-02 用户指出头部时间静止——原为刷新时写死的快照，60s 才跳一次）
function updateClock() {
  var el = document.getElementById('clock');
  if (!el) return;
  var d = new Date();
  el.textContent = d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
                   pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}
setInterval(updateClock, 1000);
// 准时率更新倒计时（周期 2 分钟 = 90s 缓存 + 30s 余量；实测导出任务提交→就绪 2.3s，2026-09-05 从 6min 收紧）
setInterval(function () {
  document.querySelectorAll('.pt-cd').forEach(function (el) {
    var gen = parseInt(el.dataset.gen || '0', 10);
    if (!gen) { el.textContent = '更新中…'; return; }
    var left = Math.max(0, (gen + 2 * 60 * 1000) - Date.now());
    el.textContent = left > 0 ? ('下次更新 ' + pad2(Math.floor(left / 60000)) + ':' + pad2(Math.floor(left / 1000) % 60)) : '更新中…';
  });
}, 1000);
// 时段模式（只看当前时段列）2026-09-26 停用：页面上早已没有 pd-auto/pd-all 切换按钮，
// 但这里还在读 localStorage['board_pd'] —— 浏览器里存着 'auto' 的人，时段达成表只剩
// 「当前时段 + 全天」3 列且无从切回（用户报「我一刷新就变成这样了/乱了」）。
// 现固定显示全列。要恢复该功能：必须同时把 pd-auto / pd-all 两个按钮加回时段卡片头部。
var pdMode = 'all';
function paintPd() {
  document.querySelectorAll('.site-block').forEach(function(block) {
    var tbl = block.querySelector('.pvtable');
    if (!tbl) return;
    var cur = parseInt(block.dataset.curidx || '0', 10);
    tbl.querySelectorAll('tr').forEach(function(tr) {
      var cells = tr.querySelectorAll('td, th');
      for (var i = 1; i < cells.length; i++) {
        var isCur = (i - 1) === cur;
        var isAll = cells[i].classList && cells[i].classList.contains('col-all');
        cells[i].style.display = (pdMode === 'auto' && !isCur && !isAll) ? 'none' : '';
      }
    });
  });
}
function setPdMode(m) {
  pdMode = m;
  try { localStorage.setItem('board_pd', m); } catch(e){}
  var a = document.getElementById('pd-auto'), b = document.getElementById('pd-all');
  if (a) a.classList.toggle('active', m === 'auto');
  if (b) b.classList.toggle('active', m === 'all');
  paintPd();
}
setPdMode(pdMode);
// ⚙ KPI指标配置（照 RiderMonitor：上传 Excel 更新目标值，保存到 data/kpi_targets.json）
var KPI_SCENARIOS = ['周中正常天','周中轻微恶天','周中一般恶天','周中严重恶天1','周中严重恶天2','周中严重恶天3','周末正常天','周末轻微恶天','周末一般恶天','周末严重恶天1','周末严重恶天2','周末严重恶天3'];
var KPI_GROUPS = [['delivery','物流妥投率'],['t8','考核准时率'],['complaint','不满意率'],['overtime','复合超时时长']];
function openKpiCfg() {
  fetch('/api/kpi_targets', {headers:{'Accept':'application/json'}})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d || !d.ok){ alert('读取KPI目标值失败'); return; }
      var t = d.targets || {};
      // 当前目标值表（只读展示）
      var html = '<div style="margin-bottom:12px;font-size:12px;color:#666">当前KPI目标值（上传Excel后可修改）</div>'
        + '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:12px;min-width:960px">'
        + '<thead><tr style="background:#13c2c2;color:#fff"><th style="padding:6px 10px;position:sticky;left:0;background:#13c2c2">场景分层</th>';
      KPI_GROUPS.forEach(function(g){ html += '<th colspan="3" style="padding:6px 10px">'+g[1]+'（0分/60分/100分）</th>'; });
      html += '</tr></thead><tbody>';
      KPI_SCENARIOS.forEach(function(sc){
        var v = t[sc] || {};
        html += '<tr><td style="padding:6px 10px;font-weight:600;white-space:nowrap;position:sticky;left:0;background:#fff">'+sc+'</td>';
        KPI_GROUPS.forEach(function(g){
          var tg = v[g[0]] || {};
          ['t0','t60','t100'].forEach(function(k){
            var val = tg[k]!=null ? tg[k] : '-';
            var disp = (g[0]==='overtime') ? val : (Number(val)*100).toFixed(2)+'%';
            html += '<td style="padding:6px 10px;text-align:right;border-bottom:1px solid #f0f0f0">'+disp+'</td>';
          });
        });
        html += '</tr>';
      });
      html += '</tbody></table></div>'
        // 上传区域（照 RiderMonitor：虚线框 + 选择Excel文件）
        + '<div style="margin-top:14px;background:#f6ffed;border:2px dashed #b7eb8f;border-radius:8px;padding:18px 20px;text-align:center">'
        + '<div style="font-size:14px;font-weight:600;color:#389e0d;margin-bottom:6px">📤 上传Excel更新KPI目标值</div>'
        + '<div style="font-size:11px;color:#666;margin-bottom:12px">格式：第一列=场景分层，后12列=4指标（物流妥投/考核准时率/不满意率/复合超时时长）的0分/60分/100分目标值 · 支持 .xlsx · 填小数（0.98=98%），超时填秒</div>'
        + '<div style="display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap">'
        + '<button class="rbtn" onclick="document.getElementById(\\'kpi-file\\').click()" style="background:#52c41a;color:#fff;border:none;padding:9px 24px;border-radius:6px;cursor:pointer;font-weight:700">📁 选择Excel文件</button>'
        + '<button class="rbtn" onclick="downloadKpiTpl()" style="background:#fff;color:#13c2c2;border:1px solid #13c2c2;padding:9px 24px;border-radius:6px;cursor:pointer">⬇ 下载模板</button>'
        + '<button class="rbtn" onclick="resetKpiCfg()" style="background:#fff;color:#B91C1C;border:1px solid #B91C1C;padding:9px 24px;border-radius:6px;cursor:pointer">↺ 恢复内置默认</button>'
        + '<input type="file" id="kpi-file" accept=".xlsx" style="display:none" onchange="uploadKpiFile(this)">'
        + '</div>'
        + '<div id="kpi-upload-msg" style="margin-top:10px;font-size:12px;color:#718096"></div>'
        + '</div>';
      document.getElementById('modal-title').textContent = '⚙ KPI指标管理 · 上传Excel修改每月考核目标值';
      document.getElementById('modal-body').innerHTML = html;
      document.getElementById('modal-mask').style.display = 'flex';
    }).catch(function(e){ alert('读取失败: '+e.message); });
}
function downloadKpiTpl(){
  fetch('/api/kpi_template')
    .then(function(r){
      if(!r.ok) throw new Error('下载失败');
      return r.blob();
    })
    .then(function(b){
      var a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = 'KPI目标值模板.xlsx';
      document.body.appendChild(a); a.click();
      setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    }).catch(function(e){ alert(e.message); });
}
function uploadKpiFile(inp){
  var msg = document.getElementById('kpi-upload-msg');
  if(!msg) return;
  var f = inp.files && inp.files[0];
  if(!f) return;
  msg.innerHTML = '⏳ 正在上传解析…';
  fetch('/api/kpi_targets/upload', {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body: f})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){ msg.innerHTML = d.msg; openKpiCfg(); if(typeof manualRefresh==='function') manualRefresh(); }
      else { msg.innerHTML = '❌ '+(d.err||'解析失败'); }
    }).catch(function(e){ msg.innerHTML = '❌ '+e.message; });
  inp.value = '';
}
function resetKpiCfg(){
  var msg = document.getElementById('kpi-upload-msg');
  if(!msg) return;
  if(!confirm('确定恢复内置默认目标值？')) return;
  fetch('/api/kpi_targets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({reset:true})})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){ msg.innerHTML = '✅ '+d.msg; openKpiCfg(); if(typeof manualRefresh==='function') manualRefresh(); }
      else { msg.innerHTML = '❌ '+(d.err||'重置失败'); }
    }).catch(function(e){ msg.innerHTML = '❌ '+e.message; });
}
// ── 档位速查：单人实时查询（2026-08-27 加入，对齐档位查询技能 rider_level_query）──
function escHtml(s){
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmtDur(h){
  var m = Math.round((h||0)*60);
  if(m <= 0) return '0分钟';
  var hh = Math.floor(m/60), mm = m%60;
  if(hh === 0) return mm+'分钟';
  if(mm === 0) return hh+'小时';
  return hh+'小时'+mm+'分钟';
}
function levelQuery(btn){
  var block = btn.closest('.site-block');
  var inp = block.querySelector('.lq-input');
  var out = block.querySelector('.lq-result');
  var name = (inp.value||'').trim();
  if(!name){ out.innerHTML = '<span class="warnc">请输入骑手姓名</span>'; inp.focus(); return; }
  var tid = parseInt(block.dataset.tid || '0', 10);
  btn.disabled = true;
  out.innerHTML = '⏳ 查询中…';
  fetch('/api/level_query', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:name, team_id:tid})})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.ok){
        var r = d.row;
        var lvCls = r.level >= 5 ? 'pass' : (r.level > 0 ? 'ok' : 'fail');
        var gapCls = r.tcls === 'fail' ? 'down' : (r.tcls === 'warn' ? 'warnc' : (r.tcls === 'pass' ? 'up' : 'mutedc'));
        var gapTxt = r.level >= 5 ? '已满档' : r.tag;
        var ded = Math.max(0, (r.raw_orders || 0) - (r.orders || 0));
        var invH = r.invalid_hours || 0;
        var trf = r.transfer || 0, inv = r.invalid || 0;
        // 串行展示：一行一个指标（2026-11-06 用户要求：无效时长、改派/无效单必须显示，且竖排串行）
        function lqline(k, v, cls, sub){
          return '<div class="lq-line"><span class="k">' + k + '</span>'
               + '<span class="v ' + (cls || '') + '">' + v
               + (sub ? '<i class="lq-sub">' + sub + '</i>' : '') + '</span></div>';
        }
        out.innerHTML = '<div class="lq-card">'
          + '<div class="lq-hd">'
          +   '<span class="pill ' + lvCls + '">' + escHtml(r.level_label) + '</span>'
          +   '<span class="nm">' + escHtml(r.name) + '</span>'
          +   '<span>' + escHtml(r.work) + '</span>'
          +   '<span class="lq-ts">实时 ' + escHtml(d.gen_at) + '</span>'
          + '</div><div class="lq-lines">'
          + lqline('有效时长', fmtDur(r.hours), '', '在线 ' + fmtDur(r.online_hours))
          + lqline('无效在线', invH > 0 ? fmtDur(invH) : '无', invH > 0 ? 'deduct' : 'zero')
          + lqline('全天完单', escHtml(r.raw_orders) + ' 单'
                   + (ded > 0 ? ' → 剔除后 <b>' + escHtml(r.orders) + ' 单</b>' : ''),
                   ded > 0 ? 'deduct' : '')
          + lqline('├ 改派单', escHtml(trf) + ' 单', trf > 0 ? 'deduct' : 'zero')
          + lqline('└ 无效单', escHtml(inv) + ' 单', inv > 0 ? 'deduct' : 'zero')
          + lqline('配送中', escHtml(r.delivering) + ' 单')
          + lqline('晋升标准', escHtml(r.std_txt))
          + lqline('差距', escHtml(gapTxt), gapCls)
          + lqline('最后班次', escHtml(r.last_shift), '', '班次差单 ' + escHtml(r.shift_gap))
          + '</div></div>';
        if(d.order_note){
          out.innerHTML += '<div class="warnc" style="margin-top:4px;font-size:11px">⚠️ ' + escHtml(d.order_note) + '（改派/无效扣减可能不准）</div>';
        }
      } else {
        out.innerHTML = '<span style="color:#C53030">❌ ' + escHtml(d.err || '查询失败') + '</span>';
      }
    })
    .catch(function(e){ out.innerHTML = '<span style="color:#C53030">❌ ' + escHtml(e.message) + '</span>'; })
    .then(function(){ btn.disabled = false; });
}
// 页面载入：恢复已保存的表头排序（按表头签名匹配）
try { restoreSorts(); } catch (e) { console.warn(e); }

/* ════════ 手机端（≤640px）方案A：宽表→卡片 + 时段表转置 ════════
   设计依据见技能 references/mobile-table-patterns.md（NN/g / uxpatterns.dev / ArcGIS 手机规范：
   手机端禁止用缩小字号塞列；明细表按「一条记录一张卡」呈现，次要字段折叠可展开）。
   实现要点：DOM 仍是真表格（筛选/排序照旧生效），只靠 data-label + CSS 把 tr 变成卡片，
   所以 60s 局部刷新（body.innerHTML 整换 → restoreUI）后重跑一次即可，无需改服务端。 */
var M2 = ['晋升标准', '最后班次', '骑手ID', '抽检类型', '抽检日期', '违规时间', '剩余申诉时间', '分类', '违规类型',
          '商家订单流水号', '运单类型', '订单来源', '平台商家名称', '要求单', '要求时', '差时', '排名',
          '要求在线', '完单', '要求完单', '差距', '运单号'];
var MHERO = ['全天完单', '剔除后完单', '准时率', '实完单', '有效时长', '在线时长'];
var MTITLE = ['姓名', '骑手', '骑手名称'];
var MCHIP = ['档位', '判定', '申诉状态', '运单状态', '配送标品', '抽检状态', '状态'];
function isMobile() { return window.innerWidth <= 640; }
function stampLabels() {
  document.querySelectorAll('table').forEach(function(tb) {
    if (tb.classList.contains('pvtable') || tb.classList.contains('m-nostack')) return;
    if (tb.style && tb.style.minWidth) return;   // KPI指标配置表（场景×指标矩阵，min-width:960px）不转卡片
    var ths = tb.querySelectorAll('thead th');
    if (!ths.length) return;
    var labels = [];
    ths.forEach(function(th) { labels.push(th.textContent.replace(/[⇅↕\\s]/g, '').trim()); });
    tb.querySelectorAll('tbody td').forEach(function(td) {
      if (!td.dataset.label) td.dataset.label = labels[td.cellIndex] || '';
    });
  });
}
function mobileTables() {
  if (!isMobile()) { document.body.classList.remove('m-cards'); return; }
  document.body.classList.add('m-cards');
  stampLabels();
  document.querySelectorAll('table').forEach(function(tb) {
    if (tb.classList.contains('pvtable') || tb.classList.contains('m-nostack')) return;
    if (tb.style && tb.style.minWidth) return;   // KPI指标配置表（场景×指标矩阵，min-width:960px）不转卡片
    if (!tb.querySelector('thead th')) return;
    tb.classList.add('mstack');
    tb.querySelectorAll('tbody tr').forEach(function(tr) {
      if (tr.querySelector('.mtoggle')) return;
      var sec = [];
      [].forEach.call(tr.children, function(td) {
        var lb = td.dataset.label || '';
        if (M2.indexOf(lb) >= 0) { td.classList.add('m2'); sec.push(td); }
        if (MHERO.indexOf(lb) >= 0) td.classList.add('mhero');
        if (MTITLE.indexOf(lb) >= 0) td.classList.add('mtitle');
        if (MCHIP.indexOf(lb) >= 0) td.classList.add('mchip');
      });
      // 只藏 1 项时反而添乱：直接显示（少一次点击）
      if (sec.length < 2) { sec.forEach(function(td) { td.classList.remove('m2'); }); return; }
      var td = document.createElement('td');
      td.className = 'mtoggle';
      td.textContent = '查看全部字段 ▸（还有 ' + sec.length + ' 项）';
      td.addEventListener('click', function(ev) {
        ev.stopPropagation();
        tr.classList.toggle('open');
        td.textContent = tr.classList.contains('open') ? '收起 ▴' : '查看全部字段 ▸（还有 ' + sec.length + ' 项）';
      });
      tr.appendChild(td);
    });
  });
  shiftCards();
}
// 时段表（矩阵：指标×时段）→ 转置成「每时段一行」，点时段开骑手明细弹窗
function shiftCards() {
  document.querySelectorAll('.site-block').forEach(function(block) {
    block.querySelectorAll('.pvtable').forEach(function(tb) {
      var wrap = tb.closest('.pvwrap');
      if (!wrap) return;
      var old = block.querySelector('.m-shift');
      if (old) old.remove();
      var rows = [].slice.call(tb.querySelectorAll('tr'));
      if (rows.length < 3) return;
      var heads = [].slice.call(rows[0].children).map(function(th, i) {
        if (i === 0) return null;
        var btn = th.querySelector('.pvlink');
        return { label: (btn ? btn.textContent : th.textContent).replace(/\\s+/g, '').replace('进行中', '').trim(),
                 idx: btn ? btn.dataset.idx : null,
                 ongoing: /进行中/.test(th.textContent) };
      });
      var pick = function(name) {
        for (var i = 0; i < rows.length; i++) {
          var cs = rows[i].children;
          if (cs.length && cs[0].textContent.replace(/\\s+/g, '').trim() === name)
            return [].slice.call(cs, 1).map(function(td) { return td.textContent.replace(/\\s+/g, ' ').trim(); });
        }
        return null;
      };
      var yp = pick('应排目标'), sp = pick('实排目标'), db = pick('达标骑手'), dl = pick('达标率'), ns = pick('排班未出勤');
      var html = '';
      heads.forEach(function(h, ci) {
        if (!h) return;
        // ⚠️ 2026-11-03 修：heads[0] 是「项目」列表头（已被 return 跳过），而 pick() 返回的是
        //    从第 2 列切出来的数组（yp[0] 对应「凌晨1」）→ 必须用 ci-1 取数，否则整列错位一格：
        //    凌晨1 显示的是早餐1 的数、夜宵2 显示的是全天合计、最后一档全天全是「-」。
        var k = ci - 1;
        var rate = dl ? (dl[k] || '') : '';
        var low = rate && parseFloat(rate) < 95;
        html += '<div class="msr' + (h.ongoing ? ' now' : '') + '" data-idx="' + (h.idx === null ? '' : h.idx) + '">'
          + '<div class="msn">' + h.label + (h.ongoing ? '<em>进行中</em>' : '') + '</div>'
          + '<div class="msnums">'
          + (yp ? '<span>应排 <b>' + (yp[k] || '-') + '</b></span>' : '')
          + (sp ? '<span>实排 <b>' + (sp[k] || '-') + '</b></span>' : '')
          + (db ? '<span>达标 <b>' + (db[k] || '-') + '</b></span>' : '')
          + (function() {
              var v = ns ? (ns[k] || '').replace(/[—\\-－]/g, '').trim() : '';
              return (v && v !== '0') ? '<span class="down">未出勤 <b>' + v + '</b></span>' : '';
            })()
          + '</div>'
          + '<div class="mspct' + (low ? ' low' : '') + '">' + (rate || '-') + '</div></div>';
      });
      var box = document.createElement('div');
      box.className = 'm-shift';
      box.innerHTML = '<div class="mshd">全天时段达标率 · 点时段看骑手明细</div>' + html;
      wrap.parentNode.insertBefore(box, wrap.nextSibling);
      box.addEventListener('click', function(e) {
        var r = e.target.closest ? e.target.closest('.msr') : null;
        if (!r || !r.dataset.idx) return;
        var btn = tb.querySelector('.pvlink[data-idx="' + r.dataset.idx + '"]');
        if (btn) btn.click();
      });
    });
  });
}
try { mobileTables(); } catch (e) { console.warn('手机端卡片化失败：', e); }
window.addEventListener('resize', function() {
  clearTimeout(window.__mtT);
  window.__mtT = setTimeout(function() { try { mobileTables(); } catch (e) {} }, 200);
});
</script>'''.replace('__INTERVAL_SEC__', str(interval))
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>配送运营实时看板</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:"PingFang SC","Microsoft YaHei",sans-serif; background:#F7FAFC; color:#2D3748; padding:20px; }}
  .board {{ width:100%; max-width:none; margin:0 auto; display:flex; flex-direction:column; gap:16px; }}
  .titlebar {{ background:linear-gradient(135deg,#1A365D,#2B6CB0); color:#fff; border-radius:12px; padding:16px 22px;
      display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }}
  .titlebar h1 {{ font-size:19px; }}
  .meta {{ font-size:12px; opacity:.9; text-align:right; line-height:1.9; }}
  .live {{ color:#3DDCFF; font-weight:700; }}
  #cd {{ font-size:12px; color:#90CDF4; font-weight:700; letter-spacing:1px; }}
  .cd-wrap {{ display:inline-block; margin-left:8px; background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.28);
      border-radius:999px; padding:2px 10px; font-size:12px; color:#fff; font-weight:700; white-space:nowrap; }}
  #fb {{ font-size:12px; color:#3DDCFF; margin-left:8px; transition:opacity .5s; opacity:0; }}
  .rbtn {{ background:rgba(255,255,255,.18); color:#fff; border:1px solid rgba(255,255,255,.4); border-radius:8px;
      padding:4px 12px; font-size:12px; cursor:pointer; margin-left:8px; }}
  .rbtn:hover {{ background:rgba(255,255,255,.32); }}
  .sitebar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .slabel {{ font-size:12px; color:#718096; font-weight:700; }}
  .site-btn {{ border:1px solid #CBD5E0; background:#fff; color:#4A5568; border-radius:10px; padding:7px 16px;
      font-size:13px; font-weight:700; cursor:pointer; }}
  .site-btn.active {{ background:linear-gradient(135deg,#1A365D,#2B6CB0); color:#fff; border-color:transparent; }}
  .ssel {{ border:1px solid #CBD5E0; background:#fff; border-radius:8px; padding:6px 12px; font-size:13px; font-weight:700; color:#1A365D; min-width:220px; outline:none; cursor:pointer; }}
  .ssel:focus {{ border-color:#2B6CB0; }}
  .site-hd {{ display:flex; justify-content:flex-end; align-items:center; flex-wrap:wrap; gap:6px; padding:2px 2px 0; }}
  .dt-badge {{ background:rgba(13,148,136,.10); color:#0D9488; border:1px solid rgba(13,148,136,.35); border-radius:8px; padding:2px 10px; font-weight:600; font-size:12.5px; letter-spacing:.2px; }}
  body[data-theme="dark"] .dt-badge {{ background:rgba(61,220,255,.12); color:#3DDCFF; border-color:rgba(61,220,255,.32); }}
  .site-name {{ font-size:16px; font-weight:800; color:#1A365D; }}
  .meta2 {{ font-size:12px; color:#718096; }}
  .card {{ background:#fff; border-radius:12px; box-shadow:0 2px 12px rgba(26,54,93,.08); overflow:hidden; }}
  .card-hd {{ background:#1A365D; color:#fff; padding:10px 18px; font-size:14px; font-weight:700; display:flex;
      justify-content:space-between; align-items:center; }}
  .card-hd .sub {{ font-size:12px; font-weight:400; opacity:.85; }}
  /* P0-1 考核判定横幅：置顶全宽状态条，红/绿底白字，3米外一眼看清 */
  .verdict {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
      margin:0; padding:16px 20px; color:#fff; font-weight:800; }}
  .verdict.pass {{ background:linear-gradient(135deg,#4F46E5,#7C3AED); }}
  .verdict.warn {{ background:linear-gradient(135deg,#B45309,#F59E0B); }}
  .verdict.fail {{ background:linear-gradient(135deg,#B91C1C,#F87171); }}
  .verdict .v-main {{ font-size:30px; line-height:1.1; }}
  .verdict .v-sub {{ font-size:14px; font-weight:600; opacity:.95; line-height:1.7; text-align:right; }}
  .verdict .v-tag {{ display:inline-block; background:rgba(255,255,255,.22); border:1px solid rgba(255,255,255,.5);
      border-radius:999px; padding:4px 14px; font-size:16px; margin-left:6px; }}
  table {{ border-collapse:collapse; width:100%; }}
  thead th {{ background:#EDF2F7; color:#1A365D; font-size:12px; font-weight:700; padding:9px 12px; text-align:center; }}
  tbody td {{ font-size:13px; padding:9px 12px; text-align:center; border-bottom:1px solid #EDF2F7; }}
  tbody tr:nth-child(even) td {{ background:#F7FAFC; }}
  .pill {{ display:inline-block; padding:2px 12px; border-radius:999px; font-size:12px; font-weight:600; }}
  /* 语义拆分：中性状态（下班/休息）灰蓝描边，失败（未达标）红底白字加粗，正常（上班）绿 */
  .pill.on {{ background:#F0FDFA; color:#0D9488; border:1px solid #99F6E4; }}
  .pill.rest {{ background:#EDF2F7; color:#4A5568; border:1px solid #CBD5E0; }}
  .pill.off {{ background:#EDF2F7; color:#4A5568; border:1px solid #CBD5E0; }}
  .pill.pass {{ background:#F0FDFA; color:#0D9488; border:1px solid #99F6E4; }}
  .pill.ok {{ background:#EFF6FF; color:#2563EB; border:1px solid #BFDBFE; }}
  .pill.warn {{ background:#FFFBEB; color:#B45309; border:1px solid #FDE68A; }}
  .pill.fail {{ background:#B91C1C; color:#fff; border:1px solid #B91C1C; font-weight:800; }}
  .pill.norm {{ background:#EDF2F7; color:#4A5568; border:1px solid #CBD5E0; }}
  .flag {{ display:block; font-size:12px; color:#B91C1C; font-weight:600; margin-top:1px; }}
  .up {{ color:#0D9488; font-weight:600; }}
  .down {{ color:#B91C1C; font-weight:600; }}
  .warnc {{ color:#B45309; font-weight:600; }}
  .mutedc {{ color:#64748B; }}
  .sumline {{ display:flex; gap:14px; padding:14px 18px; flex-wrap:wrap; }}
  .gapbar {{ display:flex; gap:10px; padding:10px 18px 0; }}
  .gapcard {{ flex:1; background:#EDF2F7; border-radius:10px; padding:10px 14px; text-align:center; }}
  .gapcard .v {{ font-size:22px; font-weight:800; color:#1A365D; }}
  .gapcard .v.info {{ color:#2563EB; }}
  .gapcard .v.up {{ color:#0D9488; }}
  .gapcard .v.down {{ color:#B91C1C; }}
  .gapcard .k {{ font-size:11px; color:#718096; margin-top:2px; }}
  .gapcard .v .gapn {{ font-size:12px; font-weight:600; margin-left:6px; color:#718096; }}
  .gapcard .v .gapn.up {{ color:#0D9488; }}
  .gapcard .v .gapn.down {{ color:#B91C1C; }}
  .modal-mask {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:999; align-items:flex-start; justify-content:center; padding:40px 16px; overflow:auto; }}
  .modal {{ background:#fff; border-radius:14px; max-width:980px; width:100%; box-shadow:0 18px 50px rgba(0,0,0,.25); }}
  .modal-hd {{ display:flex; justify-content:space-between; align-items:center; padding:14px 20px; border-bottom:1px solid #E2E8F0; font-size:16px; font-weight:800; color:#1A365D; }}
  .modal-hd button {{ border:none; background:none; font-size:18px; cursor:pointer; color:#718096; padding:4px 8px; border-radius:6px; }}
  .modal-hd button:hover {{ background:#EDF2F7; }}
  .modal-body {{ padding:16px 20px; overflow:auto; max-height:70vh; }}
  .kpirow {{ display:flex; gap:10px; padding:10px 18px 0; flex-wrap:wrap; }}
  .kpicard {{ flex:1; min-width:88px; background:#EDF2F7; border-radius:10px; padding:8px 12px; text-align:center; }}
  .kpicard .v {{ font-size:18px; font-weight:800; color:#1A365D; }}
  .kpicard .k {{ font-size:12px; color:#718096; margin-top:1px; }}
  .sub2 {{ font-size:12px; color:#718096; margin-left:2px; }}
  .feebar {{ padding:10px 18px 0; font-size:12px; color:#4A5568; }}
  .lvdist {{ padding:8px 18px 0; font-size:12px; color:#4A5568; display:flex; align-items:center; flex-wrap:wrap; gap:6px; }}
  .lvbar {{ display:inline-flex; align-items:center; gap:4px; background:#EDF2F7; border-radius:6px; padding:3px 8px; font-size:11px; }}
  .lvb {{ height:10px; border-radius:4px; background:linear-gradient(90deg,#2B6CB0,#63B3ED); display:inline-block; }}
  /* ── 全比例适配：电视/大屏 放大 ── */
  @media (min-width: 1600px) {{
    body {{ font-size:17px; padding:26px; }}
    .site-name {{ font-size:22px; }}
    .card-hd {{ font-size:18px; }}
    .card-hd .sub {{ font-size:13px; }}
    table {{ font-size:15px; }}
    th, td {{ padding:9px 12px; }}
    .sumitem .v, .kpicard .v, .gapcard .v {{ font-size:26px; }}
    .sumitem .k, .kpicard .k, .gapcard .k {{ font-size:13px; }}
    .fbtn {{ font-size:14px; padding:6px 14px; }}
    .rbtn {{ font-size:14px; padding:6px 14px; }}
    .pvlabel {{ font-size:14px; }}
    .lvbar, .faildet-hd, .feebar, .scene-line, .jf-line, .tf-line, .flabel {{ font-size:14px; }}
    .verdict .v-main {{ font-size:38px; }}
    .verdict .v-sub {{ font-size:16px; }}
    .verdict .v-tag {{ font-size:19px; }}
  }}
  @media (min-width: 2200px) {{
    body {{ font-size:20px; padding:36px; }}
    table {{ font-size:17px; }}
    th, td {{ padding:11px 14px; }}
    .sumitem .v, .kpicard .v, .gapcard .v {{ font-size:32px; }}
    .verdict .v-main {{ font-size:46px; }}
    .verdict .v-sub {{ font-size:18px; }}
    .verdict .v-tag {{ font-size:22px; }}
  }}
  /* ── 平板 ── */
  @media (min-width: 641px) and (max-width: 1200px) {{
    body {{ padding:14px; }}
    .sumitem {{ min-width:88px; }}
    .kpicard {{ min-width:72px; }}
  }}
  /* ── 手机：堆叠+紧凑+表格横滚 ── */
  @media (max-width: 640px) {{
    body {{ padding:8px; }}
    .titlebar {{ flex-direction:column; align-items:flex-start; gap:6px; }}
    .meta div {{ flex-wrap:wrap; gap:4px; }}
    .sitebar {{ flex-wrap:wrap; gap:4px; }}
    .ssel {{ width:100%; }}
    .site-btn {{ font-size:11px; padding:3px 8px; }}
    .gapbar, .kpirow {{ gap:6px; }}
    .gapcard, .kpicard {{ min-width:60px; padding:6px 8px; }}
    .gapcard .v, .kpicard .v {{ font-size:15px; }}
    .gapcard .k, .kpicard .k {{ font-size:9px; }}
    .sumitem {{ min-width:62px; padding:6px 8px; }}
    .sumitem .v {{ font-size:15px; }}
    .sumitem .k {{ font-size:9px; }}
    table {{ font-size:10.5px; }}
    .tabpane {{ overflow-x:auto; }}
    /* 手机竖屏：宽表压进一屏（时段表 11 列 / 档位达成 10 列 / 明细 8 列）——
       只靠横滚用户得旋转手机才看得全（2026-10-04 用户报「只能横屏看」）。
       ⚠️ 必须带 body 前缀提特异性：基础样式里 thead th/tbody td 是 (0,0,2)、.pvtable td 是 (0,1,1)，
       只写 `th, td`(0,0,1) 会被盖掉 → 手机端一直沿用 9px 12px 内边距，10 列光内边距就 240px 必然超宽（实测 425px） */
    body thead th, body tbody td {{ padding:2px 3px; font-size:10.5px; white-space:normal; word-break:break-all; line-height:1.3; }}
    body .pvtable th, body .pvtable td {{ padding:3px 2px; font-size:10px; white-space:normal; word-break:break-all; line-height:1.25; }}
    body .pvtable .pvlabel {{ font-size:10px; padding:3px 4px; }}
    body .pvwrap {{ padding:8px 4px 4px; }}
    body .pvlink {{ font-size:11px; padding:1px 2px; }}
    /* ── 手机端方案A：宽表→卡片（DOM 仍是真表格 → 筛选/排序照旧生效；只靠 data-label + CSS 换皮）── */
    body.m-cards table.mstack {{ display:block; width:100%; }}
    body.m-cards table.mstack thead {{ display:none; }}
    body.m-cards table.mstack tbody {{ display:block; }}
    body.m-cards table.mstack tbody tr {{ display:grid; grid-template-columns:1fr 1fr; gap:7px 12px; background:#fff;
      border:1px solid #E2E8F0; border-radius:11px; padding:11px 12px; margin:0 0 10px; }}
    body.m-cards table.mstack tbody td {{ display:flex; flex-direction:column; align-items:flex-start; gap:1px; border:0;
      padding:0; background:transparent; text-align:left; font-size:12.5px; color:#2D3748; white-space:normal;
      word-break:break-all; line-height:1.3; }}
    body.m-cards table.mstack tbody td::before {{ content:attr(data-label); flex:none; color:#A0AEC0; font-size:10px; }}
    body.m-cards table.mstack tbody tr:nth-child(even) td {{ background:transparent; }}
    body.m-cards table.mstack td.mtitle {{ grid-column:1/-1; font-size:15.5px; font-weight:800; color:#1A365D; }}
    body.m-cards table.mstack td.mtitle::before {{ display:none; }}
    body.m-cards table.mstack td.mchip {{ align-items:flex-end; }}
    body.m-cards table.mstack td.mchip::before {{ display:none; }}
    body.m-cards table.mstack td.mhero {{ font-size:19px; font-weight:800; color:#1A365D; line-height:1.12; }}
    body.m-cards table.mstack td.mhero::before {{ font-size:10px; font-weight:400; color:#A0AEC0; }}
    body.m-cards table.mstack td.m2 {{ display:none; }}
    body.m-cards table.mstack tbody tr.open td.m2 {{ display:flex; }}
    body.m-cards table.mstack td.mtoggle {{ grid-column:1/-1; flex-direction:row; justify-content:center; align-items:center;
      color:#2B6CB0; font-weight:700; font-size:11px; padding-top:3px; border-top:1px dashed #EDF2F7; }}
    body.m-cards table.mstack td.mtoggle::before {{ display:none; }}
    /* 时段表＝矩阵（指标×时段）→ 手机端藏横表，换「每时段一行」 */
    body.m-cards .pvwrap {{ display:none; }}
    body.m-cards .m-shift {{ padding:0 12px; }}
    .mshd {{ font-size:12px; font-weight:700; color:#1A365D; padding:6px 0 8px; }}
    .msr {{ display:flex; align-items:center; gap:8px; background:#fff; border:1px solid #E2E8F0; border-radius:10px;
      padding:8px 10px; margin-bottom:7px; }}
    .msr.now {{ border-color:#FCD34D; background:#FFFBEB; }}
    .msr .msn {{ width:58px; flex:none; font-size:12px; font-weight:700; color:#1A365D; }}
    .msr .msn em {{ display:block; font-style:normal; font-size:9.5px; color:#B45309; }}
    .msr .msnums {{ flex:1; display:flex; flex-wrap:wrap; gap:4px 10px; font-size:10.5px; color:#718096; }}
    .msr .msnums b {{ color:#1A365D; font-size:11.5px; }}
    .msr .mspct {{ flex:none; min-width:44px; text-align:right; font-size:14px; font-weight:800; color:#0D9488; }}
    .msr .mspct.low {{ color:#B91C1C; }}
    .m-shift .down b, .m-shift .down {{ color:#B91C1C; }}
    /* 暗色适配（卡片在暗色下别用白底） */
    body[data-theme="dark"].m-cards table.mstack tbody tr,
    body[data-theme="dark"] .msr {{ background:rgba(255,255,255,.055); border-color:rgba(255,255,255,.10); }}
    body[data-theme="dark"] .msr.now {{ background:rgba(251,211,77,.13); border-color:rgba(251,211,77,.35); }}
    body[data-theme="dark"].m-cards table.mstack tbody td {{ color:#C9D3E6; }}
    body[data-theme="dark"].m-cards table.mstack td.mtitle,
    body[data-theme="dark"].m-cards table.mstack td.mhero,
    body[data-theme="dark"] .msr .msn,
    body[data-theme="dark"] .msr .msnums b {{ color:#EAF1FF; }}
    body[data-theme="dark"] .mshd {{ color:#DCE5FA; }}
    body[data-theme="dark"].m-cards table.mstack td.mtoggle {{ color:#7FD4FF; }}
    .card:has(table) {{ overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; }}
    .card-hd {{ font-size:14px; }}
    .card-hd .sub {{ font-size:10px; }}
    .fbtn {{ font-size:11px; padding:3px 8px; }}
    .rbtn {{ font-size:11px; padding:3px 8px; }}
    .pvwrap {{ overflow-x:auto; }}
    .filterbar {{ padding:0 10px 8px; }}
    .faildet {{ margin:8px 8px 0; }}
    .feebar, .scene-line, .jf-line, .tf-line {{ font-size:10.5px; padding-left:10px; padding-right:10px; }}
    .foot {{ font-size:10px; }}
    details.faildet summary, .faildet-hd {{ font-size:12px; }}
  }}
  .faildet {{ margin:10px 18px 0; }}
  .faildet-hd {{ font-size:13px; font-weight:700; color:#1A365D; padding:8px 0 4px; }}
  .faildet table {{ margin-top:4px; }}
  .rider-link {{ color:#2B6CB0; font-weight:700; cursor:pointer; text-decoration:underline; }}
  .rider-link.warn {{ color:#B91C1C; }}
  .pt-info {{ background:linear-gradient(135deg,#0D9488,#3DDCFF); color:#fff; font-weight:800; font-size:12px; padding:2px 10px; border-radius:999px; box-shadow:0 1px 3px rgba(13,148,136,.4); }}
  .scene-line {{ padding:8px 18px 2px; font-size:12px; color:#4A5568; }}
  .jf-line {{ padding:2px 18px; font-size:12px; font-weight:600; }}
  .jf-pass {{ color:#0D9488; }}
  .jf-fail {{ color:#B91C1C; }}
  .jf-warn {{ color:#B45309; }}
  .jf-muted {{ color:#64748B; }}
  .tf-line {{ padding:2px 18px 6px; font-size:12px; color:#4A5568; }}
  .sumitem {{ flex:1; min-width:110px; background:#F7FAFC; border-radius:8px; padding:10px 12px; text-align:center; }}
  .filterbar {{ padding:0 18px 12px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  .lqbar {{ display:flex; align-items:center; gap:8px; padding:0 18px 10px; flex-wrap:wrap; }}
  .lq-input {{ flex:0 0 280px; border:1px solid #CBD5E0; background:#fff; border-radius:8px; padding:7px 12px;
      font-size:13px; color:#2D3748; outline:none; }}
  .lq-input:focus {{ border-color:#2B6CB0; box-shadow:0 0 0 2px rgba(43,108,176,.15); }}
  .lq-btn {{ background:linear-gradient(135deg,#1A365D,#2B6CB0); color:#fff; border:none; border-radius:8px;
      padding:7px 16px; font-size:13px; font-weight:700; cursor:pointer; }}
  .lq-btn:hover {{ opacity:.9; }}
  .lq-btn:disabled {{ opacity:.55; cursor:wait; }}
  .lq-hint {{ font-size:11px; color:#A0AEC0; }}
  .lq-result {{ padding:0 18px 10px; font-size:13px; }}
  .lq-row {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px 12px; background:#EDF2F7;
      border:1px solid #E2E8F0; border-radius:10px; padding:8px 14px; color:#2D3748; }}
  .lq-row .lq-ts {{ margin-left:auto; font-size:11px; color:#A0AEC0; }}
  .lq-row .lq-note {{ font-size:11px; color:#A0AEC0; }}
  /* 2026-11-06 档位查询串行卡片：一行一个指标，无效/改派单独立成行 */
  .lq-card {{ background:#EDF2F7; border:1px solid #E2E8F0; border-radius:10px; overflow:hidden; }}
  .lq-hd {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:9px 14px;
      background:#fff; border-bottom:1px solid #E2E8F0; }}
  .lq-hd .nm {{ font-size:15px; font-weight:800; color:#1A365D; }}
  .lq-hd .lq-ts {{ margin-left:auto; font-size:11px; color:#A0AEC0; }}
  .lq-lines {{ display:flex; flex-direction:column; }}
  .lq-line {{ display:flex; align-items:baseline; gap:10px; padding:6px 14px; font-size:13px;
      border-top:1px dashed #E2E8F0; }}
  .lq-line:first-child {{ border-top:none; }}
  .lq-line .k {{ flex:0 0 84px; color:#718096; font-size:12px; }}
  .lq-line .v {{ color:#2D3748; font-weight:600; }}
  .lq-line .v b {{ font-weight:800; }}
  .lq-line .v.deduct {{ color:#B45309; }}
  .lq-line .v.zero {{ color:#A0AEC0; font-weight:500; }}
  .lq-sub {{ font-style:normal; font-weight:400; font-size:11px; color:#A0AEC0; margin-left:8px; }}
  .flabel {{ font-size:11px; color:#718096; font-weight:700; margin-left:6px; }}
  .fbtn {{ border:1px solid #CBD5E0; background:#fff; color:#4A5568; border-radius:14px; padding:4px 12px; font-size:12px; cursor:pointer; }}
  .fbtn:hover {{ border-color:#2B6CB0; color:#2B6CB0; }}
  .fbtn.active {{ background:linear-gradient(135deg,#1A365D,#2B6CB0); color:#fff; border-color:transparent; }}
  .sumitem .v {{ font-size:20px; font-weight:800; color:#1A365D; }}
  .sumitem .k {{ font-size:11px; color:#718096; margin-top:2px; }}
  .lvchip {{ display:inline-block; background:#EFF6FF; color:#2563EB; border:1px solid #BFDBFE; border-radius:999px;
      padding:3px 12px; font-size:12px; margin:0 4px 4px 0; }}
  .lvchip b {{ font-size:14px; }}
  .foot {{ text-align:center; font-size:12px; color:#718096; padding:4px; }}
  .tabs {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .tab {{ flex:1 1 auto; min-width:104px; background:#fff; border:1px solid #E2E8F0; color:#4A5568; border-radius:10px;
      padding:11px 6px; font-size:14px; font-weight:700; cursor:pointer; white-space:nowrap; }}
  .tab.active {{ background:linear-gradient(135deg,#1A365D,#2B6CB0); color:#fff !important; border-color:transparent; }}
  /* 业务色只用在非激活态：激活统一白字，保证对比度（评审 P1 修复） */
  .tab.qc-tab:not(.active) {{ color:#fa541c; }}
  .tab.appeal-tab:not(.active) {{ color:#f5222d; }}
  .tab.svc-tab:not(.active) {{ color:#722ed1; }}
  .tabpane {{ display:none; }}
  .tabpane.active {{ display:block; }}
  .pvwrap {{ overflow-x:auto; padding:12px 18px 4px; }}
  .pvtable {{ border-collapse:collapse; width:100%; }}
  .pvtable th {{ background:#EDF2F7; color:#1A365D; font-size:12px; padding:7px 8px; border:1px solid #E2E8F0; }}
  .pvtable td {{ font-size:12px; padding:6px 8px; text-align:center; border:1px solid #E2E8F0; }}
  .pvtable .pvlabel {{ background:#F7FAFC; font-weight:700; color:#4A5568; }}
  .pvtable .col-all {{ background:#E8EDF5; color:#1A365D; }}
  .pvtable td:last-child {{ background:#F7FAFC; font-weight:700; }}
  .aggr {{ padding:10px 18px 2px; font-size:13px; color:#4A5568; }}
  .judge {{ margin:10px 18px 14px; padding:10px 14px; border-radius:8px; font-size:13px; font-weight:700; }}
  .judge.ok {{ background:#F0FDFA; color:#0D9488; }}
  .judge.bad {{ background:#FFF5F5; color:#B91C1C; }}
  body[data-theme="dark"] .judge.ok {{ background:rgba(13,148,136,.18); color:#3DDCFF; }}
  body[data-theme="dark"] .judge.bad {{ background:rgba(185,28,28,.16); color:#FF4D8D; }}
  .subhd {{ padding:9px 18px; background:#E8EDF5; color:#1A365D; font-weight:700; font-size:13px; }}
  .pvlink {{ background:none; border:none; color:#2B6CB0; font-size:13px; font-weight:700; cursor:pointer; text-decoration:underline; padding:2px 4px; }}
  .pvlink:hover {{ color:#1A365D; }}
  .pvlink.active {{ color:#2B6CB0; text-decoration:none; background:#EBF8FF; border-radius:4px; }}
  /* P2-5 进行中时段：列头加蓝底角标，与已结束/未开始区分 */
  .pvlink .ongoing-tag {{ display:inline-block; background:#2B6CB0; color:#fff; font-size:10px; font-weight:700;
      border-radius:999px; padding:1px 6px; margin-left:4px; vertical-align:middle; }}
  th[data-sort] {{ cursor:pointer; user-select:none; }}
  th[data-sort]:hover {{ background:#E2E8F0; }}
  .srt {{ font-size:10px; color:#2B6CB0; margin-left:2px; opacity:.5; }}
  .srt.on {{ opacity:1; font-size:12px; font-weight:700; }}
  .shift-detail {{ display:none; padding:0 0 6px; }}
  .shift-search {{ padding:10px 18px 2px; }}
  .shift-search input {{ width:260px; max-width:100%; padding:6px 12px; border:1px solid #CBD5E0;
      border-radius:8px; font-size:13px; color:#1A365D; outline:none; background:#fff; }}
  .shift-search input:focus {{ border-color:#2B6CB0; box-shadow:0 0 0 2px rgba(43,108,176,.15); }}
  .shift-search input::placeholder {{ color:#A0AEC0; }}
  .row-unsched td {{ background:#E0F2FE !important; }}
  .unsched-hd {{ background:#E0F2FE !important; color:#2B6CB0; }}
  /* 订单识别：在途单（待到店/待取餐/待送达）高亮行（2026-09-03 用户强调这两个状态点重要） */
  .row-inway td {{ background:#F0FDFA !important; }}
  body[data-theme="dark"] .row-inway td {{ background:rgba(13,148,136,.12) !important; }}
  /* ── 晋升档位分色（档位达成表「晋升档位」列，标1-标5 醒目版；lvn-0=已满档灰）── */
  .lvn-0 {{ color:#A0AEC0; font-weight:700; }}
  .lvn-1 {{ color:#4A5D82; font-weight:700; }}
  .lvn-2 {{ color:#1D6FD1; font-weight:700; }}
  .lvn-3 {{ color:#0E9F5A; font-weight:700; }}
  .lvn-4 {{ color:#D97706; font-weight:700; }}
  .lvn-5 {{ color:#B426C8; font-weight:700; }}
  /* ── 暗色模式：苹果雾面玻璃（2026-08-29 用户定稿：透明磨砂/背后色块/高光边，禁纯黑底/发光/噪点）── */
  body[data-theme="dark"] {{
    background:
      radial-gradient(1100px 640px at 6% -12%, rgba(61,220,255,.16), transparent 60%),
      radial-gradient(980px 600px at 94% -6%, rgba(124,92,255,.20), transparent 58%),
      radial-gradient(760px 520px at 50% 112%, rgba(255,77,141,.10), transparent 62%),
      #070b14;
    background-attachment: fixed;
    color: #EAF1FF;
  }}
  body[data-theme="dark"] .titlebar {{
    background: linear-gradient(135deg, rgba(61,220,255,.26), rgba(124,92,255,.32), rgba(255,77,141,.14));
    border: 1px solid rgba(255,255,255,.14);
    box-shadow: 0 8px 32px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.16);
  }}
  body[data-theme="dark"] .card {{
    background: rgba(255,255,255,.055);
    -webkit-backdrop-filter: blur(22px) saturate(170%);
    backdrop-filter: blur(22px) saturate(170%);
    border: 1px solid rgba(255,255,255,.09);
    box-shadow: 0 10px 34px rgba(0,0,0,.32), inset 0 1px 0 rgba(255,255,255,.09);
  }}
  body[data-theme="dark"] .card-hd {{
    background: linear-gradient(135deg, rgba(30,44,92,.88), rgba(44,58,120,.70));
    border-bottom: 1px solid rgba(255,255,255,.08);
  }}
  body[data-theme="dark"] thead th {{
    background: rgba(255,255,255,.08); color:#DCE5FA;
    border-bottom: 1px solid rgba(255,255,255,.10);
  }}
  body[data-theme="dark"] tbody td {{ color:#EAF1FF; border-bottom:1px solid rgba(255,255,255,.05); }}
  body[data-theme="dark"] tbody tr:nth-child(even) td {{ background:rgba(255,255,255,.03); }}
  body[data-theme="dark"] .pill.on, body[data-theme="dark"] .pill.pass {{
    background:rgba(13,148,136,.22); color:#3DDCFF; border:1px solid rgba(61,220,255,.38);
  }}
  body[data-theme="dark"] .pill.ok {{ background:rgba(37,99,235,.26); color:#8FD3FF; border:1px solid rgba(96,165,250,.38); }}
  body[data-theme="dark"] .pill.warn {{ background:rgba(180,83,9,.26); color:#FFA53D; border:1px solid rgba(251,191,36,.38); }}
  body[data-theme="dark"] .pill.rest, body[data-theme="dark"] .pill.off, body[data-theme="dark"] .pill.norm {{
    background:rgba(255,255,255,.07); color:#AAB8CE; border:1px solid rgba(255,255,255,.14);
  }}
  body[data-theme="dark"] .pill.fail {{ background:#B91C1C; color:#fff; border-color:#B91C1C; }}
  body[data-theme="dark"] .up {{ color:#3DDCFF; }}
  body[data-theme="dark"] .down {{ color:#FF4D8D; }}
  body[data-theme="dark"] .warnc {{ color:#FFA53D; }}
  body[data-theme="dark"] .mutedc {{ color:#9AABC4; }}
  body[data-theme="dark"] .sumitem, body[data-theme="dark"] .gapcard, body[data-theme="dark"] .kpicard {{
    background: rgba(255,255,255,.05);
  }}
  body[data-theme="dark"] .sumitem .v, body[data-theme="dark"] .gapcard .v, body[data-theme="dark"] .kpicard .v {{ color:#EAF1FF; }}
  body[data-theme="dark"] .gapcard .v.info {{ color:#60A5FA; }}
  body[data-theme="dark"] .gapcard .v.up {{ color:#3DDCFF; }}
  body[data-theme="dark"] .gapcard .v.down {{ color:#FF4D8D; }}
  body[data-theme="dark"] .gapcard .v .gapn {{ color:#9AABC4; }}
  body[data-theme="dark"] .gapcard .v .gapn.up {{ color:#3DDCFF; }}
  body[data-theme="dark"] .gapcard .v .gapn.down {{ color:#FF4D8D; }}
  body[data-theme="dark"] .srt {{ color:#8FD3FF; }}
  body[data-theme="dark"] .sumitem .k, body[data-theme="dark"] .gapcard .k, body[data-theme="dark"] .kpicard .k {{ color:#9AABC4; }}
  body[data-theme="dark"] .tab {{ background:rgba(255,255,255,.05); color:#B8C4E0; border:1px solid rgba(255,255,255,.10); }}
  body[data-theme="dark"] .tab.active {{
    background: linear-gradient(135deg, #2F54D0, #7A3FD0 55%, #009EDC);
    color:#fff !important; border-color:transparent;
  }}
  body[data-theme="dark"] .fbtn, body[data-theme="dark"] .site-btn {{
    background:rgba(255,255,255,.05); color:#B8C4E0; border:1px solid rgba(255,255,255,.14);
  }}
  body[data-theme="dark"] .fbtn:hover, body[data-theme="dark"] .site-btn:hover {{ border-color:#58B1FF; color:#7FD4FF; }}
  body[data-theme="dark"] .fbtn.active, body[data-theme="dark"] .site-btn.active {{
    background:linear-gradient(135deg, #2F54D0, #009EDC); color:#fff; border-color:transparent;
  }}
  body[data-theme="dark"] .ssel, body[data-theme="dark"] .lq-input, body[data-theme="dark"] .shift-search input {{
    background:rgba(255,255,255,.06); color:#EAF1FF; border:1px solid rgba(255,255,255,.15);
  }}
  body[data-theme="dark"] .lq-btn {{ background:linear-gradient(135deg,#2F54D0,#009EDC); }}
  body[data-theme="dark"] .modal {{
    background:rgba(17,23,43,.94);
    -webkit-backdrop-filter:blur(24px) saturate(160%); backdrop-filter:blur(24px) saturate(160%);
    border:1px solid rgba(255,255,255,.10); box-shadow:0 20px 60px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.10);
  }}
  body[data-theme="dark"] .modal-hd {{ color:#EAF1FF; border-bottom:1px solid rgba(255,255,255,.08); }}
  body[data-theme="dark"] .modal-hd button {{ color:#9AABC4; }}
  body[data-theme="dark"] .modal-hd button:hover {{ background:rgba(255,255,255,.08); }}
  body[data-theme="dark"] .pvtable th {{ background:rgba(255,255,255,.06); color:#C3CDE8; border:1px solid rgba(255,255,255,.09); }}
  body[data-theme="dark"] .pvtable td {{ border:1px solid rgba(255,255,255,.09); }}
  body[data-theme="dark"] .pvtable .pvlabel {{ background:rgba(255,255,255,.04); color:#AAB8CE; }}
  body[data-theme="dark"] .pvtable .col-all {{ background:rgba(58,88,178,.32); color:#D8E2F4; }}
  body[data-theme="dark"] .pvtable td:last-child {{ background:rgba(255,255,255,.04); }}
  body[data-theme="dark"] .subhd {{ background:rgba(58,88,178,.26); color:#C9D6EC; }}
  body[data-theme="dark"] .aggr, body[data-theme="dark"] .feebar, body[data-theme="dark"] .scene-line, body[data-theme="dark"] .tf-line {{ color:#AAB8CE; }}
  body[data-theme="dark"] .site-name {{ color:#D8E2F4; }}
  body[data-theme="dark"] .meta2, body[data-theme="dark"] .slabel, body[data-theme="dark"] .flabel, body[data-theme="dark"] .sub2 {{ color:#9AABC4; }}
  body[data-theme="dark"] .lvchip {{ background:rgba(37,99,235,.26); color:#8FD3FF; border:1px solid rgba(96,165,250,.36); }}
  body[data-theme="dark"] .lq-row {{ background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.12); color:#EAF1FF; }}
  body[data-theme="dark"] .lq-card {{ background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.12); }}
  body[data-theme="dark"] .lq-hd {{ background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.12); }}
  body[data-theme="dark"] .lq-hd .nm {{ color:#EAF1FF; }}
  body[data-theme="dark"] .lq-line {{ border-color:rgba(255,255,255,.10); }}
  body[data-theme="dark"] .lq-line .k {{ color:#9AABC4; }}
  body[data-theme="dark"] .lq-line .v {{ color:#EAF1FF; }}
  body[data-theme="dark"] .row-unsched td {{ background:rgba(14,116,200,.26) !important; }}
  body[data-theme="dark"] .unsched-hd {{ background:rgba(14,116,200,.26) !important; color:#7CC4F5; }}
  body[data-theme="dark"] .rider-link {{ color:#7FD4FF; }}
  body[data-theme="dark"] .rider-link.warn {{ color:#FF4D8D; }}
  body[data-theme="dark"] .jf-pass {{ color:#3DDCFF; }}
  body[data-theme="dark"] .jf-fail {{ color:#FF4D8D; }}
  body[data-theme="dark"] .jf-warn {{ color:#FFA53D; }}
  body[data-theme="dark"] .jf-muted {{ color:#9AABC4; }}
  body[data-theme="dark"] .pvlink {{ color:#7FD4FF; }}
  body[data-theme="dark"] .pvlink:hover {{ color:#C9D6EC; }}
  body[data-theme="dark"] .pvlink.active {{ background:rgba(43,108,176,.25); }}
  body[data-theme="dark"] .faildet-hd {{ color:#C9D6EC; }}
  body[data-theme="dark"] th[data-sort]:hover {{ background:rgba(255,255,255,.08); }}
  body[data-theme="dark"] .lq-hint, body[data-theme="dark"] .lq-row .lq-ts, body[data-theme="dark"] .lq-row .lq-note {{ color:#9AABC4; }}
  body[data-theme="dark"] .tab.qc-tab:not(.active) {{ color:#ff7a45; }}
  body[data-theme="dark"] .tab.appeal-tab:not(.active) {{ color:#ff7875; }}
  body[data-theme="dark"] .tab.svc-tab:not(.active) {{ color:#b37feb; }}
  body[data-theme="dark"] .foot {{ color:#9AABC4; }}
  body[data-theme="dark"] .lvn-0 {{ color:#7C8DB5; }}
  body[data-theme="dark"] .lvn-1 {{ color:#A5BBE8; }}
  body[data-theme="dark"] .lvn-2 {{ color:#66B8FF; }}
  body[data-theme="dark"] .lvn-3 {{ color:#45EDA8; }}
  body[data-theme="dark"] .lvn-4 {{ color:#FFC078; }}
  body[data-theme="dark"] .lvn-5 {{ color:#FF84E6; }}
  body[data-theme="dark"] .lvn-0, body[data-theme="dark"] .lvn-1, body[data-theme="dark"] .lvn-2,
  body[data-theme="dark"] .lvn-3, body[data-theme="dark"] .lvn-4, body[data-theme="dark"] .lvn-5 {{ text-shadow:0 1px 2px rgba(0,0,0,.45); }}
</style>
</head>
<body data-board-ver="{BOARD_VER}">
<div class="board">
  <div class="titlebar">
    <h1>配送运营实时看板</h1>
    <div class="meta">
      <div><button class="rbtn" onclick="manualRefresh()">⟳ 手动刷新</button><button class="rbtn" id="theme-btn" onclick="toggleTheme()" title="切换亮色/暗色（苹果雾面玻璃）">🌙 暗色</button><button class="rbtn" onclick="openKpiCfg()" title="修改KPI考核目标值（0分/60分/100分线）" style="color:#13c2c2;font-weight:700">⚙ KPI指标</button><button class="rbtn" onclick="openBoardPass()" title="设置手机/局域网访问看板的密码">🔑 设置密码</button><button class="rbtn" onclick="checkUpdate()" title="检查是否有新版本">🔄 检测更新</button><span class="cd-wrap">⏱ <span id="cd">--:--</span></span><span id="fb"></span></div>
      <div id="clock">{first['date']} {first['now']}</div>
    </div>
  </div>
  {site_bar}
  {''.join(blocks)}
  <div class="foot">数据来自 Aeolus 实时出勤 · 点右上角「⟳ 手动刷新」立即拉取最新数据</div>
</div>
<div class="modal-mask" id="modal-mask" onclick="closeModal()">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-hd"><span id="modal-title"></span><button onclick="closeModal()" title="关闭">✕</button></div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>
{js}
</body>
</html>'''

def _ensure_aeolus_page(cdp):
    """确保当前页面在 Aeolus 域；已在不导航（避免打断使用者在窗口里的操作）"""
    try:
        r = cdp('Runtime.evaluate', {'expression': 'location.host', 'returnByValue': True})
        v = (r.get('result') or {}).get('result') or {}
        host = v.get('value', '') if isinstance(v, dict) else ''
    except Exception:
        host = ''
    if not host or ('ele.me' not in host and 'alibaba' not in host):
        cdp_core.navigate_aeolus(cdp)


def refresh_once(teams, out_path, interval=60):
    """拉一次数据并生成 HTML。teams=[(team_id, team_name), ...]。返回 True/False
    静默模式：headless 无头常驻，不弹窗口（2026-08-28 用户定稿）。"""
    # ⚠️ 2026-08-30 修复误判：ensure_debug_chrome 返回 False 既可能是「已存在可用」
    #    （_cdp_alive 为 True 时直接返回 False）也可能是「启动失败」；且启动失败后有
    #    10 分钟节流。不能拿返回值当存活判据——先确保浏览器在，再直接查 CDP 存活。
    cdp_core.ensure_debug_chrome(headless=True)
    if not cdp_core._cdp_alive():
        print('❌ 调试浏览器不可用（CDP 端口连不上），本轮刷新跳过，10 分钟后自动重试')
        return False
    ws, cdp = cdp_core.connect()
    if not ws:
        print('❌ 连接浏览器失败')
        return False
    try:
        _ensure_aeolus_page(cdp)
        info = cdp_core.fetch_init(cdp)
        for _ in range(6):
            if info:
                break
            time.sleep(3)
            info = cdp_core.fetch_init(cdp)
        if not info:            # 会话失效：先试从本机守护实例（9223）迁移登录 cookie 自愈
            print('⚠️ Aeolus 登录已失效，尝试从本机守护实例迁移登录态...')
            n_ok, n_bad = cdp_core.sync_cookies_from()
            if n_ok:
                print(f'🔑 已迁移登录 cookie {n_ok} 条（失败 {n_bad}），重试...')
                cdp_core.navigate_aeolus(cdp)
                for _ in range(4):
                    time.sleep(2)
                    info = cdp_core.fetch_init(cdp)
                    if info:
                        print(f'✅ 登录态已恢复：{info.get("user_name", "")}')
                        break
        if not info:            # 仍失效：把 Aeolus 窗口带到登录页，让使用者在同一个窗口重新登录（不顶任何会话）
            print('⚠️ Aeolus 登录已失效，请在 Aeolus 窗口中重新登录（该窗口即你的风神后台）...')
            cdp_core.navigate_aeolus(cdp)
            deadline = time.time() + 60   # headless 无窗口可登，死等只会撞 180s 看门狗，缩到 60s
            while time.time() < deadline:
                time.sleep(4)
                info = cdp_core.fetch_init(cdp)
                if info:
                    print('✅ 已重新登录')
                    break
            if not info:
                print('❌ 等待登录超时，请重试')
                return False
        if not teams:
            teams = [(info['team_id'], info['team_name'])]

        today = time.strftime('%Y-%m-%d')
        site_results = {}
        for team_id, team_name in teams:
            _SITE_NAMES[int(team_id)] = team_name   # 登记给档位速查 handler 用（见 _SITE_NAMES 注释）
            att_path = out_path.parent / f'live_att_{team_id}.xlsx'
            if att_path.exists():
                att_path.unlink()
            print(f'📥 拉取实时出勤（{team_name}）...')
            ok = fetch_attendance(cdp, team_id, att_path)
            if not ok:
                print(f'❌ 出勤数据拉取失败：{team_name}')
                continue
            print(f'📥 拉取排班统计（{team_name}）...')
            sched = fetch_schedule_stats(cdp, team_id, today)
            # 站点人数=排班统计全天行 teamKnightNum（与 Aeolus 后台「站点人数」一致）
            # ⚠️ 2026-09-18 用户校正：maplist knightList 行数口径实测比后台少 1 人，已废弃；
            #    fetch_knight_number 保留但不再调用
            print(f'👥 站点人数（排班统计）: {sched.get("site_total")}')
            # 月度运力标准（官方档位标准，API 直取；失败用内置默认）
            std = fetch_level_standard(cdp, team_id, today)
            if std:
                std_txt = '/'.join(f"标{i+1} {std['levels'][i]['orders']}单{std['levels'][i]['hours']}h" for i in range(5))
                print(f'📊 月度运力标准（{std["month"]}）：{std_txt}')
            else:
                print('⚠️ 月度运力标准拉取失败，使用内置默认（18/29/36/44/54单+6/6/6/7/8h）')
            # 运单改派/无效统计（2分钟缓存，实测提交→就绪2.3s；与订单识别共享全局提交锁防Aeolus限流；失败不阻断看板）
            order_stats = fetch_order_stats(cdp, team_id, today,
                                            cache_path=out_path.parent / f'live_order_{team_id}.json',
                                            max_age=120, site_name=team_name)
            # 违规/申诉数据 → 服务质量KPI（2分钟缓存，直接接口实测0.2s；失败不阻断）
            # 把运单侧的「订单判定不考核 / 是否高笔单」运单id 传进去，对齐晚复盘问题单的过滤
            _sk = (order_stats or (None,) * 5)[4] or {}
            appeal = fetch_appeal_stats(cdp, team_id, today,
                                        cache_path=out_path.parent / f'live_appeal_{team_id}.json',
                                        max_age=120,
                                        order_flags={'noexam': _sk.get('noexam_ids') or [],
                                                     'high': _sk.get('high_ids') or []})
            # 橙色风暴抽检（装备抽检不通过，2分钟缓存，实测0.2s；失败不阻断）
            qc = fetch_qc_stats(cdp, team_id, today,
                                cache_path=out_path.parent / f'live_qc_{team_id}.json',
                                max_age=120)
            # 待申诉/申诉失败 → 工作台「今日待办」（5分钟缓存；失败不阻断，前端读同目录 json）
            fetch_pending_appeals(cdp, team_id,
                                  cache_path=out_path.parent / f'live_pending_appeal_{team_id}.json',
                                  max_age=300)
            # 订单识别：三种特殊配送标品（2026-11-10 改 60 秒刷新；提交走全局锁与准时率错开 ≥30s，无新任务时复用旧快照不空窗）
            special = fetch_special_orders(cdp, team_id, today,
                                           cache_path=out_path.parent / f'live_special_{team_id}.json',
                                           max_age=60)
            riders, periods = parse_attendance(att_path)
            try:
                att_path.unlink()
            except Exception:
                pass
            if not riders:
                print(f'❌ 出勤解析失败：{team_name}')
                continue
            now = datetime.now()
            data = build_board(riders, periods, sched, now, order_stats, std, appeal, qc, special)
            # 准时率数据生成时间（live_order 缓存写入时刻，用于页面倒计时）
            try:
                _oc = out_path.parent / f'live_order_{team_id}.json'
                data['punctual']['gen_at'] = int(_oc.stat().st_mtime * 1000) if _oc.exists() else int(time.time() * 1000)
            except Exception:
                data['punctual']['gen_at'] = int(time.time() * 1000)
            site_results[team_id] = {'name': team_name, 'data': data}
    except Exception as e:
        # ⚠️ 2026-08-30 防崩溃：CDP websocket 异常（如调试 Chrome 被外部清理/被杀）
        #    此前直接抛出 → run() 无兜底 → 守护进程崩溃 → launchd 反复重启 → 看板假死
        print(f'❌ 刷新异常（本轮跳过，下轮重试）: {type(e).__name__}: {e}')
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not site_results:
        print('❌ 所有站点数据拉取失败')
        return False

    html = render_multi(site_results, interval)
    tmp = out_path.with_name(out_path.name + '.tmp')
    tmp.write_text(html, encoding='utf-8')
    os.replace(tmp, out_path)  # 原子替换，避免动态刷新读到半个文件
    print(f'✅ 看板已更新：{out_path}  ({datetime.now().strftime("%H:%M:%S")}) · {len(site_results)}个站点')
    return True

def _open_board_tab(url):
    """在调试 Chrome（风神后台窗口）里新开标签页打开看板——与 Aeolus 同一个窗口，
    标签页切换即可，不再弹第二个独立浏览器窗口（2026-08-30 用户要求）。
    失败时退回默认浏览器打开。"""
    try:
        import urllib.request
        from urllib.parse import quote
        from modules import cdp_core
        req = urllib.request.Request(
            f'{cdp_core.CDP_URL}/json/new?{quote(url, safe="")}', method='PUT')
        r = urllib.request.urlopen(req, timeout=5)
        d = json.loads(r.read().decode('utf-8'))
        if d and d.get('id'):
            return True
    except Exception:
        pass
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    return False


# 看板服务端口（打开看板按钮用；run() 启动后更新为实际端口）
_BOARD_PORT = 8765


def _board_url():
    return f'http://127.0.0.1:{_BOARD_PORT}/'


def _publish_atomic(src, dest):
    """原子发布 index.html：写临时文件 + os.replace。
    ⚠️ 2026-09-26：这里原来是 shutil.copy，会先截断再写 —— 页面每 60s 自动 reload，
    正好撞上复制中间态就会读到「写了一半」的看板页（表现为表格列/数据缺失、页面看着乱）。
    out_path 的生成早已用 tmp+os.replace，发布这一环也必须原子。"""
    try:
        src, dest = Path(src), Path(dest)
        tmp = dest.with_name(dest.name + '.pubtmp')
        with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
            shutil.copyfileobj(fi, fo)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dest)
    except Exception as e:
        print(f'⚠️ 发布 index.html 失败（回退直接复制）: {e}')
        try:
            shutil.copy(src, dest)
        except Exception:
            pass


def _bootstrap_workbench(out_dir):
    """首次运行：把内置页面（assets/*.html）释放到 data/，缺失才写、绝不覆盖用户改过的版本。"""
    dst_dir = Path(out_dir)
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    src_dir = base / 'assets'
    for name in ('workbench.html', 'dash-c2.html', 'kpi-c2.html'):
        dst = dst_dir / name
        if dst.exists():
            continue
        try:
            src = src_dir / name
            if src.exists():
                shutil.copy2(src, dst)
                print(f'✅ 已释放内置页面：{name}')
        except Exception as e:
            print(f'⚠️ 释放 {name} 失败: {e}')
    # KPI 目标值：没有就用示例值起一份（必须按本月考核方案核对后再用）
    kt = dst_dir / 'kpi_targets.json'
    ex = dst_dir / 'kpi_targets.example.json'
    if not kt.exists() and ex.exists():
        try:
            shutil.copy2(ex, kt)
            print('✅ 已用示例 KPI 目标值初始化 data/kpi_targets.json（请按本月方案核对）')
        except Exception as e:
            print(f'⚠️ 初始化 kpi_targets.json 失败: {e}')


# ── 夜间静默（2026-10-29 用户定稿）──────────────────────────────────────────
# 看板原来 24 小时抓数，headless Chrome 常驻占 ~400MB。营业时间 06:00–次日02:00
# （2026-10-30 用户明确），静默窗口 = 02:00–06:00 无人时段
# 无人看板，这段时间停抓数并关掉浏览器。实测调 Chrome 启动参数**不减内存**
# （402MB→423MB 持平），只有减少常驻时间才有效，别再试图调参压缩。
_QUIET_START_MIN = 2 * 60        # 02:00（后半夜2点收工后）
_QUIET_END_MIN = 6 * 60          # 06:00（早班开工前）


def in_quiet_window(now=None):
    """当前是否处于夜间静默时段。

    兼容两种区间写法：起点<终点=同日区间（静默 02:00–06:00），
    起点>终点=跨零点区间（老配置 23:00–06:30）。改窗口时只需改上面两个常量。
    """
    now = now or time.localtime()
    m = now.tm_hour * 60 + now.tm_min
    if _QUIET_START_MIN <= _QUIET_END_MIN:
        return _QUIET_START_MIN <= m < _QUIET_END_MIN
    return m >= _QUIET_START_MIN or m < _QUIET_END_MIN


def enter_quiet_mode(status_cb=None):
    """进入夜间静默：关掉抓数浏览器，释放内存。"""
    try:
        cdp_core.cleanup_chrome(True)
    except Exception as e:
        print(f'⚠️ 夜间静默：关闭浏览器失败 {e}')
    print('🌙 进入夜间静默（02:00–06:00）：已关闭抓数浏览器，释放约 400MB 内存')
    if status_cb:
        status_cb(refresh=f"{time.strftime('%H:%M:%S')} 🌙 夜间静默（02:00–06:00 不抓数）")


def sleep_until_quiet_end(stop_event=None, step=60):
    """静默期内分段睡眠。到点返回 True；收到停止信号返回 False。"""
    while in_quiet_window():
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(step)
    return True


def run(teams, interval=60, out_dir=None, status_cb=None, stop_event=None):
    """持续刷新循环（本地服务动态刷新）。teams=[(team_id, team_name), ...]
    status_cb：每轮刷新后回调状态 dict（控制台窗口显示用，可 None）
    stop_event：threading.Event，置位后停止循环（控制台窗口关闭=退出程序）"""

    # ── 单轮刷新看门狗：refresh_once 偶发永久阻塞（CDP recv/网络挂起，2026-09-02 实测
    #    20:36→20:45 守护卡死 CPU0 无产出）时 SIGALRM 强制中断，防整进程假死 ──
    def _refresh_once_watchdog(timeout=180):
        def _alarm(signum, frame):
            raise TimeoutError(f'refresh_once 超过 {timeout}s 无响应（看门狗中断）')
        try:
            signal.signal(signal.SIGALRM, _alarm)
        except Exception:
            return refresh_once(teams, out_path, interval)  # 非主线程/无信号环境退化为直调
        signal.alarm(timeout)
        try:
            ok = refresh_once(teams, out_path, interval)
        except Exception as e:
            print(f'⚠️ 单轮刷新异常/看门狗中断: {e}')
            ok = False
        finally:
            signal.alarm(0)
        return ok
    global _BOARD_PORT
    if out_dir is None:
        out_dir = cdp_core.data_dir()
    out_path = out_dir / '实时看板.html'
    out_dir.mkdir(parents=True, exist_ok=True)
    _bootstrap_workbench(out_dir)   # 分发版首次运行释放内置工作台（不含安全台账）
    # 加载使用者自定义 KPI 目标值（分发版 data/kpi_targets.json，无则内置默认）
    _load_kpi_targets()
    print(f'📺 实时看板启动：每 {interval} 秒刷新一次（Ctrl+C 停止）')
    print(f'   看板文件：{out_path}')
    print('🪟 已打开 Aeolus 窗口（可最小化）——这个窗口就是你的风神后台，')
    print('   请在里面正常操作；不要再用主浏览器登录同一账号（会互相顶号）')
    # 本地服务：页面动态刷新（不整页重载，保留筛选状态）
    server, port = start_http_server(out_dir)
    if server:
        _BOARD_PORT = port
    if status_cb:
        status_cb(server=f'http://127.0.0.1:{port}/ 运行中' if server
                  else f'本地服务启动失败（端口 {port}）')
        try:
            _ip = lan_ip()
            if _ip != '127.0.0.1':
                status_cb(lan=f'http://{_ip}:{port}/（手机连同一WiFi）')
            else:
                status_cb(lan=f'http://127.0.0.1:{port}/（未检测到局域网）')
        except Exception:
            pass
    # 启动时若已在夜间静默时段：不拉浏览器，直接睡到 06:00（省 ~400MB）
    if in_quiet_window():
        enter_quiet_mode(status_cb)
        if not sleep_until_quiet_end(stop_event):
            print('\n👋 看板已停止')
            try:
                server.server_close()
            except Exception:
                pass
            return
    import webbrowser
    ok = _refresh_once_watchdog()
    if status_cb:
        status_cb(refresh=f"{time.strftime('%H:%M:%S')} "
                  f"{'✅ 数据正常' if ok else '⚠️ 刷新失败，自动重试中'}")
    if ok:
        try:
            _publish_atomic(out_path, out_dir / 'index.html')
        except Exception:
            pass
        if server:
            _open_board_tab(f'http://127.0.0.1:{port}/')
        else:
            _open_board_tab(str(out_path))
    while True:
        if stop_event is not None and stop_event.is_set():
            print('\n👋 看板已停止（控制台窗口已关闭）')
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print('\n👋 看板已停止')
            break
        # 夜间静默：停抓数 + 关浏览器（醒来后立即抓一轮，不等下一个 interval）
        if in_quiet_window():
            enter_quiet_mode(status_cb)
            if not sleep_until_quiet_end(stop_event):
                print('\n👋 看板已停止')
                break
        ok = _refresh_once_watchdog()
        if status_cb:
            status_cb(refresh=f"{time.strftime('%H:%M:%S')} "
                      f"{'✅ 数据正常' if ok else '⚠️ 刷新失败，自动重试中'}")
        # ⚠️ 只有刷新成功才覆盖 index.html（2026-08-30 修复：失败时无条件 copy
        #    会把旧文件当新数据发布，页面时间戳在跳但内容永远停在最后一次成功）
        if ok and server:
            try:
                _publish_atomic(out_path, out_dir / 'index.html')
            except Exception:
                pass
    # 循环退出：关掉本地服务（进程随后由调用方结束）
    try:
        server.server_close()
    except Exception:
        pass

if __name__ == '__main__':
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    run(None, interval)
