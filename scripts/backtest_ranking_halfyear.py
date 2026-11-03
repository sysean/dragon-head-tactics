#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""最近半年一进二回测（按 v1.2.x 的 A/B/C 排序规则）。

口径（sean_yang 2026-11-03 定）
------------------------------
- 信号日 d：昨日首板（limit_list_d U，limit_times==1）
- 买入：d 的下一交易日 T 的开盘价（9:25 竞价价 = 开盘价，禁止用涨停价排板）
- 胜：T+1（买入次日）盘中最高价 > 买入价 → 记 1（有溢价就算胜利）
- 收益：T+1 收盘 / 买入价 − 1（对照用）
- 排除：T 日一字/涨停开（开盘价距涨停 <0.3%）、T 日低开
- 打分（9:25 可见项）：首板成交额 / 板块共振（同行业昨日涨停家数）/ 高开幅度 / 封成比+换手 / 首封时间
- 量能：9:35 真实口径（开盘5分钟成交/首板全天 ≥8%）历史不可得，用「当日二板成交额 ÷ 首板成交额」
  作近似（量能比 ≥1.5 ≈ 开盘5分钟 ≥8%），该维度含轻微前视，单列一版对照

用法
----
python3 backtest_ranking_halfyear.py --start 20260501 --end 20261031
"""
import os
import sys
import time
import argparse
import warnings

try:
    import pandas as pd
except ModuleNotFoundError:
    sys.exit('need pandas: /usr/local/bin/python3')

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from premarket_dragon_scan import pro, limit_pct  # noqa: E402

warnings.filterwarnings('ignore')
CACHE = os.path.expanduser('~/.hermes/cache/f2s_rank')
os.makedirs(CACHE, exist_ok=True)


def cal(start, end):
    return sorted(pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')['cal_date'].tolist())


def get(fn, fname, **kw):
    p = os.path.join(CACHE, fname + '.pkl')
    if os.path.exists(p):
        try:
            return pd.read_pickle(p)
        except Exception:
            pass
    for i in range(3):
        try:
            df = fn(**kw)
            pd.to_pickle(df, p)
            return df
        except Exception as e:
            if i == 2:
                print(f'  [warn] {fname} {e}')
                return None
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20260501')
    ap.add_argument('--end', default='20261031')
    a = ap.parse_args()
    days = cal(a.start, a.end)
    print(f'区间 {days[0]}~{days[-1]}，{len(days)} 个交易日')

    trades = []
    for i, d in enumerate(days[:-2]):
        nd, nd1 = days[i + 1], days[i + 2]
        U = get(pro.limit_list_d, f'U_{d}', trade_date=d, limit_type='U')
        if U is None or not len(U):
            continue
        U['lt'] = pd.to_numeric(U['limit_times'], errors='coerce').fillna(0).astype(int)
        first = U[U['lt'] == 1]
        if not len(first):
            continue
        ind_cnt = U.groupby('industry')['ts_code'].count().to_dict()
        DT = get(pro.daily, f'D_{nd}', trade_date=nd)
        DT1 = get(pro.daily, f'D_{nd1}', trade_date=nd1)
        Dd = get(pro.daily, f'D_{d}', trade_date=d)   # 供涨停价与量能比
        if DT is None or DT1 is None or Dd is None:
            continue
        t0 = DT.set_index('ts_code')
        t1 = DT1.set_index('ts_code')
        for _, r in first.iterrows():
            code = r['ts_code']
            if code not in t0.index or code not in t1.index:
                continue
            pre = float(t0.loc[code, 'pre_close'])
            op = float(t0.loc[code, 'open'])
            if pre <= 0 or op <= 0:
                continue
            lim = round(pre * (1 + limit_pct(code)), 2)
            if abs(op - lim) < 0.011:          # 一字/涨停开：买不到，剔除
                continue
            gap = (op / pre - 1) * 100
            amt_prev = (r['amount'] or 0) / 1e8
            fd = (r['fd_amount'] / r['amount'] * 100) if r['amount'] else 999
            tover = float(r['turnover_ratio'] or 0)
            ft = int(str(r['first_time']).zfill(6)) if str(r['first_time']).isdigit() else 235959
            ind = ind_cnt.get(r['industry'], 0)
            amt_t = float(t0.loc[code, 'amount']) * 1000   # 千元 → 元
            vr = amt_t / r['amount'] if r['amount'] else float('nan')

            # ---- 9:25 静态分（不含量能）----
            s0 = 0
            if amt_prev >= 5: s0 += 2
            elif amt_prev >= 2: s0 += 1
            else: s0 -= 2
            if ind >= 5: s0 += 2
            elif ind >= 3: s0 += 1
            if 0 <= gap <= 6: s0 += 1
            elif gap > 7: s0 -= 3
            elif gap > 6: s0 -= 1
            else: s0 -= 2
            if fd < 10 and tover >= 10: s0 += 1
            if ft <= 103000: s0 += 1
            # ---- 量能分（近似 9:35 门槛）----
            sv = 2 if vr >= 1.5 else (1 if vr >= 1.0 else -2)
            buy = op
            hi1 = float(t1.loc[code, 'high'])
            cl1 = float(t1.loc[code, 'close'])
            trades.append(dict(signal=d, buy_day=nd, code=code, name=r['name'], s0=s0, sv=sv,
                               vr=vr, gap=gap, amt_prev=amt_prev, buy=buy,
                               win=1 if hi1 > buy else 0,
                               ret=cl1 / buy - 1, hi_ret=hi1 / buy - 1))
        if i % 10 == 0:
            print(f'  {d} 累计 {len(trades)} 笔', flush=True)

    df = pd.DataFrame(trades)
    if not len(df):
        print('无样本'); return
    df['tier'] = (df['s0'] + df['sv']).apply(lambda s: 'A' if s >= 8 else ('B' if s >= 6 else 'C'))
    df['tier0'] = df['s0'].apply(lambda s: 'A' if s >= 6 else ('B' if s >= 4 else 'C'))
    df.to_csv(os.path.join(CACHE, 'trades.csv'), index=False)

    def show(sub, label):
        if not len(sub):
            print(f'{label:28s} n=0'); return
        print(f'{label:28s} n={len(sub):5d}｜盘中胜率 {(sub.win.mean()*100):5.1f}%'
              f'｜收盘均值 {sub.ret.mean()*100:+6.2f}%｜收盘中位 {sub.ret.median()*100:+6.2f}%'
              f'｜T+1最高均值 {sub.hi_ret.mean()*100:+6.2f}%')

    print(f'\n总样本 {len(df)} 笔｜信号日 {df.signal.nunique()} 个｜{df.buy_day.min()}~{df.buy_day.max()}')
    print('\n===== 全样本 =====')
    show(df, '全部一进二（可买）')
    print('\n===== 只按 9:25 静态分（无量能）=====')
    for t in ('A', 'B', 'C'):
        show(df[df.tier0 == t], f'静态 {t} 档')
    print('\n===== 静态分 + 量能分（≈9:35 规则）=====')
    for t in ('A', 'B', 'C'):
        show(df[df.tier == t], f'综合 {t} 档')
    print('\n===== 量能单维度 =====')
    for lo, hi, lab in ((0, 1.0, '量能比<1.0 缩量'), (1.0, 1.5, '1.0-1.5'), (1.5, 2.5, '1.5-2.5 最优'), (2.5, 99, '>2.5')):
        show(df[(df.vr >= lo) & (df.vr < hi)], lab)
    print('\n===== 高开分档 =====')
    for lo, hi, lab in ((-99, 0, '低开'), (0, 2, '0-2%'), (2, 4, '2-4%'), (4, 6, '4-6%'), (6, 99, '>6%')):
        show(df[(df.gap >= lo) & (df.gap < hi)], lab)
    print('\n===== 综合 A 档 × 量能确认（推荐组合）=====')
    show(df[(df.tier == 'A') & (df.vr >= 1.5)], 'A 档 且 量能比≥1.5')
    show(df[(df.tier == 'A') & (df.vr < 1.5)], 'A 档 但 量能不足')
    print('\n===== 按月（综合 A 档）=====')
    sub = df[df.tier == 'A'].copy()
    if len(sub):
        sub['m'] = sub.buy_day.str[:6]
        g = sub.groupby('m').agg(n=('win', 'size'), 胜率=('win', lambda x: round(x.mean() * 100, 1)),
                                 收盘均值=('ret', lambda x: round(x.mean() * 100, 2)))
        print(g.to_string())
    print(f'\n明细已存 {os.path.join(CACHE, "trades.csv")}')


if __name__ == '__main__':
    main()
