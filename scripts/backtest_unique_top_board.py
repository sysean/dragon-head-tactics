#!/usr/bin/env python3
"""唯一最高标打板回测（龙头战法口径）—— 见 SKILL.md 第十一节

规则：每个交易日取涨停股里连板数最高者；只有「唯一」最高标才买；
     一字板（开=低=涨停价，全天没在涨停价下方成交过）买不进 → 跳过；
     其余情况按当日涨停价（stk_limit.up_limit）成交。
     单票满仓复利、100 股取整，含佣金万 2.5（最低 5 元）/ 卖出印花税 0.05% / 过户费 0.001%。

卖法 --mode：
  hold     涨停锁仓，断板当日收盘卖（默认，龙头战法主口径）
  holdnext 涨停锁仓，断板次日开盘卖
  nextc    买入次日收盘卖（隔日无脑）
  nexto    买入次日开盘卖
  swap     同 hold，但断板日收盘卖出后若当日另有可买的唯一最高标则立即换股（乐观上界，别混进主口径）

数据：Tushare Pro limit_list_d + daily + stk_limit，按日缓存在 --data-dir。
当日涨停池还没入库（收盘后到约 18:00 之间）时自动用 daily + stk_limit 自算连板数；
该自算口径与 limit_list_d 的 limit_times 已抽样核对一致。
注意 pro.daily / pro.stk_limit 都没有 name 列，股票名统一从 stock_basic 建字典。
"""
import argparse
import os
import time

import pandas as pd
import tushare as ts

COMM, MIN_COMM, STAMP, TRANSFER = 0.00025, 5.0, 0.0005, 0.00001
EPS = 0.005
DEFAULT_DIR = os.path.expanduser('~/.hermes/cache/dragonhead')


# ---------- 数据 ----------
def fetch(pro, days, data_dir):
    os.makedirs(data_dir, exist_ok=True)
    for d in days:
        f = os.path.join(data_dir, f'd_{d}.pkl')
        if os.path.exists(f):
            continue
        pd.to_pickle(dict(ll=pro.limit_list_d(trade_date=d),
                          daily=pro.daily(trade_date=d),
                          limit=pro.stk_limit(trade_date=d)), f)
        time.sleep(0.4)


def load(days, data_dir):
    D = {}
    for d in days:
        o = pd.read_pickle(os.path.join(data_dir, f'd_{d}.pkl'))
        D[d] = dict(ll=o['ll'] if o['ll'] is not None else pd.DataFrame(),
                    px=o['daily'].set_index('ts_code'),
                    limit=o['limit'].set_index('ts_code'))
    return D


def names_map(pro, data_dir):
    f = os.path.join(data_dir, 'names.pkl')
    if os.path.exists(f):
        return pd.read_pickle(f)
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    nm = dict(zip(sb['ts_code'], sb['name']))
    pd.to_pickle(nm, f)
    return nm


def is_u(D, d, code):
    px_, lim = D[d]['px'], D[d]['limit']
    if len(px_) == 0 or code not in px_.index or code not in lim.index:
        return None
    return abs(float(px_.loc[code, 'close']) - float(lim.loc[code, 'up_limit'])) < EPS


def boards(D, days, code, d, n=1):
    """沿完整交易日历往回数连板；当日无行情（停牌）视为中断。"""
    i = days.index(d)
    if i == 0 or n > 25:
        return n
    if is_u(D, days[i - 1], code):
        return boards(D, days, code, days[i - 1], n + 1)
    return n


def signals(D, days, names):
    out = {}
    for d in days:
        ll = D[d]['ll']
        if len(ll) == 0:
            px_, lim = D[d]['px'], D[d]['limit']
            u = [c for c in px_.index
                 if c in lim.index and not c.endswith('.BJ')
                 and 'ST' not in names.get(c, '') and '退' not in names.get(c, '')
                 and abs(float(px_.loc[c, 'close']) - float(lim.loc[c, 'up_limit'])) < EPS]
            if not u:
                out[d] = dict(max=None, top=[], src='daily')
                continue
            bd = {c: boards(D, days, c, d) for c in u}
            mx = max(bd.values())
            out[d] = dict(max=mx, src='daily', top=[
                dict(ts_code=c, name=names.get(c, ''), limit_times=mx) for c in u if bd[c] == mx])
            continue
        u = ll[(ll['limit'] == 'U') & (~ll['ts_code'].str.endswith('.BJ'))].copy()
        u = u[~u['name'].str.contains('ST|退', na=False)]
        u = u[u['limit_times'].fillna(0) > 0]
        if len(u) == 0:
            out[d] = dict(max=None, top=[], src='pool')
            continue
        mx = u['limit_times'].max()
        top = u[u['limit_times'] == mx]
        out[d] = dict(max=int(mx), src='pool',
                      top=top[['ts_code', 'name', 'limit_times']].to_dict('records'))
    return out


def px(D, d, code):
    t = D[d]['px']
    if len(t) == 0 or code not in t.index:
        return None                      # 停牌
    r = t.loc[code]
    if isinstance(r, pd.DataFrame):
        r = r.iloc[0]
    up = None
    lim = D[d]['limit']
    if len(lim) and code in lim.index:
        rr = lim.loc[code]
        up = float(rr.iloc[0]['up_limit']) if isinstance(rr, pd.DataFrame) else float(rr['up_limit'])
    if up is None:
        pct = 0.20 if code[:3] in ('300', '301', '688') else 0.10
        up = round(float(r['pre_close']) * (1 + pct), 2)
    return dict(o=float(r['open']), h=float(r['high']), l=float(r['low']),
                c=float(r['close']), pre=float(r['pre_close']), up=up)


# ---------- 撮合与记账 ----------
def buy_cost(p, sh):
    a = p * sh
    return a + max(a * COMM, MIN_COMM) + a * TRANSFER


def sell_net(p, sh):
    a = p * sh
    return a - max(a * COMM, MIN_COMM) - a * STAMP - a * TRANSFER


def lots_for(cash, price):
    n = int(cash // (price * 100))
    while n > 0 and buy_cost(price, n * 100) > cash:
        n -= 1
    return n * 100


def run(D, days, sig, mode, init=10000.0, force_yizi=False):
    cash, trades, skipped, eq = init, [], [], []
    holding, pending_sell = None, False

    def try_entry(d):
        nonlocal cash, holding
        s = sig[d]
        if s['max'] is None or len(s['top']) != 1:
            return
        st = s['top'][0]
        p = px(D, d, st['ts_code'])
        if p is None:
            return
        yizi = abs(p['o'] - p['up']) < EPS and abs(p['l'] - p['up']) < EPS
        if not force_yizi and (yizi or p['l'] >= p['up'] - EPS):
            skipped.append(dict(date=d, code=st['ts_code'], name=st['name'],
                                board=st['limit_times'], why='一字板' if yizi else '未破涨停价'))
            return
        sh = lots_for(cash, p['up'])
        if sh == 0:
            skipped.append(dict(date=d, code=st['ts_code'], name=st['name'],
                                board=st['limit_times'], why='资金不足1手'))
            return
        cost = buy_cost(p['up'], sh)
        eq0 = cash
        cash -= cost
        holding = dict(code=st['ts_code'], name=st['name'], board=st['limit_times'], bdate=d,
                       bpx=p['up'], sh=sh, cost=cost, last=p['c'], eq0=eq0, ndays=1,
                       util=cost / eq0 * 100)

    for d in days:
        if holding:
            p = px(D, d, holding['code'])
            if p is None:                                   # 停牌：按上一收盘盯市继续持有
                eq.append((d, cash + holding['sh'] * holding['last']))
                continue
            holding['ndays'] += 1
            if pending_sell:                                # holdnext：断板次日开盘卖
                cash += sell_net(p['o'], holding['sh'])
                trades.append(dict(holding, sdate=d, spx=p['o'],
                                   ret=(cash / holding['eq0'] - 1) * 100, eq=cash))
                holding, pending_sell = None, False
                try_entry(d)                                # 竞价卖出后可当日再买
                eq.append((d, cash if holding is None else cash + holding['sh'] * holding['last']))
                continue
            if mode in ('hold', 'swap'):
                if p['c'] >= p['up'] - EPS:                 # 还在涨停，锁仓
                    holding['last'] = p['c']
                    eq.append((d, cash + holding['sh'] * p['c']))
                    continue
                cash += sell_net(p['c'], holding['sh'])     # 断板当日收盘卖
                trades.append(dict(holding, sdate=d, spx=p['c'],
                                   ret=(cash / holding['eq0'] - 1) * 100, eq=cash))
                holding = None
                if mode == 'swap':
                    try_entry(d)
                eq.append((d, cash if holding is None else cash + holding['sh'] * holding['last']))
                continue
            if mode == 'holdnext':
                holding['last'] = p['c']
                if p['c'] >= p['up'] - EPS:
                    eq.append((d, cash + holding['sh'] * p['c']))
                    continue
                pending_sell = True
                eq.append((d, cash + holding['sh'] * p['c']))
                continue
            sp = p['c'] if mode == 'nextc' else p['o']          # 隔日卖出
            cash += sell_net(sp, holding['sh'])
            trades.append(dict(holding, sdate=d, spx=sp,
                               ret=(cash / holding['eq0'] - 1) * 100, eq=cash))
            holding = None
            eq.append((d, cash))
            continue
        try_entry(d)
        eq.append((d, cash if holding is None else cash + holding['sh'] * holding['last']))

    return dict(mode=mode, final=eq[-1][1], trades=trades, skipped=skipped, eq=eq, holding=holding)


def stats(r, init):
    tr = r['trades']
    win = [t for t in tr if t['ret'] > 0]
    vals = [v for _, v in r['eq']]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    s = pd.Series([t['ret'] for t in tr]) if tr else pd.Series([0.0])
    return dict(final=r['final'], total=(r['final'] / init - 1) * 100, n=len(tr),
                win=len(win), wr=len(win) / max(len(tr), 1) * 100, avg=s.mean(), med=s.median(),
                best=s.max(), worst=s.min(), mdd=mdd * 100,
                hold=sum(t['ndays'] for t in tr) / max(len(tr), 1),
                util=pd.Series([t['util'] for t in tr]).mean() if tr else float('nan'))


def chain_check(r):
    """复利链自检：每笔 (1+ret) 连乘应等于终值（无持仓时）。"""
    if r['holding'] is not None:
        return None
    v = 1.0
    for t in r['trades']:
        v *= 1 + t['ret'] / 100
    return abs(v * r['trades'][0]['eq0'] - r['final']) < 1.0 if r['trades'] else True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20260701')
    ap.add_argument('--end', default=time.strftime('%Y%m%d'))
    ap.add_argument('--init', type=float, default=10000.0)
    ap.add_argument('--data-dir', default=DEFAULT_DIR)
    ap.add_argument('--modes', default='hold,holdnext,nextc,nexto,swap')
    ap.add_argument('--mode', default=None, help='只跑一种卖法')
    ap.add_argument('--force-yizi', action='store_true', help='假设一字板也能买到（反事实）')
    ap.add_argument('--trades', action='store_true', help='打印逐笔明细')
    a = ap.parse_args()

    pro = ts.pro_api(os.environ['TUSHARE_TOKEN'])
    cal = pro.trade_cal(exchange='SSE', start_date=a.start, end_date=a.end)
    days = sorted(cal[cal['is_open'] == 1]['cal_date'].tolist())
    fetch(pro, days, a.data_dir)
    D = load(days, a.data_dir)
    nm = names_map(pro, a.data_dir)
    sig = signals(D, days, nm)
    modes = [a.mode] if a.mode else a.modes.split(',')

    print(f"区间 {days[0]}~{days[-1]}  {len(days)} 个交易日  本金 {a.init:.0f}"
          f"{'  [反事实：一字板可买]' if a.force_yizi else ''}")
    uniq = [d for d in days if sig[d]['max'] is not None and len(sig[d]['top']) == 1]
    tie = [d for d in days if sig[d]['max'] is not None and len(sig[d]['top']) > 1]
    print(f"唯一最高标 {len(uniq)} 天 / 并列 {len(tie)} 天")
    print(f"{'卖法':9}{'终值':>11}{'收益%':>9}{'笔数':>5}{'胜率%':>7}"
          f"{'均值%':>8}{'中位%':>8}{'最好%':>8}{'最差%':>8}{'回撤%':>8}{'均持仓':>6}")
    for m in modes:
        r = run(D, days, sig, m, a.init, a.force_yizi)
        s = stats(r, a.init)
        print(f"{m:9}{s['final']:>11.2f}{s['total']:>9.2f}{s['n']:>5}{s['wr']:>7.1f}"
              f"{s['avg']:>8.2f}{s['med']:>8.2f}{s['best']:>8.2f}{s['worst']:>8.2f}"
              f"{s['mdd']:>8.2f}{s['hold']:>6.1f}")
        ok = chain_check(r)
        if ok is False:
            print('  !! 复利链自检失败：单笔收益与终值对不上，先查记账（cash += net 是否写成 cash = net）')
        if a.trades:
            for t in r['trades']:
                print(f"  {t['bdate']} {t['name']}{t['board']:.0f}板 @{t['bpx']:.2f}"
                      f" -> {t['sdate']} @{t['spx']:.2f}  {t['ret']:+7.2f}%  持{t['ndays']}天"
                      f"  仓位{t['util']:.0f}%  权益{t['eq']:.0f}")
            for sk in r['skipped']:
                print(f"  跳过 {sk['date']} {sk['name']}{sk['board']:.0f}板 {sk['why']}")
            if r['holding']:
                h = r['holding']
                print(f"  期末持仓 {h['bdate']} {h['name']}{h['board']:.0f}板 @{h['bpx']:.2f} "
                      f"{h['sh']}股")


if __name__ == '__main__':
    main()
