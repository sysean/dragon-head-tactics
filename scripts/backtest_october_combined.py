#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""10 月清单（龙头战法 × 一进二 双打法，按周期切换）。

规则
----
每个信号日 d（= 买入日的前一交易日）判周期，再决定打法：
  1) 高位接力（龙头战法）：昨日涨停 ≥60 家 且 昨日涨停池今日竞价红盘率 ≥40%（最高板≥6 已于 v1.5.1 取消）
     选：昨日连板 ≥3 板里板数最高的一只（同板数取成交额最大）；一字/涨停开则退到次选
  2) 一进二：涨停 <60 家 或 晋级率 <15%（闸门内）
     选：昨日首板按「静态分+量能分」排序第 1，且 同行业涨停 ≥3 家、量能比 ≥1.5
  3) 其余情况：空仓

买卖口径
--------
买入 = 买入日开盘价（9:25 竞价价）；胜 = 再次日盘中最高 > 买入价；收益 = 再次日收盘/买入价-1

用法
----
python3 backtest_october_combined.py --month 202610
"""
import os
import sys
import argparse
import warnings

import pandas as pd

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from premarket_dragon_scan import pro, limit_pct  # noqa: E402

warnings.filterwarnings('ignore')
C = os.path.expanduser('~/.hermes/cache/f2s_rank')
COST = 0.0012


def getD(d):
    p = os.path.join(C, f'D_{d}.pkl')
    if os.path.exists(p):
        try:
            return pd.read_pickle(p)
        except Exception:
            pass
    df = pro.daily(trade_date=d)
    pd.to_pickle(df, p)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--month', default='202610')
    a = ap.parse_args()

    tr = pd.read_csv(os.path.join(C, 'trades.csv'), dtype={'signal': str, 'code': str})
    tr['tot'] = tr['s0'] + tr['sv']
    days = sorted(f[2:-4] for f in os.listdir(C) if f.startswith('U_') and f.endswith('.pkl'))

    # 补 11/02 日线（10/29、10/30 信号需要 T+1）
    cal = sorted(pro.trade_cal(exchange='SSE', start_date='20261001', end_date='20261105', is_open='1')['cal_date'])
    meta, prev = {}, None
    for d in cal:
        p = os.path.join(C, f'U_{d}.pkl')
        if not os.path.exists(p):
            u = pro.limit_list_d(trade_date=d, limit_type='U')
            if u is None or not len(u):
                meta[d] = None
                continue
            pd.to_pickle(u, p)
        u = pd.read_pickle(p)
        u['lt'] = pd.to_numeric(u['limit_times'], errors='coerce').fillna(0).astype(int)
        ind = u.groupby('industry')['ts_code'].count().to_dict()
        s = set(u['ts_code'])
        promo = (len(s & prev) / len(prev) * 100) if prev else float('nan')
        meta[d] = dict(lu=len(u), maxb=int(u['lt'].max()), promo=promo, ind=ind, U=u)
        prev = s

    idx = {d: i for i, d in enumerate(cal)}
    rows = []
    for d in cal:
        if not d.startswith(a.month) or meta.get(d) is None:
            continue
        i = idx[d]
        if i + 2 >= len(cal):
            continue
        nd, nd1 = cal[i + 1], cal[i + 2]
        m = meta[d]
        DT, DT1 = getD(nd), getD(nd1)
        if DT is None or DT1 is None or not len(DT):
            continue
        t0, t1 = DT.set_index('ts_code'), DT1.set_index('ts_code')
        u = m['U']
        # 昨日涨停池今日竞价红盘率
        g0 = t0.reindex(u['ts_code'])
        gap_red = float((g0['open'] > g0['pre_close']).mean() * 100)

        play, pick, note = '', None, ''
        relay_ok = (m['lu'] >= 60) and (m['maxb'] >= 6) and (gap_red >= 40)
        if relay_ok:
            play = '高位接力'
            cand = u[u['lt'] >= 3].copy()
            cand['amt'] = cand['amount'].fillna(0)
            cand = cand.sort_values(['lt', 'amt'], ascending=False)
            for _, r in cand.iterrows():
                code = r['ts_code']
                if code not in t0.index:
                    continue
                pre = float(t0.loc[code, 'pre_close']); op = float(t0.loc[code, 'open'])
                if pre <= 0 or op <= 0:
                    continue
                if abs(op - round(pre * (1 + limit_pct(code)), 2)) < 0.011:
                    continue      # 一字买不到，退次选
                pick = (r, code, op, '连板%d' % int(r['lt']))
                break
            if pick is None:
                note = '高位无换手标的→退一进二'
                play = '一进二'
        if play in ('', '一进二'):
            gate = (m['lu'] < 60) or (m['promo'] < 15)
            if not gate:
                play, note = '空仓', '闸门：涨停≥60 且 晋级率≥15%'
            elif play != '一进二':
                play = '一进二'
                g = tr[tr['signal'] == d]
                g = g[(g['ind_n'] if 'ind_n' in g else 0) >= 3] if False else g
                # 用元数据补板块家数
                def indn(row):
                    code = row['code']
                    rowu = u[u['ts_code'] == code]
                    if not len(rowu):
                        return 0
                    return m['ind'].get(rowu.iloc[0]['industry'], 0)
                if len(g):
                    g = g.assign(ind_n=[indn(r) for _, r in g.iterrows()])
                    g = g[(g['ind_n'] >= 3) & (g['vr'] >= 1.5)].sort_values(['tot', 'vr', 'gap'], ascending=False)
                if not len(g):
                    note = '一进二无合格（板块≥3 + 量能≥1.5）'
                else:
                    r = g.iloc[0]
                    rows.append(dict(信号日=d, 买入日=nd, 涨停=m['lu'], 最高板=m['maxb'],
                                     晋级率=round(m['promo'], 1), 红盘率=round(gap_red, 1),
                                     打法='一进二', 标的=f"{r['name']}", 代码=r['code'],
                                     分数=int(r['tot']), 买价=round(r['buy'], 2),
                                     次日最高=round(r['hi_ret'] * 100, 1), 收盘=round(r['ret'] * 100, 2),
                                     胜负='胜' if r['win'] else '负', 备注=note))
                    continue
                if play == '一进二':
                    rows.append(dict(信号日=d, 买入日=nd, 涨停=m['lu'], 最高板=m['maxb'],
                                     晋级率=round(m['promo'], 1), 红盘率=round(gap_red, 1),
                                     打法='空仓', 标的='—', 代码='', 分数='', 买价='',
                                     次日最高='', 收盘='', 胜负='', 备注=note))
                    continue
        if play == '高位接力' and pick:
            r, code, op, tag = pick
            if code not in t1.index:
                rows.append(dict(信号日=d, 买入日=nd, 涨停=m['lu'], 最高板=m['maxb'],
                                 晋级率=round(m['promo'], 1), 红盘率=round(gap_red, 1),
                                 打法='高位接力', 标的=r['name'], 代码=code, 分数='', 买价=round(op, 2),
                                 次日最高='', 收盘='', 胜负='', 备注='次日无数据'))
                continue
            hi = float(t1.loc[code, 'high']); cl = float(t1.loc[code, 'close'])
            rows.append(dict(信号日=d, 买入日=nd, 涨停=m['lu'], 最高板=m['maxb'],
                             晋级率=round(m['promo'], 1), 红盘率=round(gap_red, 1),
                             打法='高位接力', 标的=r['name'], 代码=code, 分数='', 买价=round(op, 2),
                             次日最高=round((hi / op - 1) * 100, 1), 收盘=round((cl / op - 1) * 100, 2),
                             胜负='胜' if hi > op else '负', 备注=tag))
        elif play == '空仓':
            rows.append(dict(信号日=d, 买入日=nd, 涨停=m['lu'], 最高板=m['maxb'],
                             晋级率=round(m['promo'], 1), 红盘率=round(gap_red, 1),
                             打法='空仓', 标的='—', 代码='', 分数='', 买价='',
                             次日最高='', 收盘='', 胜负='', 备注=note))

    df = pd.DataFrame(rows)
    pd.set_option('display.width', 250)
    print(f'===== {a.month} 清单（龙头战法 × 一进二）=====')
    print(df.to_string(index=False))
    real = df[df['胜负'].isin(['胜', '负'])]
    print(f'\n出手 {len(real)} 天｜胜 {int((real["胜负"]=="胜").sum())} 负 {int((real["胜负"]=="负").sum())}'
          f'｜胜率 {(real["胜负"]=="胜").mean()*100:.1f}%' if len(real) else '\n无出手')
    if len(real):
        r = real['收盘'].astype(float) / 100
        print(f'收盘均值(扣成本) {(r.mean()-COST)*100:+.2f}%｜累计 {(r.sum()-COST*len(r))*100:+.1f}%'
              f'｜最差 {r.min()*100:+.2f}%')
        print(real.groupby('打法').agg(n=('胜负', 'size'),
                                       胜率=('胜负', lambda x: round((x == '胜').mean() * 100, 1)),
                                       收盘均值=('收盘', lambda x: round(float(x.astype(float).mean()), 2))).to_string())
    df.to_csv(os.path.join(C, f'october_{a.month}.csv'), index=False)


if __name__ == '__main__':
    main()
