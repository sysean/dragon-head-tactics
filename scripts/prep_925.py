#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""前一晚 21:00 预取次日 9:25 需要的数据，落盘成一个缓存包。

为什么：9:25 那一次运行要在 30 秒内算完全市场，Tushare 的 limit_list_d / daily_basic /
daily / kpl_list 每个都要一两秒，串起来容易超过竞价窗口。前一晚（T 日收盘后数据已全量入库）
先把这些取好、算好派生字段，9:25 只读本地缓存 + 取一次实时竞价快照。

产物：~/.hermes/cache/f2s_rank/prep_YYYYMMDD.pkl
{
  'trade_date': '20261103',      # 数据所属交易日（= 次日的"昨日"）
  'mkt_yi': 17151.0,             # 全市场成交额（亿元）
  'first': DataFrame,            # 首板池 + 派生字段
  'ind_cnt': dict, 'ind_max': dict,   # 行业口径：涨停家数 / 最高板
  'theme_cnt': dict,             # 题材口径（kpl_list.theme 拆分）涨停家数 —— 板块兜底
}
"""
import os
import sys

if os.path.exists('/usr/local/bin/python3') and os.path.realpath(sys.executable) != os.path.realpath('/usr/local/bin/python3'):
    os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3', os.path.abspath(__file__)] + sys.argv[1:])

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))

import datetime  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from premarket_dragon_scan import pro, trading_days, last_prev_day  # noqa: E402

CACHE = os.path.expanduser('~/.hermes/cache/f2s_rank')
os.makedirs(CACHE, exist_ok=True)


def main():
    today = datetime.datetime.now().strftime('%Y%m%d')
    if len(sys.argv) > 1 and sys.argv[1].isdigit():   # 回放：python3 prep_925.py 20261103
        today = sys.argv[1]
        target = today
        return _prep(target)
    is_td, nd = last_prev_day(today)
    return _prep(today if is_td else nd)


def _prep(target):
    today = datetime.datetime.now().strftime('%Y%m%d')
    if not target:
        print('【SKIP】找不到可用的交易日')
        return
    # 下一交易日存在才值得准备（长假最后一天也照做）
    days = trading_days((datetime.datetime.strptime(target, '%Y%m%d') - datetime.timedelta(days=10)).strftime('%Y%m%d'), today)
    if target not in days:
        print(f'【SKIP】{target} 不在交易日历')
        return

    # 1) 涨停池（U）与炸板池（Z）
    u = pro.limit_list_d(trade_date=target, limit_type='U')
    if u is None or not len(u):
        print(f'【SKIP】{target} 涨停池为空（可能尚未入库）')
        return
    for c in ('fd_amount', 'amount', 'limit_times', 'turnover_ratio'):
        u[c] = pd.to_numeric(u[c], errors='coerce')
    u = u[~u.ts_code.str.endswith('.BJ')]
    u = u[~u.name.fillna('').str.contains('ST')]

    # 2) 全市场成交额 + 量比 + 前5日均额 + MA10
    d = pro.daily(trade_date=target, fields='ts_code,trade_date,open,high,low,close,pre_close,pct_chg,amount')
    mkt_yi = float(d['amount'].sum()) / 1e5
    b = pro.daily_basic(trade_date=target, fields='ts_code,turnover_rate,turnover_rate_f,circ_mv,volume_ratio')
    hist = pro.daily(start_date=(datetime.datetime.strptime(target, '%Y%m%d') - datetime.timedelta(days=30)).strftime('%Y%m%d'),
                     end_date=target, ts_code=','.join(u.ts_code.tolist()[:50]), fields='ts_code,trade_date,close,amount')
    frames = [hist] if hist is not None and len(hist) else []
    codes = u.ts_code.tolist()
    if len(codes) > 50:
        for i in range(50, len(codes), 50):
            h = pro.daily(start_date=(datetime.datetime.strptime(target, '%Y%m%d') - datetime.timedelta(days=30)).strftime('%Y%m%d'),
                          end_date=target, ts_code=','.join(codes[i:i + 50]), fields='ts_code,trade_date,close,amount')
            if h is not None and len(h):
                frames.append(h)
    hist = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    adv5, ma10 = {}, {}
    if len(hist):
        hist = hist.sort_values(['ts_code', 'trade_date'])
        for code, g in hist.groupby('ts_code'):
            a = g['amount'].shift(1).tail(5)
            if a.notna().sum() >= 3:
                adv5[code] = float(a.mean()) / 1e5
            if len(g) >= 10:
                ma10[code] = float(g['close'].tail(10).mean())

    # 3) 首板池 + 派生
    first = u[u.limit_times == 1].copy()
    first['amt_yi'] = first.amount / 1e8
    first['fd_pct'] = first.fd_amount / first.amount * 100
    first['adv5_yi'] = first.ts_code.map(adv5)
    first['ma10'] = first.ts_code.map(ma10)
    vr = dict(zip(b.ts_code, b.volume_ratio)) if b is not None and len(b) else {}
    trf = dict(zip(b.ts_code, b.turnover_rate_f)) if b is not None and len(b) else {}
    cmv = dict(zip(b.ts_code, b.circ_mv)) if b is not None and len(b) else {}
    first['vr'] = first.ts_code.map(vr)
    first['turnover_f'] = first.ts_code.map(trf)
    first['circ_mv_yi'] = first.ts_code.map(cmv) / 1e4

    # 4) 行业口径 / 题材口径家数（题材口径作为板块兜底）
    ind_cnt = u.groupby('industry').size().to_dict()
    ind_max = u.groupby('industry').limit_times.max().to_dict()
    theme_cnt, stock_themes, theme_max = {}, {}, {}
    try:
        k = pro.kpl_list(trade_date=target, limit_type='U')
        if k is not None and len(k):
            for _, kr in k.iterrows():
                ts = [x.strip() for x in str(kr.get('theme') or '').replace('，', '、').split('、') if x.strip()]
                stock_themes[kr['ts_code']] = ts
                lt = int(kr.get('limit_times') or 1) if str(kr.get('limit_times') or '1').isdigit() else 1
                for t in ts:
                    theme_cnt[t] = theme_cnt.get(t, 0) + 1
                    theme_max[t] = max(theme_max.get(t, 1), lt)
    except Exception:
        pass
    first['themes'] = first.ts_code.map(lambda c: stock_themes.get(c, []))

    pack = dict(trade_date=target, mkt_yi=mkt_yi, first=first,
                ind_cnt=ind_cnt, ind_max=ind_max, theme_cnt=theme_cnt, theme_max=theme_max)
    out = os.path.join(CACHE, f'prep_{target}.pkl')
    pd.to_pickle(pack, out)
    print(f'【OK】{target} 预取完成：首板 {len(first)} 只 ｜ 全市场 {mkt_yi:.0f} 亿 ｜ '
          f'行业 {len(ind_cnt)} 个 ｜ 题材 {len(theme_cnt)} 个 ｜ 量比覆盖 {first.vr.notna().sum()}/{len(first)}')
    print(f'   → {out}')


if __name__ == '__main__':
    main()
