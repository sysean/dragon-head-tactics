#!/usr/bin/env python3
"""9:25 选票标准回测（SKILL.md §4.5「9:25 的判断顺序」的可算版本）

规则（按顺序执行，任一步否决则当天不开仓）：

  S1 档位      用 T 日集合竞价（= 开盘价）算全市场温度：红盘率 ≥40% 且 开盘涨跌中位 >0
  S2 读最高标  取 T-1 唯一最高板 H（并列则视为无龙头）。判「高开无量」：
               高开 >7%  且  同行业(T-1 涨停池，剔 H) 的 T 日开盘涨幅中位 <2%  且  家数 ≤3
               → 接力环境不成立，当天降档不接力
  S3 选票      T-1 连板 ≥2 的票里，取 T 日高开落在 3%–7% 的（一字板天然不在窗口内）
               若 H 自己在窗口内 → 优先 H（情绪核心）
               否则按「同行业 T 日开盘涨幅中位」降序，再按 T-1 成交额降序，取第 1 只
  S4 卖法      买入价 = T 日开盘价（集合竞价成交价）；主口径 T+1 收盘卖
               胜 = 卖价 > 买价（与本地胜率表同口径，未计手续费滑点）

不可回溯项（必须在报告里写明）：买一挂单额、竞价额占昨全天成交比 ——
Tushare 的 stk_auction_o/_c 需 5000 积分，历史竞价明细拿不到。
所以 S2 是「四项判据里的三项可算版本」（价格 + 跟风方向 + 参战家数）。
历史连板数一律用 daily + stk_limit 自算（close ≈ up_limit 往回数），不依赖 limit_list_d。

数据：~/.hermes/cache/limitpool/{daily,stk_limit}_YYYYMMDD.pkl + stock_basic.pkl
"""
import argparse
import os
from statistics import median

import pandas as pd

CACHE = os.path.expanduser('~/.hermes/cache/limitpool')
EPS = 0.005
BOARD_EXCLUDE_PREFIX = ('8', '4', '9')  # 北交所 / B 股


def cached(kind, d):
    f = os.path.join(CACHE, f'{kind}_{d}.pkl')
    return pd.read_pickle(f) if os.path.exists(f) else None


def fetch_missing(start, end):
    """缓存缺失时用 Tushare 补齐（需要 TUSHARE_TOKEN）。start/end 为 YYYYMMDD。"""
    import time

    import tushare as ts
    pro = ts.pro_api(os.environ.get('TUSHARE_TOKEN'))
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
    todo = [d for d in cal.cal_date.tolist()
            if cached('daily', d) is None or cached('stk_limit', d) is None]
    if todo:
        print(f'补数据 {len(todo)} 个交易日 → {CACHE}')
    for d in todo:
        if cached('daily', d) is None:
            pd.to_pickle(pro.daily(trade_date=d), os.path.join(CACHE, f'daily_{d}.pkl'))
            time.sleep(0.35)
        if cached('stk_limit', d) is None:
            pd.to_pickle(pro.stk_limit(trade_date=d), os.path.join(CACHE, f'stk_limit_{d}.pkl'))
            time.sleep(0.35)
    if not os.path.exists(os.path.join(CACHE, 'stock_basic.pkl')):
        sb = pro.stock_basic(exchange='', list_status='L',
                             fields='ts_code,name,industry,list_date,market')
        pd.to_pickle(sb, os.path.join(CACHE, 'stock_basic.pkl'))


def trading_days(start, end):
    out = []
    for f in sorted(os.listdir(CACHE)):
        if not f.startswith('daily_') or not f.endswith('.pkl') or f == 'daily_panel.pkl':
            continue
        d = f[6:-4]
        if start <= d <= end:
            out.append(d)
    return out


class Book:
    """按日缓存的行情 + 涨跌停价 + 基础信息。"""

    def __init__(self):
        self.px, self.lim, self.info = {}, {}, None

    def day(self, d):
        if d not in self.px:
            px, lm = cached('daily', d), cached('stk_limit', d)
            if px is None or lm is None:
                raise SystemExit(f'缺少 {d} 的行情缓存')
            self.px[d] = px.drop_duplicates('ts_code').set_index('ts_code')
            self.lim[d] = lm.drop_duplicates('ts_code').set_index('ts_code')
        return self.px[d], self.lim[d]

    def basic(self):
        if self.info is None:
            sb = pd.read_pickle(os.path.join(CACHE, 'stock_basic.pkl'))
            self.info = sb.drop_duplicates('ts_code').set_index('ts_code')
        return self.info

    def name(self, code):
        try:
            return self.basic().loc[code, 'name']
        except KeyError:
            return code

    def industry(self, code):
        try:
            v = self.basic().loc[code, 'industry']
            return v if isinstance(v, str) and v else '未分类'
        except KeyError:
            return '未分类'

    def is_st(self, code):
        n = self.name(code) or ''
        return 'ST' in n or '退' in n

    def tradable(self, d, code):
        px = self.px[d]
        return code in px.index and float(px.loc[code, 'open']) > 0

    def limit_up(self, d, code):
        px, lm = self.px[d], self.lim[d]
        if code not in px.index or code not in lm.index:
            return False
        c, u = float(px.loc[code, 'close']), float(lm.loc[code, 'up_limit'])
        return abs(c - u) < max(EPS, u * 0.0005)

    def open_pct(self, d, code):
        px = self.px[d]
        if code not in px.index:
            return None
        o, pc = float(px.loc[code, 'open']), float(px.loc[code, 'pre_close'])
        if o <= 0 or pc <= 0:
            return None
        return (o / pc - 1) * 100

    def amount(self, d, code):
        px = self.px[d]
        return float(px.loc[code, 'amount']) / 1e5 if code in px.index else 0.0  # 千元 → 亿

    def limit_times(self, days, i, code):
        """从 days[i] 往回数连板。"""
        n = 0
        j = i
        while j >= 0 and self.limit_up(days[j], code):
            n += 1
            j -= 1
        return n

    def pool(self, days, i):
        """days[i] 的涨停池（含连板数），排除北交所/B 股/ST。"""
        d = days[i]
        px = self.px[d]
        codes = [c for c in px.index if not c.startswith(BOARD_EXCLUDE_PREFIX)]
        rows = []
        for c in codes:
            if not self.limit_up(d, c) or self.is_st(c):
                continue
            rows.append(dict(ts_code=c, name=self.name(c), industry=self.industry(c),
                             limit_times=self.limit_times(days, i, c)))
        return pd.DataFrame(rows)


def market_gate(bk, d):
    px = bk.px[d]
    pct = (px['open'] / px['pre_close'] - 1) * 100
    pct = pct[(px['open'] > 0) & (px['pre_close'] > 0)].dropna()
    red = float((pct > 0).mean())
    med = float(pct.median())
    return red, med, (red >= 0.40 and med > 0)


def sector_open_median(bk, d, codes):
    vals = [bk.open_pct(d, c) for c in codes]
    vals = [v for v in vals if v is not None]
    return median(vals) if vals else None


def tier_of(board, circ, turn):
    """质量分档（SKILL.md §4.5）：A 3板+ & 流通50-100亿 & 自由换手≥25%；B 其一；C 都不合；D 2板。"""
    if board >= 3:
        cap_ok = circ is not None and 50 <= circ <= 100
        to_ok = turn is not None and turn >= 25
        if cap_ok and to_ok:
            return 'A'
        return 'B' if (cap_ok or to_ok) else 'C'
    return 'D'


def run(bk, days, use_gate=True, use_leader_read=True, only_leader=False,
        first_sig=None, last_sig=None):
    """返回 (trades, skipped)"""
    trades, skipped = [], []
    for i in range(1, len(days) - 1):
        T, P, N = days[i], days[i - 1], days[i + 1]
        if (first_sig and T < first_sig) or (last_sig and T > last_sig):
            continue  # 历史缓冲区，只用它的涨停池与连板数

        # S1 档位
        red, med, gate_pass = market_gate(bk, T) if use_gate else (None, None, True)

        # S2 最高标读数
        prev_pool = bk.pool(days, i - 1)
        hw = False
        H, g_H, n_peer, m_peer = None, None, None, None
        if len(prev_pool):
            top = int(prev_pool.limit_times.max())
            leaders = prev_pool[prev_pool.limit_times == top]
            if len(leaders) == 1:
                H = leaders.iloc[0].ts_code
                g_H = bk.open_pct(T, H)
                peers = [c for c in prev_pool[prev_pool.industry == bk.industry(H)].ts_code if c != H]
                n_peer = len(peers)
                m_peer = sector_open_median(bk, T, peers)
                if g_H is not None:
                    hw = g_H > 7 and (m_peer is None or m_peer < 2) and n_peer <= 3
                    if use_leader_read and hw:
                        skipped.append(dict(date=T, reason='最高标高开无量→降档', leader=H,
                                            leader_name=bk.name(H), leader_board=top,
                                            gap=round(g_H, 2), peer_med=None if m_peer is None else round(m_peer, 2),
                                            peer_n=n_peer))
                        continue

        if use_gate and not gate_pass:
            skipped.append(dict(date=T, reason='竞价温度否决', leader=H,
                                leader_name=bk.name(H) if H else '', leader_board=None,
                                gap=None, peer_med=None, peer_n=None))
            continue

        # S3 候选
        cands = prev_pool[prev_pool.limit_times >= 2].copy()
        db = cached('basic', P)
        db = db.drop_duplicates('ts_code').set_index('ts_code') if db is not None else None
        rows = []
        for _, r in cands.iterrows():
            c = r.ts_code
            p = bk.open_pct(T, c)
            if p is None or not (3 <= p <= 7):
                continue
            px, lm = bk.px[T], bk.lim[T]
            if c in lm.index and float(px.loc[c, 'open']) >= float(lm.loc[c, 'up_limit']) - EPS:
                continue  # 一字开盘，买不到
            peers = [x for x in prev_pool[prev_pool.industry == r.industry].ts_code if x != c]
            turn = circ = None
            if db is not None and c in db.index:
                if 'turnover_rate_f' in db.columns and pd.notna(db.loc[c, 'turnover_rate_f']):
                    turn = float(db.loc[c, 'turnover_rate_f'])
                if 'circ_mv' in db.columns and pd.notna(db.loc[c, 'circ_mv']):
                    circ = float(db.loc[c, 'circ_mv']) / 1e4  # 万元 → 亿
            sec_pool = prev_pool[prev_pool.industry == r.industry]
            rows.append(dict(ts_code=c, name=r['name'], industry=r.industry,
                             limit_times=int(r.limit_times), gap=p,
                             sector_med=sector_open_median(bk, T, peers),
                             sec_max_board=int(sec_pool.limit_times.max()),
                             turnover=turn, circ=circ,
                             prev_amount=bk.amount(P, c),
                             is_leader=(c == H)))
        if only_leader:
            rows = [r for r in rows if r['is_leader']]
        if not rows:
            skipped.append(dict(date=T, reason='无窗口内候选', leader=H,
                                leader_name=bk.name(H) if H else '',
                                leader_board=int(prev_pool.limit_times.max()) if len(prev_pool) else None,
                                gap=g_H, peer_med=m_peer, peer_n=n_peer))
            continue
        # 排序（词典序）：唯一最高标永远第一；其余按 板位 → 同题材最高板 → 自由换手 → 高开 → T-1 成交额
        # 不做加权打分：136 个候选上拟合的复合权重在档位放行日反而更差（45% vs 55%），是过拟合
        rows.sort(key=lambda r: (1 if r['is_leader'] else 0, r['limit_times'], r['sec_max_board'],
                                 r['turnover'] if r['turnover'] is not None else -1,
                                 r['gap'], r['prev_amount']), reverse=True)
        pick = rows[0]

        buy = float(bk.px[T].loc[pick['ts_code'], 'open'])
        nxt = bk.px[N]
        if pick['ts_code'] not in nxt.index:
            skipped.append(dict(date=T, reason='T+1 停牌未派', leader=H, leader_name=bk.name(H) if H else '',
                                leader_board=None, gap=None, peer_med=None, peer_n=None))
            continue
        sell_c = float(nxt.loc[pick['ts_code'], 'close'])
        sell_o = float(nxt.loc[pick['ts_code'], 'open'])
        trades.append(dict(date=T, code=pick['ts_code'], name=pick['name'], industry=pick['industry'],
                           board=pick['limit_times'], gap=round(pick['gap'], 2),
                           sector_med=None if pick['sector_med'] is None else round(pick['sector_med'], 2),
                           tier=tier_of(pick['limit_times'], pick['circ'], pick['turnover']),
                           turnover=None if pick['turnover'] is None else round(pick['turnover'], 1),
                           circ=None if pick['circ'] is None else round(pick['circ'], 1),
                           is_leader=pick['is_leader'], buy=round(buy, 2), sell_close=round(sell_c, 2),
                           sell_open=round(sell_o, 2), ret=round((sell_c / buy - 1) * 100, 2),
                           ret_open=round((sell_o / buy - 1) * 100, 2),
                           win=sell_c > buy, win_open=sell_o > buy,
                           mkt_red=None if red is None else round(red * 100, 1),
                           mkt_med=None if med is None else round(med, 2),
                           n_cand=len(rows)))
    return trades, skipped


def summarize(trades, label):
    if not trades:
        print(f'\n[{label}] 无成交')
        return
    t = pd.DataFrame(trades)
    n = len(t)
    wr = t.win.mean() * 100
    print(f'\n[{label}] 笔数 {n}｜T+1 收盘卖 胜率 {wr:.1f}%｜平均 {t.ret.mean():+.2f}%｜'
          f'中位 {t.ret.median():+.2f}%｜最好 {t.ret.max():+.2f}%｜最差 {t.ret.min():+.2f}%｜'
          f'T+1 开盘卖 胜率 {t.win_open.mean()*100:.1f}% 平均 {t.ret_open.mean():+.2f}%')
    print('  按板数：' + '｜'.join(
        f"{int(b)}板 {len(g)}笔 胜率{g.win.mean()*100:.0f}% 均{g.ret.mean():+.2f}%"
        for b, g in sorted(t.groupby('board'))))
    if 'tier' in t.columns:
        print('  按质量档：' + '｜'.join(
            f"{k}档 {len(g)}笔 胜率{g.win.mean()*100:.0f}% 均{g.ret.mean():+.2f}%"
            for k, g in sorted(t.groupby('tier'))))
    ld = t[t.is_leader]
    if len(ld):
        print(f"  其中「自己就是最高标」：{len(ld)}笔 胜率{ld.win.mean()*100:.0f}% 均{ld.ret.mean():+.2f}%")
    print(f'  净值口径（每笔等权复利）：{(1 + t.ret / 100).prod():.3f} 倍')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20260730')
    ap.add_argument('--end', default='20261028', help='信号日上限；T+1 必须有行情')
    ap.add_argument('--trades', action='store_true')
    ap.add_argument('--variants', action='store_true')
    ap.add_argument('--fetch', action='store_true', help='缓存缺失时用 Tushare 补齐（需 TUSHARE_TOKEN）')
    a = ap.parse_args()

    if a.fetch:
        lo_d = (pd.Timestamp(a.start) - pd.Timedelta(days=45)).strftime('%Y%m%d')
        hi_d = (pd.Timestamp(a.end) + pd.Timedelta(days=30)).strftime('%Y%m%d')
        fetch_missing(lo_d, hi_d)

    all_days = trading_days('20260601', '20261130')
    lo = max(0, all_days.index(a.start) - 12)
    hi = min(len(all_days), all_days.index(a.end) + 3)
    days = all_days[lo:hi]
    bk = Book()
    days = [d for d in days if cached('daily', d) is not None and cached('stk_limit', d) is not None]
    for d in days:
        bk.day(d)

    print(f'区间 {days[0]}~{days[-1]}（{len(days)} 个交易日，信号日 {a.start}~{a.end}）')
    trades, skipped = run(bk, days, first_sig=a.start, last_sig=a.end)
    s = pd.DataFrame(skipped)
    print('\n否决统计：' + (s.reason.value_counts().to_string().replace('\n', '｜') if len(s) else '无'))
    summarize(trades, '主口径：档位 + 最高标读数 + 3-7% 窗口')

    if a.trades and trades:
        t = pd.DataFrame(trades)
        print('\n逐笔明细（卖=T+1 收盘）')
        print(f"{'信号日':<10}{'代码':<11}{'名称':<9}{'板':>3}{'档':>2}{'行业':<8}{'高开%':>7}{'买价':>8}"
              f"{'T+1收':>8}{'收益%':>8}{'胜':>4}{'红盘%':>7}{'候选':>5}")
        for _, r in t.iterrows():
            print(f"{r.date:<10}{r.code:<11}{str(r['name']):<9}{r.board:>3}{str(r.get('tier','-')):>2}"
                  f"{str(r.industry):<8}"
                  f"{r.gap:>7.2f}{r.buy:>8.2f}{r.sell_close:>8.2f}{r.ret:>+8.2f}{'✓' if r.win else '✗':>4}"
                  f"{r.mkt_red:>7.1f}{r.n_cand:>5}")

    if a.variants:
        print('\n===== 对照口径 =====')
        for kw, label in [
            (dict(use_gate=False), '去掉竞价温度档位'),
            (dict(use_leader_read=False), '去掉最高标读数'),
            (dict(use_gate=False, use_leader_read=False), '两者都去掉（纯 3-7% 窗口）'),
            (dict(only_leader=True), '只买唯一最高标（窗口内 + 档位）'),
        ]:
            tr, _ = run(bk, days, first_sig=a.start, last_sig=a.end, **kw)
            summarize(tr, label)


if __name__ == '__main__':
    main()
