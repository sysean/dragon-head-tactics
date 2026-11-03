#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""只买「9:25 排第一的那一只」回测 + 周期节点开仓闸门 + 板块效应门槛。

数据来源：~/.hermes/cache/f2s_rank/ 里 backtest_ranking_halfyear.py 的逐笔明细与逐日缓存
（trades.csv + U_*.pkl），不重复拉数据。

口径
----
- 每个信号日 d：昨日首板 ∩ 今日非一字/非涨停开，按「静态分 + 量能分」排序，只取第 1 名
- 买入：d 的下一交易日 T 开盘价；胜：T+1 盘中最高 > 买入价；收益：T+1 收盘/买入价-1
- 周期闸门（9:25 可算，用 d 日数据）：涨停家数、晋级率（|U(d-1)∩U(d)|/|U(d-1)|）
- 板块效应：同行业昨日涨停家数（板块共振）

用法
----
python3 backtest_top1_cycle.py --month 202610
"""
import os
import sys
import argparse
import warnings

import pandas as pd

warnings.filterwarnings('ignore')
C = os.path.expanduser('~/.hermes/cache/f2s_rank')


def load():
    tr = pd.read_csv(os.path.join(C, 'trades.csv'), dtype={'signal': str, 'code': str})
    days = sorted(f[2:-4] for f in os.listdir(C) if f.startswith('U_') and f.endswith('.pkl'))
    meta, ind_of = {}, {}
    prev_set = None
    for d in days:
        u = pd.read_pickle(os.path.join(C, f'U_{d}.pkl'))
        u['lt'] = pd.to_numeric(u['limit_times'], errors='coerce').fillna(0).astype(int)
        ind = u.groupby('industry')['ts_code'].count().to_dict()
        for _, r in u.iterrows():
            ind_of[(d, r['ts_code'])] = ind.get(r['industry'], 0)
        s = set(u['ts_code'])
        promo = (len(s & prev_set) / len(prev_set) * 100) if prev_set else float('nan')
        meta[d] = dict(lu=len(u), maxb=int(u['lt'].max()), promo=promo, ind=ind)
        prev_set = s
    tr['ind_n'] = [ind_of.get((r.signal, r.code), 0) for r in tr.itertuples()]
    tr['lu'] = tr['signal'].map(lambda d: meta.get(d, {}).get('lu', 0))
    tr['promo'] = tr['signal'].map(lambda d: meta.get(d, {}).get('promo', float('nan')))
    tr['tot'] = tr['s0'] + tr['sv']
    return tr, meta, days


def run(tr, gate=None, need_ind=0, need_vr=0.0):
    picks = []
    for d, g in tr.groupby('signal'):
        d = str(d)
        if gate is not None and not gate(g.iloc[0]):
            continue
        s = g[g['ind_n'] >= need_ind] if need_ind else g
        if need_vr:
            s = s[s['vr'] >= need_vr]
        if not len(s):
            continue
        s = s.sort_values(['tot', 'vr', 'gap'], ascending=False)
        picks.append(s.iloc[0])
    return pd.DataFrame(picks)


def stat(df, label):
    if not len(df):
        print(f'{label:42s} n=0'); return
    cost = 0.0012
    print(f'{label:42s} n={len(df):3d} 交易日{df.signal.nunique():3d}｜盘中胜率 {df.win.mean()*100:5.1f}%'
          f'｜收盘均值 {(df.ret.mean()-cost)*100:+6.2f}%｜累计 {(df.ret-cost).sum()*100:+6.1f}%'
          f'｜最差 {df.ret.min()*100:+6.2f}%｜涨停命中 {(df.ret>=0.095).mean()*100:4.1f}%')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--month', default=None, help='例 202610')
    a = ap.parse_args()
    tr, meta, days = load()
    print(f'样本池 {len(tr)} 笔 / {tr.signal.nunique()} 个信号日\n')

    g40 = lambda r: r['lu'] < 40
    g60 = lambda r: r['lu'] < 60
    g60p = lambda r: r['lu'] < 60 or r['promo'] < 15
    g80 = lambda r: r['lu'] < 80

    print('===== 变体对比（都只买排名第一那 1 只）=====')
    stat(run(tr), 'V0 每日第 1 名，无闸门')
    stat(run(tr, need_ind=3), 'V1 + 板块≥3 家')
    stat(run(tr, need_ind=3, need_vr=1.5), 'V2 + 板块≥3 & 量能比≥1.5')
    stat(run(tr, gate=g60, need_ind=3, need_vr=1.5), 'V3 + 周期闸门(涨停<60)')
    stat(run(tr, gate=g60p, need_ind=3, need_vr=1.5), 'V4 闸门(涨停<60 或 晋级<15%)')
    stat(run(tr, gate=g80, need_ind=3, need_vr=1.5), 'V5 闸门(涨停<80)')
    stat(run(tr, gate=g40, need_ind=3, need_vr=1.5), 'V6 闸门(涨停<40，最严)')

    print('\n===== 选用 V4（板块≥3 + 量能≥1.5 + 周期闸门）按月 =====')
    best = run(tr, gate=g60p, need_ind=3, need_vr=1.5)
    if len(best):
        b = best.copy(); b['m'] = b['signal'].str[:6]
        print(b.groupby('m').agg(n=('win', 'size'), 胜率=('win', lambda x: round(x.mean() * 100, 1)),
                                 均值=('ret', lambda x: round((x.mean() - 0.0012) * 100, 2)),
                                 累计=('ret', lambda x: round((x.sum() - 0.0012 * len(x)) * 100, 1))).to_string())

    if a.month:
        print(f'\n===== {a.month} 清单（V4 口径：每日只买第 1 名，闸门不过就空仓）=====')
        rows = []
        for d, g in tr.groupby('signal'):
            d = str(d)
            if not d.startswith(a.month):
                continue
            mt = meta.get(d, {})
            gate_ok = (mt.get('lu', 999) < 60) or (mt.get('promo', 99) < 15)
            s = g[(g['ind_n'] >= 3) & (g['vr'] >= 1.5)]
            s = s.sort_values(['tot', 'vr', 'gap'], ascending=False)
            if not gate_ok:
                rows.append(dict(信号日=d, 涨停=mt.get('lu'), 最高板=mt.get('maxb'),
                                 晋级率=round(mt.get('promo', 0), 1), 股票='【空仓】',
                                 板块='', 分='', 买价='', 次日最高='', 收盘='', 胜负=''))
            elif not len(s):
                rows.append(dict(信号日=d, 涨停=mt.get('lu'), 最高板=mt.get('maxb'),
                                 晋级率=round(mt.get('promo', 0), 1), 股票='【无合格】',
                                 板块='', 分='', 买价='', 次日最高='', 收盘='', 胜负=''))
            else:
                r = s.iloc[0]
                rows.append(dict(信号日=d, 涨停=mt.get('lu'), 最高板=mt.get('maxb'),
                                 晋级率=round(mt.get('promo', 0), 1),
                                 股票=f"{r['name']}({r.code})", 板块=f'同行业{r.ind_n}家',
                                 分=int(r.tot), 买价=round(r.buy, 2),
                                 次日最高=f"{(r.hi_ret)*100:+.1f}%", 收盘=f"{r.ret*100:+.2f}%",
                                 胜负='胜' if r.win else '负'))
        out = pd.DataFrame(rows)
        pd.set_option('display.width', 200)
        print(out.to_string(index=False))
        nz = out[~out['胜负'].isin(['', '胜', '负']) | (out['胜负'] == '')]
        real = out[out['胜负'].isin(['胜', '负'])]
        if len(real):
            print(f"\n{ a.month } 实际出手 {len(real)} 次：胜 {int((real['胜负']=='胜').sum())} / "
                  f"负 {int((real['胜负']=='负').sum())}，胜率 {(real['胜负']=='胜').mean()*100:.1f}%；"
                  f"空仓 {int((out['股票']=='【空仓】').sum())} 天、无合格 {int((out['股票']=='【无合格】').sum())} 天")


if __name__ == '__main__':
    main()
