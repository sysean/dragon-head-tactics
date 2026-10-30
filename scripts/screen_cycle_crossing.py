#!/usr/bin/env python3
"""跨周期存活（「穿越」）全市场扫描。

回答「现在市场上哪只股是穿越周期的龙」时先跑这个，再人工定性；不要凭记忆点名。

用法::

    python3 screen_cycle_crossing.py --start 20260701 --end 20261021
    python3 screen_cycle_crossing.py --start 20260701 --end 20261021 \\
        --detail 002696.SZ,002724.SZ          # 对头部候选打印涨停日序列/席位/披露日

口径：涨停按板块阈值（主板 9.7 / 创科 19.7 / 北交所 29.7）；穿越次数 = 收盘从区间高点回撤
≥15% 后重新创出收盘新高；存活 = 距最高收盘 > -5% 且收盘在 MA10 上方；次新剔除。

缓存：~/.hermes/cache/limitpool/（首次建约 2-4 分钟；本机 pandas 无 pyarrow，用 pickle/CSV）。
"""
import argparse
import os
import pathlib
import time

import numpy as np
import pandas as pd
import tushare as ts

CACHE = pathlib.Path.home() / '.hermes' / 'cache' / 'limitpool'
NEW_STOCK_DAYS = 120


def pro_api():
    return ts.pro_api(os.environ['TUSHARE_TOKEN'])


def trade_days(pro, start, end):
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
    return sorted(cal.cal_date.tolist())


def build_cache(pro, start, end, days, force=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    tag = '%s_%s' % (start, end)
    pool_f = CACHE / ('pool_%s.csv' % tag)
    daily_f = CACHE / 'daily_panel.pkl'
    basic_f = CACHE / 'basic_panel.pkl'
    if pool_f.exists() and daily_f.exists() and not force:
        print('[cache] 复用 %s' % pool_f)
        return pool_f
    pool, dp, bp = [], [], []
    for dd in days:
        try:
            l = pro.limit_list_d(trade_date=dd)   # 必须逐日：整段拉会在 6000 行截断
            if len(l):
                pool.append(l)
        except Exception as exc:                  # 单日失败不拖垮整轮
            print('[warn] limit_list_d %s %s' % (dd, str(exc)[:60]))
        try:
            a = pro.daily(trade_date=dd)
            if len(a):
                dp.append(a)
        except Exception as exc:
            print('[warn] daily %s %s' % (dd, str(exc)[:60]))
        try:
            b = pro.daily_basic(trade_date=dd,
                                fields='ts_code,trade_date,turnover_rate_f,circ_mv,pe_ttm')
            if len(b):
                bp.append(b)
        except Exception as exc:
            print('[warn] daily_basic %s %s' % (dd, str(exc)[:60]))
        time.sleep(0.05)
    pd.concat(pool, ignore_index=True).to_csv(pool_f, index=False)
    pd.concat(dp, ignore_index=True).to_pickle(daily_f)
    pd.concat(bp, ignore_index=True).to_pickle(basic_f)
    print('[cache] %d 日 %d 条涨停 -> %s' % (len(days), len(pd.concat(pool)), pool_f))
    return pool_f


def load(pool_f):
    L = pd.read_csv(pool_f, dtype={'trade_date': str})
    L = L[L.limit == 'U'].copy()
    L['lt'] = pd.to_numeric(L.limit_times, errors='coerce')
    D = pd.read_pickle(CACHE / 'daily_panel.pkl')
    B = pd.read_pickle(CACHE / 'basic_panel.pkl')
    return L, D, B


def cross_count(close):
    """回撤 >=15% 后重新创出收盘新高，记一次穿越。"""
    cross, peak, deep = 0, close.iloc[0], False
    for _, px in close.items():
        if px / peak - 1 <= -0.15:
            deep = True
        if px > peak:
            if deep:
                cross += 1
                deep = False
            peak = px
    return cross


def screen(pro, L, D, B, min_zt=6, new_days=NEW_STOCK_DAYS, start=None):
    sb = pro.stock_basic(fields='ts_code,name,industry,list_date')
    agg = (L.groupby('ts_code')
             .agg(涨停=('trade_date', 'nunique'), 首板=('trade_date', 'min'),
                  末板=('trade_date', 'max'), 最大板=('lt', 'max'))
             .reset_index()
             .merge(sb, on='ts_code', how='left'))
    if start:
        cut = (pd.Timestamp(start) - pd.Timedelta(days=new_days)).strftime('%Y%m%d')
        agg = agg[agg.list_date < cut]
    agg = agg[agg['涨停'] >= min_zt]

    days = sorted(D.trade_date.unique())
    close = D.pivot_table(index='trade_date', columns='ts_code', values='close').reindex(days)
    last_basic = B[B.trade_date == B.trade_date.max()].set_index('ts_code')

    rows = []
    for code in agg.ts_code:
        s = close[code].dropna() if code in close else pd.Series(dtype=float)
        if len(s) < 40:
            continue
        ma10 = s.rolling(10).mean().iloc[-1]
        last, hi = s.iloc[-1], s.max()
        rows.append({
            'ts_code': code,
            'name': agg.set_index('ts_code').at[code, 'name'],
            '行业': agg.set_index('ts_code').at[code, 'industry'],
            '涨停': int(agg.set_index('ts_code').at[code, '涨停']),
            '最大板': int(agg.set_index('ts_code').at[code, '最大板'] or 0),
            '穿越次数': cross_count(s),
            '距最高收盘%': round((last / hi - 1) * 100, 1),
            '距MA10%': round((last / ma10 - 1) * 100, 1),
            '最大回撤%': round((s / s.cummax() - 1).min() * 100, 1),
            '末板': agg.set_index('ts_code').at[code, '末板'],
            '流通亿': round(last_basic.circ_mv.get(code, np.nan) / 1e4, 1)
            if code in last_basic.index else np.nan,
        })
    R = pd.DataFrame(rows).sort_values(['穿越次数', '涨停'], ascending=False)
    return R


def detail(pro, L, D, B, code, bars=34):
    s = D[D.ts_code == code].set_index('trade_date').sort_index()
    b = B[B.ts_code == code].set_index('trade_date')
    s['ma10'] = s.close.rolling(10).mean()
    zt = set(L[L.ts_code == code].trade_date)
    name = s.index[-1]
    print('\n--- %s %s ---' % (code, name))
    for dt, r in s.tail(bars).iterrows():
        turn = b.turnover_rate_f.get(dt, np.nan)
        print('%s %s c=%8.2f (%+6.2f%%) hi=%8.2f lo=%8.2f amt=%6.2f亿 换手=%6.2f%% ma10=%7.2f'
              % (dt[4:], 'Z' if dt in zt else ' ', r.close, r.pct_chg, r.high, r.low,
                 r.amount / 1e5, turn, r.ma10))
    k = pro.kpl_list(ts_code=code)
    if len(k):
        print('题材序列:')
        print(k.sort_values('trade_date')[['trade_date', 'lu_desc', 'theme', 'status']]
               .tail(10).to_string(index=False))
    print('最近上榜日与席位:')
    for dd in sorted(L[L.ts_code == code].trade_date)[-5:]:
        try:
            t = pro.top_list(trade_date=dd)
        except Exception:
            continue
        r = t[t.ts_code == code] if len(t) else t
        if not len(r):
            continue
        print('  %s %s' % (dd, r.reason.iloc[0]))
        ti = pro.top_inst(trade_date=dd, ts_code=code)
        if len(ti):
            print(ti[['exalter', 'side', 'buy', 'sell']].head(6).to_string(index=False))
    try:
        sd = pro.suspend_d(ts_code=code, start_date=L.trade_date.min(), end_date=L.trade_date.max())
        if len(sd):
            print('停牌记录（S=停，R=复）:')
            print(sd.to_string(index=False))
    except Exception:
        pass
    try:
        dd = pro.disclosure_date(ts_code=code, end_date='20260930')
        if len(dd):
            print('三季报预约披露:', dd.pre_date.tolist())
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--min-zt', type=int, default=6)
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--detail', default='')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()

    pd.set_option('display.width', 320)
    pd.set_option('display.max_columns', 60)
    pro = pro_api()
    days = trade_days(pro, a.start, a.end)
    pool_f = build_cache(pro, a.start, a.end, days, force=a.force)
    L, D, B = load(pool_f)
    R = screen(pro, L, D, B, min_zt=a.min_zt, start=a.start)
    print('\n候选 %d 只（涨停>=%d 且非次新），按穿越次数排序：' % (len(R), a.min_zt))
    print(R.head(a.top).to_string(index=False))
    print('\n同时满足：穿越>=1 且 距最高收盘>-5%% 且 MA10 上方')
    alive = R[(R['穿越次数'] >= 1) & (R['距最高收盘%'] > -5) & (R['距MA10%'] > 0)]
    print(alive.to_string(index=False) if len(alive) else '  无')
    if a.detail:
        for code in [c.strip() for c in a.detail.split(',') if c.strip()]:
            detail(pro, L, D, B, code)


if __name__ == '__main__':
    main()
