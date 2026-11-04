#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""09:25 集合竞价成交档案：把「竞价量/竞价额」按交易日落盘，供事后回测。

为什么需要它
------------
竞价成交额只在 09:25:00-09:25:59 这个窗口里以"当日累计额"的形态存在过：
连续竞价 09:30 一开，快照里的成交额就变成累计值，事后无法再拆出来。
所以「9:25 竞价档」是一次性数据，不存就永久丢失。

数据源
------
Tushare 爬虫接口 ts.realtime_quote（批量 600 只/次、秒级、无限频）；
抓完用腾讯 qt.gtimg.cn 抽检 10 只，误差 >2% 报警（防接口口径漂移）。

用法
----
python3 snapshot_925.py           # cron 在交易日 09:25 调用（自动等快照跨过 09:25）
python3 snapshot_925.py --force   # 跳过时间闸门（仅调试，落盘会标 mode=cumulative）
输出：~/.hermes/cache/f2s_rank/snap_925_YYYYMMDD.pkl
"""
import argparse
import datetime
import os
import sys

# cron 用系统 python 跑 .py（无 pandas），必须先自举到装了 pandas 的解释器
if os.path.exists('/usr/local/bin/python3') and os.path.realpath(sys.executable) != os.path.realpath('/usr/local/bin/python3'):
    os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3', os.path.abspath(__file__)] + sys.argv[1:])

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))

import pandas as pd  # noqa: E402

from premarket_dragon_scan import pro, fetch_quotes, last_prev_day  # noqa: E402

CACHE = os.path.expanduser('~/.hermes/cache/f2s_rank')
os.makedirs(CACHE, exist_ok=True)


def all_hs_codes():
    b = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,market')
    b = b[~b.ts_code.str.endswith('.BJ')]
    return b.ts_code.tolist(), dict(zip(b.ts_code, b.name))


def tencent_check(codes):
    """腾讯快照抽检：返回 [(code, 腾讯量手, 我们的量手, 相对误差%)]"""
    import ssl
    import urllib.request
    ctx = ssl.create_default_context(cafile='/etc/ssl/cert.pem')
    q = ','.join(('sh' if c[0] == '6' else 'sz') + c[:6] for c in codes)
    try:
        raw = urllib.request.urlopen(
            f'https://qt.gtimg.cn/q={q}', timeout=10, context=ctx
        ).read().decode('gbk', 'ignore')
    except Exception as e:
        return [('ERR', str(e), '', '')]
    idx = {c: i for i, c in enumerate(codes)}
    res, i = [], 0
    for line in raw.split(';'):
        line = line.strip()
        if '="' not in line:
            continue
        f = line.split('"')[1].split('~')
        if len(f) < 10:
            continue
        code = codes[i] if i < len(codes) else f[2]
        i += 1
        try:
            res.append((code, float(f[6]), float(f[3])))
        except Exception:
            pass
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='跳过 09:24-09:40 时间闸门（调试）')
    ap.add_argument('--limit', type=int, default=0, help='只抓前 N 只（调试）')
    a = ap.parse_args()

    now = datetime.datetime.now()
    today = now.strftime('%Y%m%d')
    hm = now.strftime('%H:%M')
    # 09:25-09:30 之间连续竞价还没开始：快照里的累计成交量/额 = 纯集合竞价成交
    in_window = '09:25' <= hm < '09:30'
    if not in_window and not a.force:
        print(f'【SKIP】现在 {hm}，不在 09:25-09:30 竞价窗口；要调试请加 --force')
        return

    is_td, prev = last_prev_day(today)
    if not is_td and not a.force:
        print(f'【SKIP】{today} 不是 A 股交易日')
        return

    codes, name_map = all_hs_codes()
    if a.limit:
        codes = codes[:a.limit]
    print(f'→ 抓取 {len(codes)} 只的 {hm} 快照 …')
    q = fetch_quotes(codes, need_time=in_window)
    if q is None or not len(q):
        print('【FAIL】快照取数失败（爬虫与东财兜底都不可用）')
        return

    ts = str(q.attrs.get('ts', '?'))
    src = str(q.attrs.get('src', '?'))
    # 只有 09:25:xx 的快照才是纯竞价；09:30 之后是累计额
    # 窗口口径：09:25:00-09:29:59 内任意一次快照都是竞价档（这段没有连续竞价撮合）
    mode = 'auction' if '09:25' <= ts[:5] < '09:30' else 'cumulative'
    q = q.dropna(subset=['price', 'pre'])
    q = q[q.pre > 0].copy()
    q['pct'] = (q.price / q.pre - 1) * 100
    q['amt'] = q.amount.astype(float)
    q['vol_hand'] = q.amt / (q.price * 100)
    out = q[['code', 'name', 'price', 'pre', 'pct', 'amt', 'vol_hand']].copy()
    out['ts'], out['src'], out['mode'] = ts, src, mode

    # 腾讯抽检
    chk = tencent_check(out.code.tolist()[:10]) if len(out) >= 10 else []
    bad = []
    for code, tc_vol, _ in chk:
        if code == 'ERR':
            print(f'⚠ 腾讯抽检失败：{tc_vol}')
            continue
        row = out[out.code == code]
        if not len(row):
            continue
        mine = float(row.vol_hand.iloc[0])
        if mine > 0 and abs(tc_vol / mine - 1) > 0.02:
            bad.append((code, tc_vol, round(mine, 1), round((tc_vol / mine - 1) * 100, 2)))
    print(f'   腾讯抽检 {len(chk)} 只，偏差 >2% 的 {len(bad)} 只' + (f'：{bad[:3]}' if bad else ''))

    tag = '' if mode == 'auction' else '_TEST'
    # 文件名带 all：snap_925_YYYYMMDD.pkl 是 9:25/9:35 流水线自己写的基线文件（列 amount/ts），
    # 不能覆盖它，否则 09:35 相减会读错列。
    p = os.path.join(CACHE, f'snap_925_all_{today}{tag}.pkl')
    pd.to_pickle(out, p)
    print(f'【OK】{today} 快照 {ts}（{src}，mode={mode}）覆盖 {len(out)} 只')
    print(f'   全市场竞价额合计 {out.amt.sum() / 1e8:.0f} 亿｜红盘 {int((out.pct > 0).sum())}/{len(out)}'
          f'（{(out.pct > 0).mean() * 100:.1f}%）｜中位 {out.pct.median():+.2f}%')
    print(f'   → {p}')
    if mode != 'auction':
        print('   ⚠ mode=cumulative：这不是竞价档，不可用于「竞价占昨全天」类指标')


if __name__ == '__main__':
    main()
