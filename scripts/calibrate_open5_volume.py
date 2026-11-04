#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""开盘量能门槛校准：首 5 分钟（含集合竞价）占当日全天的分布，以及它与「量能比」的对应关系。

为什么需要它
------------
盘中量能判据是「amt(09:35) − amt(09:25) ÷ 首板全天」——分钟线接口（rt_min / stk_mins）限频不能用，
所以门槛只能靠事后校准，不能凭感觉定。定 8% 时全市场约 60% 的票都能过（等于没筛），校准后改 10%。

数据源
------
- 分钟成交额：新浪 `https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sz000001&scale=5&ma=no&datalen=N`
  首根 09:35 的 `amount` 已含 09:25 集合竞价，与「09:25 累计额 → 09:35 累计额」口径一致（元）。
- 全天成交额：Tushare `daily.amount`（注意单位是千元，要 ×1000 换算成元）。
- 首板池：`limit_list_d(limit_type='U')` 里 `limit_times == 1`。

用法
----
    python3 calibrate_open5_volume.py --pool-date 20261102 --dist-only
    python3 calibrate_open5_volume.py --pool-date 20261030 --next-date 20261102
"""
import os
import sys
import json
import ssl
import argparse
import urllib.request
import warnings

if sys.executable != '/usr/local/bin/python3':
    try:
        import pandas  # noqa: F401
    except ImportError:
        os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3'] + sys.argv)

import certifi  # noqa: E402
import pandas as pd  # noqa: E402
import tushare as ts  # noqa: E402

warnings.filterwarnings('ignore')

TOK = os.environ.get('TUSHARE_TOKEN')
if not TOK:
    p = os.path.expanduser('~/.hermes/.env')
    if os.path.exists(p):
        for line in open(p):
            if line.startswith('TUSHARE_TOKEN'):
                TOK = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
pro = ts.pro_api(TOK)
CTX = ssl.create_default_context(cafile=certifi.where())


def bars(code, n=400):
    sym = ('sh' if code.startswith('6') else 'sz') + code[:6]
    url = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
           f'?symbol={sym}&scale=5&ma=no&datalen={n}')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                               'Referer': 'https://finance.sina.com.cn/'})
    return json.loads(urllib.request.urlopen(req, timeout=15, context=CTX).read().decode())


def day_bars(code, date8, n=400):
    """date8: YYYYMMDD → 当日 5 分钟 K 列表（day 字段形如 2026-11-02 09:35:00）"""
    d = f'{date8[:4]}-{date8[4:6]}-{date8[6:]}'
    return [x for x in bars(code, n) if str(x['day']).startswith(d)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool-date', required=True, help='首板池日期 YYYYMMDD')
    ap.add_argument('--next-date', default=None, help='二板日 YYYYMMDD（给了才算量能比对应）')
    ap.add_argument('--limit', type=int, default=70, help='最多扫多少只（新浪逐只请求，别开太大）')
    ap.add_argument('--dist-only', action='store_true', help='只出分布，不算量能比')
    a = ap.parse_args()

    u = pro.limit_list_d(trade_date=a.pool_date, limit_type='U')
    if u is None or not len(u):
        sys.exit(f'{a.pool_date} 涨停池为空')
    u['lt'] = pd.to_numeric(u['limit_times'], errors='coerce').fillna(0).astype(int)
    first = u[u['lt'] == 1]

    amt_pool = pro.daily(trade_date=a.pool_date)[['ts_code', 'amount']].set_index('ts_code')
    amt_next = None
    if a.next_date and not a.dist_only:
        amt_next = pro.daily(trade_date=a.next_date)[['ts_code', 'amount']].set_index('ts_code')

    rows = []
    for _, r in first.head(a.limit).iterrows():
        code = r['ts_code']
        if code not in amt_pool.index:
            continue
        try:
            db = day_bars(code, a.pool_date)
        except Exception:
            continue
        if not db:
            continue
        first5 = float(db[0]['amount'])
        full = float(amt_pool.loc[code, 'amount']) * 1000     # 千元 → 元
        if full <= 0:
            continue
        rec = dict(code=code, name=r['name'], r5=first5 / full * 100)
        if amt_next is not None and code in amt_next.index:
            rec['vr'] = float(amt_next.loc[code, 'amount']) / float(amt_pool.loc[code, 'amount'])
        rows.append(rec)

    df = pd.DataFrame(rows)
    if not len(df):
        sys.exit('无样本')
    print(f'首板池 {a.pool_date}：样本 {len(df)} 只')
    print('首 5 分钟（含集合竞价）占当日全天：'
          f"P10 {df.r5.quantile(.1):.1f}% | P25 {df.r5.quantile(.25):.1f}% | ".
          replace('\n', ' ')
          + f'中位 {df.r5.median():.1f}% | P75 {df.r5.quantile(.75):.1f}% | P90 {df.r5.quantile(.9):.1f}%')
    for th in (8, 10, 12, 15):
        print(f'  ≥{th}% 的票占 {(df.r5 >= th).mean() * 100:.0f}%（门槛越松越没筛）')

    if 'vr' in df.columns:
        df['bin'] = pd.cut(df.r5, [0, 5, 8, 10, 12, 15, 20, 100],
                           labels=['<5', '5-8', '8-10', '10-12', '12-15', '15-20', '>20'])
        g = df.groupby('bin', observed=True).agg(
            n=('vr', 'size'),
            量能比中位=('vr', 'median'),
            量能比均值=('vr', 'mean'),
            达标率_1p5=('vr', lambda x: round((x >= 1.5).mean() * 100, 1)))
        print(f'\n→ {a.next_date} 二板日的量能比（二板全天 ÷ 首板全天）按开盘占比分档：')
        print(g.round(2).to_string())
        print('\n判读：门槛要落在「量能比中位 ≈1.5」那一档，不是越高越好；样本 <50 只时只作方向参考。')


if __name__ == '__main__':
    main()
