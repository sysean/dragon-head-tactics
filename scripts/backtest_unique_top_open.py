#!/usr/bin/env python3
"""唯一最高标「开盘价买入 + 次日溢价」回测 —— 见 SKILL.md 第十一节变体

两种口径（都用当日开盘价买入，区别在「哪天算信号日」）：

  A（可执行，无未来函数）
    信号 = D-1 收盘时该股是「独立最高标」（limit_times 严格最大且只有一只）。
    D 日开盘价买入 —— 开盘时你能看到的就是 D-1 的板数。
    这是「隔日竞价接力唯一最高标」的真实现场。

  B（事后口径，含未来函数，只作对照）
    信号 = D 收盘确认该股是独立最高标（当日封住并在板数上独享第一）。
    仍按 D 开盘价买入 —— 等价于「提前知道它会封住」，收益必然偏乐观。

溢价口径（相对 D 日开盘买入价）：
  d_close   当日收盘溢价（open → close）
  n_open    次日开盘溢价（次日 open / 买入价 - 1）← 主口径
  n_close   次日收盘溢价
  n_high    次日最高（可兑现上界）
  n_low     次日最低（浮亏最深）

数据：Tushare Pro limit_list_d + daily + stk_limit，按日缓存 ~/.hermes/cache/dragonhead。
"""
import argparse
import os
import time

import pandas as pd
import tushare as ts

EPS = 0.005
DEFAULT_DIR = os.path.expanduser('~/.hermes/cache/dragonhead')
NAMES = {}


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
                    px=o['daily'].set_index('ts_code') if o['daily'] is not None else pd.DataFrame(),
                    limit=o['limit'].set_index('ts_code') if o['limit'] is not None else pd.DataFrame())
    return D


def names_map(pro, data_dir):
    f = os.path.join(data_dir, 'names.pkl')
    if os.path.exists(f):
        return pd.read_pickle(f)
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    nm = dict(zip(sb['ts_code'], sb['name']))
    pd.to_pickle(nm, f)
    return nm


def _u_set(D, d, names):
    """当日收盘封住涨停的代码集合（用 stk_limit 权威涨停价，缺失时按板块阈值）。"""
    px_, lim = D[d]['px'], D[d]['limit']
    if len(px_) == 0:
        return set()
    out = set()
    for c in px_.index:
        if c.endswith('.BJ') or 'ST' in names.get(c, '') or '退' in names.get(c, ''):
            continue
        r = px_.loc[c]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        if len(lim) and c in lim.index:
            rr = lim.loc[c]
            up = float(rr.iloc[0]['up_limit']) if isinstance(rr, pd.DataFrame) else float(rr['up_limit'])
        else:
            pct = 0.20 if c[:3] in ('300', '301', '688') else 0.10
            up = round(float(r['pre_close']) * (1 + pct), 2)
        if abs(float(r['close']) - up) < EPS:
            out.add(c)
    return out


def _boards_back(D, all_days, d, code):
    """沿完整交易日历往回数连板（池子缺失时的自算口径）。"""
    i = all_days.index(d)
    n = 1
    while i - n >= 0 and n <= 25 and code in _u_set(D, all_days[i - n], NAMES):
        n += 1
    return n


def toppool(D, d, all_days=None):
    """当日 U 池（剔除北交所/ST/退），返回 [(ts_code, name, limit_times)]，按板数降序。
    池子未入库时用 daily + stk_limit 自算代理（会注明）。"""
    ll = D[d]['ll']
    if len(ll) == 0:
        if all_days is None or d not in all_days:
            return []
        u = _u_set(D, d, NAMES)
        if not u:
            return []
        rows = [(c, NAMES.get(c, ''), _boards_back(D, all_days, d, c)) for c in u]
        return sorted(rows, key=lambda x: -x[2])
    u = ll[(ll['limit'] == 'U') & (~ll['ts_code'].str.endswith('.BJ'))].copy()
    u = u[~u['name'].str.contains('ST|退', na=False)]
    u = u[u['limit_times'].fillna(0) > 0]
    if len(u) == 0:
        return []
    return list(u.sort_values('limit_times', ascending=False)
                [['ts_code', 'name', 'limit_times']].itertuples(index=False, name=None))


def top_of_day(D, d, all_days=None):
    """返回 (max_board, [并列最高标记录], 唯一最高标记录 or None, 唯一性)"""
    p = toppool(D, d, all_days)
    if not p:
        return None, [], None, 'no_pool'
    mx = p[0][2]
    tops = [r for r in p if r[2] == mx]
    return int(mx), tops, (tops[0] if len(tops) == 1 else None), ('unique' if len(tops) == 1 else 'tie')


def bars(D, d, code):
    px_ = D[d]['px']
    if len(px_) == 0 or code not in px_.index:
        return None
    r = px_.loc[code]
    if isinstance(r, pd.DataFrame):
        r = r.iloc[0]
    return dict(o=float(r['open']), h=float(r['high']), l=float(r['low']),
                c=float(r['close']), pre=float(r['pre_close']), pct=float(r['pct_chg']))


def limit_up(D, d, code, b):
    lim = D[d]['limit']
    if len(lim) and code in lim.index:
        rr = lim.loc[code]
        return float(rr.iloc[0]['up_limit']) if isinstance(rr, pd.DataFrame) else float(rr['up_limit'])
    pct = 0.20 if code[:3] in ('300', '301', '688') else 0.10
    return round(b['pre'] * (1 + pct), 2)


def build(D, days, mode, sig_no_yizi=False):
    """mode='A' → 用 D-1 收盘的唯一最高标当 D 日信号；mode='B' → 用 D 收盘确认的。"""
    out = []
    _pending_pk = None
    for d in days:
        i = ALL_DAYS.index(d)
        if mode == 'A':
            if i == 0:
                continue
            mx, tops, uniq, _ = top_of_day(D, ALL_DAYS[i - 1], ALL_DAYS)
            if uniq is None:
                continue
            code, name, lt = uniq
            sig_date = ALL_DAYS[i - 1]
        else:
            mx, tops, uniq, _ = top_of_day(D, d, ALL_DAYS)
            if uniq is None:
                continue
            code, name, lt = uniq
            sig_date = d
            if i > 0:
                pmx, ptops, _, pst = top_of_day(D, ALL_DAYS[i - 1], ALL_DAYS)
                ppool = toppool(D, ALL_DAYS[i - 1], ALL_DAYS)
                prev_b = next((b for c, n, b in ppool if c == code), None)
                _pending_pk = dict(prev_max=pmx, prev_tops=ptops, prev_stat=pst, prev_board=prev_b)
            else:
                _pending_pk = dict(prev_max=None, prev_tops=[], prev_stat='', prev_board=None)
        if sig_no_yizi:
            sb = bars(D, sig_date, code)
            if sb is not None:
                sup = limit_up(D, sig_date, code, sb)
                if abs(sb['o'] - sup) < EPS and abs(sb['l'] - sup) < EPS:
                    continue
        b = bars(D, d, code)
        if b is None:                     # 信号日停牌，成交不了
            out.append(dict(date=d, sig_date=sig_date, code=code, name=name, board=lt,
                            buy=None, note='信号日停牌'))
            continue
        up = limit_up(D, d, code, b)
        yizi = abs(b['o'] - up) < EPS and abs(b['l'] - up) < EPS
        rec = dict(date=d, sig_date=sig_date, code=code, name=name, board=lt,
                   pk=_pending_pk if mode == 'B' else None,
                   buy=b['o'], up=up, yizi=yizi, open_at_limit=bool(abs(b['o'] - up) < EPS),
                   sealed=bool(b['c'] >= up - EPS), d_pct=b['pct'], d_close=None,
                   n_open=None, n_close=None, n_high=None, n_low=None, n_pct=None, note='')
        rec['d_close'] = (b['c'] / b['o'] - 1) * 100
        if i + 1 < len(ALL_DAYS):
            nb = bars(D, ALL_DAYS[i + 1], code)
            if nb is None:
                rec['note'] = '次日停牌'
            else:
                rec['n_open'] = (nb['o'] / rec['buy'] - 1) * 100
                rec['n_close'] = (nb['c'] / rec['buy'] - 1) * 100
                rec['n_high'] = (nb['h'] / rec['buy'] - 1) * 100
                rec['n_low'] = (nb['l'] / rec['buy'] - 1) * 100
                rec['n_pct'] = nb['pct']
        else:
            rec['note'] = '次日未收盘'
        out.append(rec)
    return out


def _stats(rows, label):
    if not rows:
        print(f'  [{label}] 无可结算样本')
        return
    s = pd.DataFrame([{k: r[k] for k in ('n_open', 'n_close', 'n_high', 'n_low')} for r in rows])
    print(f"  [{label}] 笔数 {len(rows)}")
    for k, nm in (('n_open', '次日开盘'), ('n_close', '次日收盘'), ('n_high', '次日最高'), ('n_low', '次日最低')):
        print(f"      {nm}溢价 均值{s[k].mean():+6.2f}%  中位{s[k].median():+6.2f}%  "
              f"胜率{(s[k] > 0).mean() * 100:3.0f}%  最好{s[k].max():+6.2f}%  最差{s[k].min():+6.2f}%")
    v = 1.0
    for r in rows:
        v *= 1 + r['n_open'] / 100
    print(f"      次日开盘卖·满仓复利: 1万 → {10000 * v:,.0f} 元 ({(v - 1) * 100:+.1f}%)")



def pk_text(r):
    """把「当天怎么赢下最高板」讲清楚：续板 / PK 击败 / 原高度断板后顶上 / 复牌顶上。"""
    pk = r['pk']
    prev_b = pk.get('prev_board')
    pmx = pk.get('prev_max')
    ptops = pk.get('prev_tops') or []
    rival_names = '、'.join(f'{x[1]}{x[2]:.0f}板' for x in ptops if x[0] != r['code'])
    if prev_b is None:
        return f"前日未涨停(停牌/断板) → 今日{r['board']:.0f}板直接顶上"
    if prev_b >= pmx:                       # 前日它就是最高板
        if len(ptops) > 1:
            others = '、'.join(f'{x[1]}' for x in ptops if x[0] != r['code'])
            return f"PK：前日{prev_b:.0f}板并列({others}) → 今日{r['board']:.0f}板独享"
        return f"续板：前日{prev_b:.0f}板唯一最高 → 今日{r['board']:.0f}板"
    return f"前日{prev_b:.0f}板非最高 → 今日{r['board']:.0f}板顶上，原{pmx:.0f}板({rival_names})断板"


def report(D, days, recs, tag, exclude_yizi=False):
    print(f'\n===== 口径 {tag} =====')
    if not recs:
        print('无信号')
        return
    buyable = []
    for r in recs:
        if r['buy'] is None:
            print(f"{r['date']}  {r['name']}({r['code']}) {r['board']:.0f}板  不能成交[{r['note']}]")
            continue
        pktxt = pk_text(r) if r.get('pk') else ''
        if r['yizi']:
            tag2 = '  [一字板·买不到]'
        elif r['open_at_limit']:
            tag2 = '  [T字·开盘即涨停价,需排队]'
        else:
            tag2 = ''
        print(f"{r['date']}  {r['name']}({r['code']}) {r['board']:.0f}板  买@{r['buy']:>7.2f}{tag2}"
              + ('  当日封住' if r['sealed'] else '  当日断板')
              + (f"  [{pktxt}]" if pktxt else '')
              + f"  当日收盘{r['d_close']:+6.2f}%"
              + (f"  |次日 开{r['n_open']:+6.2f}% 收{r['n_close']:+6.2f}% 高{r['n_high']:+6.2f}% 低{r['n_low']:+6.2f}%"
                 if r['n_open'] is not None else f"  [{r['note'] or '次日无数据'}]"))
        if r['n_open'] is not None and not (exclude_yizi and r['yizi']):
            buyable.append(r)
    all_ok = buyable
    real = [r for r in buyable if not r['yizi']]
    print()
    if exclude_yizi:
        _stats(all_ok, '剔除一字板后（可成交样本）')
    else:
        _stats(all_ok, '全部信号（含一字板）')
        _stats(real, '剔除一字板（真实可成交）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20261001')
    ap.add_argument('--end', default='20261031')
    ap.add_argument('--data-dir', default=DEFAULT_DIR)
    ap.add_argument('--modes', default='A,B')
    ap.add_argument('--exclude-yizi', action='store_true', help='一字板不计入统计（当天炸过板的 T 字板保留）')
    ap.add_argument('--sig-no-yizi', action='store_true', help='信号日本身是一字板的直接不出信号（更严口径）')
    a = ap.parse_args()

    global NAMES, ALL_DAYS
    pro = ts.pro_api(os.environ['TUSHARE_TOKEN'])
    # 多取前 15 个交易日，保证起点附近也能拿到 D-1 池子
    cal_wide = pro.trade_cal(exchange='SSE', start_date='20260901', end_date=a.end)
    all_days = sorted(cal_wide[cal_wide['is_open'] == 1]['cal_date'].tolist())
    days = [d for d in all_days if a.start <= d <= a.end]
    fetch(pro, all_days, a.data_dir)
    D = load(all_days, a.data_dir)
    NAMES = names_map(pro, a.data_dir)
    ALL_DAYS = all_days

    print(f'区间 {days[0]}~{days[-1]}  {len(days)} 个交易日')
    print('\n每日涨停池最高板：')
    for d in days:
        mx, tops, uniq, st = top_of_day(D, d, all_days)
        names = '、'.join(f'{r[1]}{r[2]}板' for r in tops)
        print(f'  {d}  最高{mx}板  {st:8}  {names}')

    for m in a.modes.split(','):
        recs = build(D, days, m, a.sig_no_yizi)
        report(D, days, recs, m, a.exclude_yizi)


if __name__ == '__main__':
    main()
