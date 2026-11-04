#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""首板 → T+1 竞价 → T+1/T+2/T+3 行情：一年样本一张表（CSV），供特征分析。

样本定义
--------
T 日收盘首板（limit_list_d U 且 limit_times==1）；T+1 09:25 竞价后按开盘价买入（买价 = T+1 open）。
标签：T1_win = T+1 收盘 > 买价；T2_win = T+2 收盘 > 买价；T2_intraday_win = T+2 最高 > 买价；
      T1_intraday_win = T+1 最高 > 买价；T3_win / T3_intraday_win 同理。
特征：T 日涨停池字段、T 日日线/基本面/资金流/筹码/技术指标/涨停原因与题材/龙虎榜、历史形态（MA/zt20/hi60/量比）、
      T 日市场（涨停家数/炸板/最高板/晋级率/行业家数）、T+1 官方集合竞价（stk_auction）与全市场竞价温度。
全部字段说明见同目录 build_first_board_dataset.py 末尾 COLUMN_DOC（写入 *_columns.md）。

用法
----
python3 build_first_board_dataset.py --start 20251101 --end 20261103
产物：~/.hermes/cache/firstboard/first_board_1y.csv（+ _columns.md）
每日接口结果逐个落盘到 ~/.hermes/cache/fb1y/，重跑不重复调接口。
"""
import os
import sys
import time
import argparse
import datetime
import warnings

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from premarket_dragon_scan import pro  # noqa: E402

CACHE = os.path.expanduser('~/.hermes/cache/fb1y')
OUT_DIR = os.path.expanduser('~/.hermes/cache/firstboard')
LP = os.path.expanduser('~/.hermes/cache/limitpool')
os.makedirs(CACHE, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


# ---------------- 取数（逐日落盘） ----------------
def _call(fn, **kw):
    last = None
    for i, wait in enumerate((0, 3, 8, 20)):
        if wait:
            time.sleep(wait)
        try:
            return fn(**kw)
        except Exception as e:
            last = e
            if '权限' in str(e):
                raise
    raise last


def get(api, d, **kw):
    """按 (api, 日期) 缓存；limit_U / daily / stk_limit 优先复用 limitpool 老缓存。"""
    fp = os.path.join(CACHE, f'{api}_{d}.pkl')
    if os.path.exists(fp):
        return pd.read_pickle(fp)
    alt = {'limit_U': os.path.join(LP, f'lim_{d}_U.pkl'),
           'limit_Z': os.path.join(LP, f'lim_{d}_Z.pkl'),
           'daily': os.path.join(LP, f'daily_{d}.pkl'),
           'stk_limit': os.path.join(LP, f'stk_limit_{d}.pkl')}.get(api)
    if alt and os.path.exists(alt):
        try:
            df = pd.read_pickle(alt)
            if df is not None and len(df) and 'ts_code' in df.columns:
                return df
        except Exception:
            pass
    fnmap = {
        'limit_U': lambda: _call(pro.limit_list_d, trade_date=d, limit_type='U'),
        'limit_Z': lambda: _call(pro.limit_list_d, trade_date=d, limit_type='Z'),
        'daily': lambda: _call(pro.daily, trade_date=d),
        'stk_limit': lambda: _call(pro.stk_limit, trade_date=d),
        'daily_basic': lambda: _call(pro.daily_basic, trade_date=d),
        'stk_auction': lambda: _call(pro.stk_auction, trade_date=d),
        'moneyflow': lambda: _call(pro.moneyflow, trade_date=d),
        'cyq_perf': lambda: _call(pro.cyq_perf, trade_date=d),
        'stk_factor': lambda: _call(pro.stk_factor, trade_date=d,
                                    fields='ts_code,trade_date,macd_dif,macd_dea,macd,kdj_k,kdj_d,kdj_j,rsi_6,rsi_12,rsi_24,boll_upper,boll_mid,boll_lower,cci'),
        'kpl_U': lambda: _call(pro.kpl_list, trade_date=d, limit_type='U'),
        'top_list': lambda: _call(pro.top_list, trade_date=d),
    }
    df = fnmap[api]()
    if df is None:
        df = pd.DataFrame()
    pd.to_pickle(df, fp)
    time.sleep(0.12)
    return df


def trade_days(start, end):
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
    return sorted(cal.cal_date.tolist())


def hhmm_to_min(v):
    """'093015' -> 自 09:30 起的分钟数（竞价即封 = 0 以下取 0）。"""
    s = str(v or '').strip()
    if not s.isdigit():
        return np.nan
    s = s.zfill(6)
    h, m = int(s[:2]), int(s[2:4])
    t = h * 60 + m - (9 * 60 + 30)
    if h >= 13:
        t -= 90
    return max(t, 0)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20251101')
    ap.add_argument('--end', default='20261103')
    ap.add_argument('--out', default=os.path.join(OUT_DIR, 'first_board_1y.csv'))
    a = ap.parse_args()

    today = datetime.datetime.now().strftime('%Y%m%d')
    hist_start = (datetime.datetime.strptime(a.start, '%Y%m%d') - datetime.timedelta(days=110)).strftime('%Y%m%d')
    days_all = trade_days(hist_start, today)
    T_days = [d for d in days_all if a.start <= d <= a.end]
    pos = {d: i for i, d in enumerate(days_all)}
    log(f'交易日 {len(days_all)} 天（含回看），样本 T 日 {len(T_days)} 天：{T_days[0]} ~ {T_days[-1]}')

    # 1) 日线 + 涨跌停价：全区间
    D, L = [], []
    for i, d in enumerate(days_all, 1):
        D.append(get('daily', d)); L.append(get('stk_limit', d))
        if i % 40 == 0:
            log(f'  daily/stk_limit {i}/{len(days_all)}')
    DAILY = pd.concat(D, ignore_index=True)
    LIM = pd.concat(L, ignore_index=True)[['ts_code', 'trade_date', 'up_limit', 'down_limit']]
    P = DAILY.merge(LIM, on=['ts_code', 'trade_date'], how='left').sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    for c in ('open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount', 'up_limit', 'down_limit', 'pct_chg'):
        P[c] = pd.to_numeric(P[c], errors='coerce')
    P['amount'] = P['amount'] * 1000                      # 千元 -> 元
    P['is_zt'] = (P.close >= P.up_limit - 1e-6).astype(int)
    P['is_touch'] = (P.high >= P.up_limit - 1e-6).astype(int)
    P['is_dt'] = (P.close <= P.down_limit + 1e-6).astype(int)
    P['is_yizi'] = ((P.low >= P.up_limit - 1e-6) & (P.high >= P.up_limit - 1e-6)).astype(int)
    g = P.groupby('ts_code', sort=False)
    # 历史形态（截至 T，不含 T 的用 shift(1)）
    P['prev_pct'] = g['pct_chg'].shift(1)
    P['prev2_pct'] = g['pct_chg'].shift(2)
    P['prev_zt'] = g['is_zt'].shift(1)
    P['cs'] = g['close'].shift(1)
    P['ma5'] = g['cs'].transform(lambda s: s.rolling(5).mean())
    P['ma10'] = g['cs'].transform(lambda s: s.rolling(10).mean())
    P['ma20'] = g['cs'].transform(lambda s: s.rolling(20).mean())
    P['ma60'] = g['cs'].transform(lambda s: s.rolling(60, min_periods=30).mean())
    P['hi60'] = g['cs'].transform(lambda s: s.rolling(60, min_periods=30).max())
    P['lo60'] = g['cs'].transform(lambda s: s.rolling(60, min_periods=30).min())
    P['hi20'] = g['cs'].transform(lambda s: s.rolling(20).max())
    P['ret5'] = P.close / g['close'].shift(5) - 1
    P['ret10'] = P.close / g['close'].shift(10) - 1
    P['ret20'] = P.close / g['close'].shift(20) - 1
    P['ret60'] = P.close / g['close'].shift(60) - 1
    P['zt20'] = g['is_zt'].transform(lambda s: s.shift(1).rolling(20).sum())
    P['zt60'] = g['is_zt'].transform(lambda s: s.shift(1).rolling(60, min_periods=30).sum())
    P['touch20'] = g['is_touch'].transform(lambda s: s.shift(1).rolling(20).sum())
    P['adv5'] = g['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
    P['adv20'] = g['amount'].transform(lambda s: s.shift(1).rolling(20).mean())
    P['vol5_max'] = g['amount'].transform(lambda s: s.shift(1).rolling(20).max())
    P['up_days'] = g['pct_chg'].transform(lambda s: (s > 0).astype(int).groupby((s <= 0).cumsum()).cumsum())
    # 未来（T+1/T+2/T+3）
    for k in (1, 2, 3):
        for c in ('trade_date', 'open', 'high', 'low', 'close', 'amount', 'vol', 'up_limit', 'down_limit', 'is_zt', 'is_touch', 'is_yizi'):
            P[f'n{k}_{c}'] = g[c].shift(-k)
    P = P.drop(columns=['cs'])
    log(f'日线面板 {len(P)} 行')

    # 2) 静态表
    SB = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,market,list_date,exchange,area')
    SB = SB.rename(columns={'name': 'sb_name', 'industry': 'sb_industry'})

    # 3) 逐 T 日：涨停池 / 炸板池 / 基本面 / 资金流 / 筹码 / 指标 / KPL / 龙虎榜 / T+1 竞价与基本面
    rows, mkt_rows = [], []
    for i, T in enumerate(T_days, 1):
        if pos[T] + 1 >= len(days_all):
            break
        T1 = days_all[pos[T] + 1]
        Tm1 = days_all[pos[T] - 1]
        U = get('limit_U', T); Z = get('limit_Z', T); Um1 = get('limit_U', Tm1)
        B = get('daily_basic', T); B1 = get('daily_basic', T1)
        MF = get('moneyflow', T); CQ = get('cyq_perf', T); SF = get('stk_factor', T)
        K = get('kpl_U', T); TL = get('top_list', T); AU = get('stk_auction', T1)
        if i % 20 == 0:
            log(f'  T 日 {i}/{len(T_days)} {T}')
        if U is None or not len(U):
            continue
        U = U.drop_duplicates('ts_code').copy()
        for c in ('amount', 'fd_amount', 'float_mv', 'total_mv', 'turnover_ratio', 'open_times', 'limit_times', 'pct_chg', 'close'):
            U[c] = pd.to_numeric(U[c], errors='coerce')
        U['limit_times'] = U['limit_times'].fillna(1).astype(int)
        # 市场级（T）
        dT = P[P.trade_date == T]
        n_up = int(len(U)); n_z = int(len(Z)) if Z is not None else 0
        max_board = int(U.limit_times.max()) if len(U) else 0
        n_first = int((U.limit_times == 1).sum())
        n_2plus = int((U.limit_times >= 2).sum())
        n_dt = int(dT.is_dt.sum()); n_all = int(len(dT))
        mkt_amt = float(dT.amount.sum())
        med_pct = float(dT.pct_chg.median()) if n_all else np.nan
        red_T = float((dT.pct_chg > 0).mean() * 100) if n_all else np.nan
        bigdrop = int((dT.pct_chg <= -8).sum())
        # 晋级率（T-1 首板 → T 连板）
        if Um1 is not None and len(Um1):
            f1 = Um1[pd.to_numeric(Um1.limit_times, errors='coerce').fillna(1).astype(int) == 1]
            promo_first = float(len(set(f1.ts_code) & set(U.ts_code)) / max(len(f1), 1) * 100)
            promo_all = float(len(set(Um1.ts_code) & set(U.ts_code)) / max(len(Um1), 1) * 100)
            n_up_m1 = int(len(Um1))
        else:
            promo_first = promo_all = np.nan; n_up_m1 = np.nan
        # T+1 竞价温度（全市场 + 昨日涨停池）
        if AU is not None and len(AU):
            AU = AU.drop_duplicates('ts_code').copy()
            for c in ('vol', 'price', 'amount', 'pre_close', 'turnover_rate', 'volume_ratio', 'float_share'):
                AU[c] = pd.to_numeric(AU[c], errors='coerce')
            AU['au_gap'] = (AU.price / AU.pre_close - 1) * 100
            au_red = float((AU.au_gap > 0).mean() * 100); au_med = float(AU.au_gap.median())
            au_hi3 = int((AU.au_gap >= 3).sum()); au_lo3 = int((AU.au_gap <= -3).sum())
            pool = AU[AU.ts_code.isin(U.ts_code)]
            au_pool_red = float((pool.au_gap > 0).mean() * 100) if len(pool) else np.nan
            au_pool_med = float(pool.au_gap.median()) if len(pool) else np.nan
            AUi = AU.set_index('ts_code')
        else:
            au_red = au_med = au_pool_red = au_pool_med = np.nan; au_hi3 = au_lo3 = np.nan; AUi = None
        mkt = dict(trade_date=T, T1_date=T1, mkt_n_up=n_up, mkt_n_zha=n_z, mkt_zha_rate=n_z / max(n_up + n_z, 1) * 100,
                   mkt_max_board=max_board, mkt_n_first=n_first, mkt_n_2plus=n_2plus, mkt_n_dt=n_dt, mkt_n_stocks=n_all,
                   mkt_amount_yi=mkt_amt / 1e8, mkt_med_pct=med_pct, mkt_red_pct=red_T, mkt_bigdrop=bigdrop,
                   mkt_n_up_prev=n_up_m1, mkt_promo_first=promo_first, mkt_promo_all=promo_all,
                   au_mkt_red=au_red, au_mkt_med=au_med, au_mkt_hi3=au_hi3, au_mkt_lo3=au_lo3,
                   au_pool_red=au_pool_red, au_pool_med=au_pool_med)
        mkt_rows.append(mkt)
        # 行业 / 题材口径
        ind_cnt = U.groupby('industry').size().to_dict()
        ind_max = U.groupby('industry').limit_times.max().to_dict()
        ind_first = U[U.limit_times == 1].groupby('industry').size().to_dict()
        theme_cnt, theme_max, stock_theme, kpl_i = {}, {}, {}, None
        if K is not None and len(K):
            K = K.drop_duplicates('ts_code').copy()
            kpl_i = K.set_index('ts_code')
            for _, kr in K.iterrows():
                ts = [x.strip() for x in str(kr.get('theme') or '').replace('，', '、').replace(',', '、').split('、') if x.strip()]
                stock_theme[kr['ts_code']] = ts
                st = str(kr.get('status') or '')
                lt = 1
                try:
                    lt = int(st.split('天')[-1].replace('板', '')) if '板' in st else 1
                except Exception:
                    lt = 1
                for t in ts:
                    theme_cnt[t] = theme_cnt.get(t, 0) + 1
                    theme_max[t] = max(theme_max.get(t, 1), lt)
        Bi = B.drop_duplicates('ts_code').set_index('ts_code') if B is not None and len(B) else None
        B1i = B1.drop_duplicates('ts_code').set_index('ts_code') if B1 is not None and len(B1) else None
        MFi = MF.drop_duplicates('ts_code').set_index('ts_code') if MF is not None and len(MF) else None
        CQi = CQ.drop_duplicates('ts_code').set_index('ts_code') if CQ is not None and len(CQ) else None
        SFi = SF.drop_duplicates('ts_code').set_index('ts_code') if SF is not None and len(SF) else None
        TLi = TL.drop_duplicates('ts_code').set_index('ts_code') if TL is not None and len(TL) else None

        first = U[U.limit_times == 1]
        for _, r in first.iterrows():
            c = r['ts_code']
            row = dict(ts_code=c, trade_date=T, name=r['name'], industry=r['industry'],
                       lu_close=r['close'], lu_pct=r['pct_chg'], lu_amount=r['amount'], lu_fd_amount=r['fd_amount'],
                       lu_fd_pct=(r['fd_amount'] / r['amount'] * 100) if r['amount'] else np.nan,
                       lu_float_mv_yi=r['float_mv'] / 1e8 if r['float_mv'] == r['float_mv'] else np.nan,
                       lu_total_mv_yi=r['total_mv'] / 1e8 if r['total_mv'] == r['total_mv'] else np.nan,
                       lu_turnover=r['turnover_ratio'], lu_first_time=r['first_time'], lu_last_time=r['last_time'],
                       lu_first_min=hhmm_to_min(r['first_time']), lu_last_min=hhmm_to_min(r['last_time']),
                       lu_open_times=r['open_times'], lu_up_stat=r['up_stat'],
                       ind_cnt=ind_cnt.get(r['industry'], 0), ind_max_board=ind_max.get(r['industry'], 1),
                       ind_first_cnt=ind_first.get(r['industry'], 0))
            ths = stock_theme.get(c, [])
            row['kpl_theme'] = '、'.join(ths)
            row['theme_cnt_max'] = max([theme_cnt.get(t, 0) for t in ths], default=0)
            row['theme_board_max'] = max([theme_max.get(t, 1) for t in ths], default=0)
            row['peer_cnt'] = max(row['ind_cnt'], row['theme_cnt_max'])
            row['peer_board_max'] = max(row['ind_max_board'], row['theme_board_max'])
            if kpl_i is not None and c in kpl_i.index:
                kr = kpl_i.loc[c]
                row['kpl_lu_desc'] = kr.get('lu_desc'); row['kpl_status'] = kr.get('status')
                row['kpl_bid_amount'] = pd.to_numeric(kr.get('bid_amount'), errors='coerce')
                row['kpl_bid_change'] = pd.to_numeric(kr.get('bid_change'), errors='coerce')
                row['kpl_net_change'] = pd.to_numeric(kr.get('net_change'), errors='coerce')
            if Bi is not None and c in Bi.index:
                b = Bi.loc[c]
                for k in ('turnover_rate', 'turnover_rate_f', 'volume_ratio', 'pe_ttm', 'pb', 'total_mv', 'circ_mv', 'free_share', 'float_share'):
                    row[f'b_{k}'] = pd.to_numeric(b.get(k), errors='coerce')
            if MFi is not None and c in MFi.index:
                m = MFi.loc[c]
                for k in ('buy_sm_amount', 'sell_sm_amount', 'buy_md_amount', 'sell_md_amount', 'buy_lg_amount', 'sell_lg_amount',
                          'buy_elg_amount', 'sell_elg_amount', 'net_mf_amount'):
                    row[f'mf_{k}'] = pd.to_numeric(m.get(k), errors='coerce')
            if CQi is not None and c in CQi.index:
                q = CQi.loc[c]
                for k in ('his_low', 'his_high', 'cost_5pct', 'cost_50pct', 'cost_85pct', 'cost_95pct', 'weight_avg', 'winner_rate'):
                    row[f'cyq_{k}'] = pd.to_numeric(q.get(k), errors='coerce')
            if SFi is not None and c in SFi.index:
                f = SFi.loc[c]
                for k in ('macd_dif', 'macd_dea', 'macd', 'kdj_k', 'kdj_d', 'kdj_j', 'rsi_6', 'rsi_12', 'rsi_24', 'boll_upper', 'boll_mid', 'boll_lower', 'cci'):
                    row[f'tf_{k}'] = pd.to_numeric(f.get(k), errors='coerce')
            row['top_list'] = int(TLi is not None and c in TLi.index)
            if TLi is not None and c in TLi.index:
                t = TLi.loc[c]
                for k in ('l_buy', 'l_sell', 'net_amount', 'net_rate', 'amount_rate'):
                    row[f'top_{k}'] = pd.to_numeric(t.get(k), errors='coerce')
            if AUi is not None and c in AUi.index:
                au = AUi.loc[c]
                row['au_amount'] = au['amount']; row['au_vol'] = au['vol']; row['au_price'] = au['price']
                row['au_pre_close'] = au['pre_close']; row['au_turnover'] = au['turnover_rate']
                row['au_volume_ratio'] = au['volume_ratio']; row['au_gap'] = au['au_gap']
            if B1i is not None and c in B1i.index:
                b1 = B1i.loc[c]
                row['t1_turnover_rate'] = pd.to_numeric(b1.get('turnover_rate'), errors='coerce')
                row['t1_volume_ratio'] = pd.to_numeric(b1.get('volume_ratio'), errors='coerce')
            rows.append(row)
    S = pd.DataFrame(rows)
    M = pd.DataFrame(mkt_rows)
    log(f'样本 {len(S)} 条（T 日 {S.trade_date.nunique()} 天）')

    # 4) 合并日线面板（T 日特征 + T+1..T+3）
    keep = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount', 'up_limit', 'down_limit',
            'is_yizi', 'is_touch', 'prev_pct', 'prev2_pct', 'prev_zt', 'ma5', 'ma10', 'ma20', 'ma60', 'hi60', 'lo60', 'hi20',
            'ret5', 'ret10', 'ret20', 'ret60', 'zt20', 'zt60', 'touch20', 'adv5', 'adv20', 'vol5_max', 'up_days'] + \
           [c for c in P.columns if c.startswith('n1_') or c.startswith('n2_') or c.startswith('n3_')]
    S = S.merge(P[keep].rename(columns={'open': 'd_open', 'high': 'd_high', 'low': 'd_low', 'close': 'd_close', 'pre_close': 'd_pre_close',
                                        'vol': 'd_vol', 'amount': 'd_amount', 'up_limit': 'd_up_limit', 'down_limit': 'd_down_limit',
                                        'is_yizi': 'd_is_yizi', 'is_touch': 'd_is_touch'}),
                on=['ts_code', 'trade_date'], how='left')
    S = S.merge(M, on='trade_date', how='left')
    S = S.merge(SB[['ts_code', 'market', 'list_date', 'area', 'sb_industry']], on='ts_code', how='left')

    # 5) 派生特征
    S['board_type'] = S.market.map({'主板': '10cm', '创业板': '20cm', '科创板': '20cm', '北交所': '30cm'})
    S['is_st'] = S.name.fillna('').str.contains('ST').astype(int)
    S['listed_days'] = (pd.to_datetime(S.trade_date) - pd.to_datetime(S.list_date, errors='coerce')).dt.days
    S['d_gap_T'] = (S.d_open / S.d_pre_close - 1) * 100            # T 日自己的高开
    S['d_low_pct_T'] = (S.d_low / S.d_pre_close - 1) * 100         # T 日最低点涨幅（低开/下探深度）
    S['d_amp_T'] = (S.d_high - S.d_low) / S.d_pre_close * 100
    S['vr5'] = S.lu_amount / S.adv5                                 # 首板额 / 前 5 日均额
    S['vr20'] = S.lu_amount / S.adv20
    S['amt_vs_max20'] = S.lu_amount / S.vol5_max
    S['dev_ma5'] = (S.lu_close / S.ma5 - 1) * 100
    S['dev_ma10'] = (S.lu_close / S.ma10 - 1) * 100
    S['dev_ma20'] = (S.lu_close / S.ma20 - 1) * 100
    S['dev_ma60'] = (S.lu_close / S.ma60 - 1) * 100
    S['dist_hi60'] = (S.lu_close / S.hi60 - 1) * 100
    S['dist_lo60'] = (S.lu_close / S.lo60 - 1) * 100
    S['dist_hi20'] = (S.lu_close / S.hi20 - 1) * 100
    S['mkt_share_pct'] = S.lu_amount / (S.mkt_amount_yi * 1e8) * 100
    S['mf_net_lg_pct'] = ((S.get('mf_buy_lg_amount', 0).fillna(0) + S.get('mf_buy_elg_amount', 0).fillna(0)
                           - S.get('mf_sell_lg_amount', 0).fillna(0) - S.get('mf_sell_elg_amount', 0).fillna(0)) * 1e4 / S.lu_amount * 100)
    S['mf_net_pct'] = S.get('mf_net_mf_amount', np.nan) * 1e4 / S.lu_amount * 100
    S['cyq_close_vs_avg'] = (S.lu_close / S.get('cyq_weight_avg', np.nan) - 1) * 100
    S['au_ratio_prev'] = S.au_amount / S.lu_amount * 100            # 竞昨比：T+1 竞价额 / T 全天
    S['au_amount_yi'] = S.au_amount / 1e8
    S['au_is_yizi'] = ((S.au_price >= S.n1_up_limit - 0.011)).astype('Int64')
    S['au_float_pct'] = S.au_vol / (S.b_float_share * 1e4) * 100 if 'b_float_share' in S else np.nan
    # 买入与标签
    S['buy'] = S.n1_open
    S['t1_tradable'] = ((S.n1_up_limit - S.n1_open) / S.d_close > 0.003).astype('Int64')
    S['T1_gap'] = (S.n1_open / S.d_close - 1) * 100
    S['T1_ret'] = (S.n1_close / S.buy - 1) * 100
    S['T1_max_ret'] = (S.n1_high / S.buy - 1) * 100
    S['T1_min_ret'] = (S.n1_low / S.buy - 1) * 100
    S['T1_close_pct'] = (S.n1_close / S.d_close - 1) * 100
    S['T1_win'] = (S.n1_close > S.buy).astype('Int64')
    S['T1_intraday_win'] = (S.n1_high > S.buy).astype('Int64')
    S['T1_seal'] = S.n1_is_zt.astype('Int64')
    S['T1_touch'] = S.n1_is_touch.astype('Int64')
    S['T1_amt_ratio'] = S.n1_amount / S.lu_amount
    S['T2_ret'] = (S.n2_close / S.buy - 1) * 100
    S['T2_max_ret'] = (S.n2_high / S.buy - 1) * 100
    S['T2_open_ret'] = (S.n2_open / S.buy - 1) * 100
    S['T2_win'] = (S.n2_close > S.buy).astype('Int64')
    S['T2_intraday_win'] = (S.n2_high > S.buy).astype('Int64')
    S['T2_open_win'] = (S.n2_open > S.buy).astype('Int64')
    S['T2_seal'] = S.n2_is_zt.astype('Int64')
    S['T3_ret'] = (S.n3_close / S.buy - 1) * 100
    S['T3_max_ret'] = (S.n3_high / S.buy - 1) * 100
    S['T3_win'] = (S.n3_close > S.buy).astype('Int64')
    S['T3_intraday_win'] = (S.n3_high > S.buy).astype('Int64')
    S['t1_date_ok'] = (S.n1_trade_date == S.T1_date).astype('Int64')   # 停牌时 shift(-1) 不是真正 T+1
    for k in (1, 2, 3):
        S[f'n{k}_date'] = S[f'n{k}_trade_date']
        S = S.drop(columns=[f'n{k}_trade_date'])
    for c in S.columns:
        if S[c].dtype == object:
            S[c] = S[c].astype(str).replace({'nan': '', 'None': ''})
    S = S.sort_values(['trade_date', 'ts_code']).reset_index(drop=True)
    S.to_csv(a.out, index=False, encoding='utf-8-sig')
    log(f'写出 {a.out}：{len(S)} 行 × {S.shape[1]} 列')
    with open(a.out.replace('.csv', '_columns.md'), 'w') as f:
        f.write(COLUMN_DOC)
        f.write('\n\n## 实际列清单\n\n' + '\n'.join(f'- `{c}`' for c in S.columns) + '\n')


COLUMN_DOC = """# first_board_1y.csv 字段说明

样本：T 日收盘首板（limit_list_d limit_times==1），一行一只。买价 = T+1 开盘价（`buy` = `n1_open`，即 9:25 竞价成交价）。
金额单位：元（`*_yi` 为亿元）；百分比列直接是 %。

## 标识 / 分类
- `ts_code` `name` `industry`（limit_list_d 行业）`sb_industry`（stock_basic 行业）`market` `board_type`（10cm/20cm/30cm）`is_st` `area` `list_date` `listed_days`
- `trade_date`=T；`T1_date`=T+1 交易日；`n1_date`/`n2_date`/`n3_date`=该股实际的下 1/2/3 条日线日期（停牌会错位，`t1_date_ok`=1 表示 n1 就是 T+1）

## T 日涨停池（limit_list_d）
- `lu_close` `lu_pct` `lu_amount`（全天成交额）`lu_fd_amount`（封单额）`lu_fd_pct`（封单/成交 %）`lu_float_mv_yi` `lu_total_mv_yi` `lu_turnover`
- `lu_first_time` `lu_last_time`（HHMMSS）`lu_first_min` `lu_last_min`（自 09:30 起分钟，去掉午休）`lu_open_times`（炸板次数）`lu_up_stat`（几天几板）

## T 日日线 / 形态（daily + 自算）
- `d_open` `d_high` `d_low` `d_close` `d_pre_close` `d_vol` `d_amount` `d_up_limit` `d_down_limit` `d_is_yizi`（T 日一字）`d_is_touch`
- `d_gap_T`（T 日高开 %）`d_low_pct_T`（T 日最低点相对昨收 %）`d_amp_T`（振幅）
- `prev_pct` `prev2_pct`（T-1/T-2 涨幅）`prev_zt`（T-1 是否涨停，首板应为 0）`up_days`（含 T 的连续上涨天数）
- `ma5` `ma10` `ma20` `ma60`（截至 T-1）`dev_ma5/10/20/60`（T 收盘偏离 %）`hi60` `lo60` `hi20` `dist_hi60` `dist_lo60` `dist_hi20`
- `ret5` `ret10` `ret20` `ret60`（含 T 的区间涨幅，小数）`zt20` `zt60`（此前 20/60 日涨停次数）`touch20`
- `adv5` `adv20`（前 5/20 日均额）`vr5` `vr20`（首板额/均额）`vol5_max`（前 20 日最大成交额）`amt_vs_max20`

## T 日基本面 / 资金 / 筹码 / 指标
- `b_turnover_rate` `b_turnover_rate_f`（自由流通换手）`b_volume_ratio`（官方量比）`b_pe_ttm` `b_pb` `b_total_mv` `b_circ_mv`（万元）`b_free_share` `b_float_share`（万股）
- `mf_*`（moneyflow，万元）`mf_net_lg_pct`（大单+特大单净流入/成交额 %）`mf_net_pct`
- `cyq_*`（筹码分布）`cyq_winner_rate`（获利盘 %）`cyq_close_vs_avg`（收盘相对平均成本 %）
- `tf_*`（stk_factor 技术指标：macd/kdj/rsi/boll/cci）
- `top_list`（是否上龙虎榜）`top_l_buy` `top_l_sell` `top_net_amount` `top_net_rate` `top_amount_rate`
- `kpl_lu_desc`（涨停原因）`kpl_theme` `kpl_status` `kpl_bid_amount` `kpl_bid_change` `kpl_net_change`

## 板块（T 日）
- `ind_cnt`（同行业涨停家数）`ind_max_board`（同行业最高板）`ind_first_cnt`（同行业首板数）
- `theme_cnt_max`（所属题材里涨停家数最多的那个）`theme_board_max`；`peer_cnt` `peer_board_max` = 行业/题材取大者

## 市场（T 日，`mkt_*`）与 T+1 竞价温度（`au_mkt_*` / `au_pool_*`）
- `mkt_n_up` `mkt_n_zha` `mkt_zha_rate` `mkt_max_board` `mkt_n_first` `mkt_n_2plus` `mkt_n_dt` `mkt_n_stocks` `mkt_amount_yi` `mkt_med_pct` `mkt_red_pct` `mkt_bigdrop`
- `mkt_n_up_prev` `mkt_promo_first`（T-1 首板 → T 二板晋级率）`mkt_promo_all`
- `au_mkt_red`（T+1 全市场竞价红盘 %）`au_mkt_med` `au_mkt_hi3` `au_mkt_lo3` `au_pool_red`（T 日涨停池 T+1 竞价红盘 %）`au_pool_med`
- `mkt_share_pct`（首板额占全市场 %）

## T+1 集合竞价（stk_auction，9:25 即知）
- `au_amount` `au_amount_yi` `au_vol` `au_price`（=开盘价）`au_pre_close` `au_turnover`（竞价换手 %）`au_volume_ratio`（竞价量比）`au_gap`（竞价涨幅 %）
- `au_ratio_prev`（竞昨比：竞价额/首板全天 %）`au_is_yizi`（竞价即涨停价）`au_float_pct`（竞价量/流通股 %）
- `t1_turnover_rate` `t1_volume_ratio`（T+1 全天，盘后才知，只作对照）

## T+1 / T+2 / T+3 行情与标签（`n1_*` `n2_*` `n3_*` 原始；下面是派生）
- `buy`（=T+1 开盘）`t1_tradable`（开盘距涨停 >0.3%）`T1_gap`（T+1 高开 %）
- `T1_ret`（T+1 收盘/买价 %）`T1_max_ret` `T1_min_ret` `T1_close_pct`（T+1 当日涨跌 %）`T1_amt_ratio`（T+1 成交/首板全天）
- `T1_win`（收盘 > 买价）`T1_intraday_win`（最高 > 买价）`T1_seal`（T+1 封板）`T1_touch`
- `T2_ret` `T2_max_ret` `T2_open_ret` `T2_win` `T2_intraday_win` `T2_open_win` `T2_seal`
- `T3_ret` `T3_max_ret` `T3_win` `T3_intraday_win`
"""


if __name__ == '__main__':
    main()
