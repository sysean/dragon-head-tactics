#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""首板战法（集合竞价版）：历史回测 + 当日 9:25 竞价选股。

用法:
  python3 first_board_auction.py --mode backtest --start 20260401 --end 20261021
  python3 first_board_auction.py --mode today --top 18

口径（重要）:
  · 首板 = 当日收盘封住涨停 且 前一交易日未涨停。
  · 竞价信息 = 开盘价（A 股 09:25 集合竞价成交价）/ 昨收 - 1，9:25 即可知，**无未来函数**。
  · 三种收益口径必须分开报，混着报会得出相反结论：
      竞价买入  = 9:25 竞价价买入（即开盘价），当日收盘卖 → close/open-1  [高开股普遍亏]
      打板      = 盘中触板即以涨停价成交；封住持有到次日开盘卖，炸板当日收盘卖
      持有到断板 = 以首板涨停价买入，持有到第一个断板日收盘卖
  · 竞价成交额历史无权限（Tushare stk_auction_o 需 5000 积分），当日用新浪 1 分钟首根
    （09:31，含集合竞价成交）成交额作代理；回测段只用价格类 + 昨日形态 + 流通盘。
数据源：Tushare Pro（daily / daily_basic / stk_limit / stock_basic / trade_cal）；
        实时开盘价用 ts.realtime_quote 爬虫接口；实时涨停池用东财 push2ex；分钟成交额用新浪。
"""
import os, sys, re, json, time, ssl, argparse, warnings, urllib.request
warnings.filterwarnings('ignore')
if sys.executable != '/usr/local/bin/python3':
    try:
        import pandas  # noqa
    except ImportError:
        os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3'] + sys.argv)
import pandas as pd, numpy as np, tushare as ts

CACHE = os.path.expanduser('~/.hermes/cache/firstboard')
os.makedirs(CACHE, exist_ok=True)
TOK = os.environ.get('TUSHARE_TOKEN')
if not TOK:
    for line in open(os.path.expanduser('~/.hermes/.env')):
        if line.startswith('TUSHARE_TOKEN'):
            TOK = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
pro = ts.pro_api(TOK)
ts.set_token(TOK)
BM = ('600', '601', '603', '605', '000', '001', '002', '003', '300', '301')   # 主板 + 创业板
CTX = ssl._create_unverified_context()
pd.set_option('display.width', 250)


def log(*a):
    print(*a, flush=True)


def trade_days(start, end):
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
    return sorted(cal[cal.is_open == 1].cal_date.tolist())


def _cached(name, fn):
    p = os.path.join(CACHE, name + '.pkl')
    if os.path.exists(p):
        return pd.read_pickle(p)
    df = fn()
    df.to_pickle(p)
    return df


# ---------------- 面板 ----------------
def build_panel(start, end):
    days = trade_days(start, end)
    log(f'交易日 {len(days)} 天：{days[0]} ~ {days[-1]}')
    for i, d in enumerate(days, 1):
        _cached(f'daily_{d}', lambda d=d: pro.daily(trade_date=d))
        _cached(f'limit_{d}', lambda d=d: pro.stk_limit(trade_date=d))
        _cached(f'basic_{d}', lambda d=d: pro.daily_basic(
            trade_date=d, fields='ts_code,circ_mv,turnover_rate_f,volume_ratio'))
        if i % 30 == 0 or i == len(days):
            log(f'  缓存 {i}/{len(days)}')
    dy = pd.concat([pd.read_pickle(os.path.join(CACHE, f'daily_{d}.pkl')) for d in days], ignore_index=True)
    lm = pd.concat([pd.read_pickle(os.path.join(CACHE, f'limit_{d}.pkl')) for d in days], ignore_index=True)
    bl = []
    for d in days:
        b = pd.read_pickle(os.path.join(CACHE, f'basic_{d}.pkl')).copy()
        b['trade_date'] = d          # daily_basic 用 trade_date 查询时不回传该列
        bl.append(b)
    bs = pd.concat(bl, ignore_index=True)
    df = dy.merge(lm[['ts_code', 'trade_date', 'up_limit', 'down_limit']], on=['ts_code', 'trade_date'], how='left')
    df = df.merge(bs[['ts_code', 'trade_date', 'circ_mv', 'turnover_rate_f', 'volume_ratio']],
                  on=['ts_code', 'trade_date'], how='left')
    return df.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)


def features(df):
    """特征只用 T-1 及更早 + 今日开盘价（09:25 竞价成交价）。"""
    g = df.groupby('ts_code', sort=False)
    df['prev_close'] = g['close'].shift(1)
    df['prev_pct'] = g['pct_chg'].shift(1)
    df['prev_amount'] = g['amount'].shift(1) * 1000          # 千元 -> 元
    df['prev_up'] = g['up_limit'].shift(1)
    df['prev_turn_f'] = g['turnover_rate_f'].shift(1)
    df['prev_vr'] = g['volume_ratio'].shift(1)
    df['prev_zt'] = (df['prev_close'] >= df['prev_up'] - 1e-6).astype('int8')
    df['is_zt'] = (df['close'] >= df['up_limit'] - 1e-6).astype('int8')
    df['is_touch'] = (df['high'] >= df['up_limit'] - 1e-6).astype('int8')
    df['gap'] = df['open'] / df['pre_close'] - 1
    df['cs'] = g['close'].shift(1)
    df['ma5'] = g['cs'].transform(lambda s: s.rolling(5).mean())
    df['ma20'] = g['cs'].transform(lambda s: s.rolling(20).mean())
    df['hi60'] = g['cs'].transform(lambda s: s.rolling(60).max())
    df['dev_ma20'] = df['cs'] / df['ma20'] - 1
    df['dist_hi60'] = df['cs'] / df['hi60'] - 1
    df['zt20'] = g['is_zt'].transform(lambda s: s.shift(1).rolling(20).sum())
    df['next_open'] = g['open'].shift(-1)
    df['next_pct'] = g['pct_chg'].shift(-1)
    df['next_zt'] = g['is_zt'].shift(-1)
    df['r_open_close'] = (df['close'] / df['open'] - 1) * 100          # 竞价买入，收盘卖
    df['r_daban_next'] = (df['next_open'] / df['up_limit'] - 1) * 100  # 打板成交，次日开盘卖
    df['r_daban_hold'] = (df['close'] / df['up_limit'] - 1) * 100      # 打板炸板当日收盘卖
    df['tradable'] = ((df['up_limit'] - df['open']) / df['pre_close']) > 0.003
    return df


def universe(df, sb):
    sb = sb[['ts_code', 'name', 'industry', 'list_date', 'market']]
    df = df.merge(sb, on='ts_code', how='left')
    df = df[df.ts_code.str[:3].isin(BM)]
    df = df[~df.name.fillna('').str.contains('ST|退')]
    df['listed_days'] = (pd.to_datetime(df.trade_date) - pd.to_datetime(df.list_date, errors='coerce')).dt.days
    df = df[df.listed_days >= 60]
    df['circ_yi'] = df['circ_mv'] / 1e4          # 万元 -> 亿元
    return df


def add_resonance(df):
    df['hot'] = (df['gap'] >= 0.02).astype(int)
    df['ind_hot'] = df.groupby(['trade_date', 'industry'])['hot'].transform('sum')
    return df


def load_panel(start, end):
    PP = os.path.join(CACHE, f'panel_{start}_{end}.pkl')
    if os.path.exists(PP):
        return pd.read_pickle(PP)
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,list_date,market')
    d = build_panel(start, end).dropna(subset=['open', 'pre_close', 'up_limit'])
    d = features(d); d = universe(d, sb); d = add_resonance(d)
    d = d.dropna(subset=['gap', 'prev_pct', 'circ_yi', 'dist_hi60', 'dev_ma20', 'ind_hot'])
    d.to_pickle(PP)
    return d


def hold_to_break(df, sel):
    """首板涨停价买入、持有到第一个断板日收盘：返回 cont（继续板数）/ hold_ret。"""
    idx = {c: g.index.tolist() for c, g in df.groupby('ts_code')}
    conts, rets = [], []
    for i in sel.index:
        seq = idx[df.at[i, 'ts_code']]
        k = seq.index(i)
        cont, exit_px = 0, np.nan
        for j in seq[k + 1:]:
            if df.at[j, 'is_zt'] == 1:
                cont += 1
            else:
                exit_px = df.at[j, 'close']
                break
        conts.append(cont)
        rets.append((exit_px / df.at[i, 'up_limit'] - 1) * 100 if exit_px == exit_px else np.nan)
    out = sel.copy()
    out['cont'] = conts
    out['hold_ret'] = rets
    return out


def ev(sub, days, label):
    if not len(sub):
        return dict(规则=label, 样本=0)
    toc = sub[sub.is_touch == 1]
    zt = sub[sub.is_zt == 1]
    h = sub.dropna(subset=['hold_ret']) if 'hold_ret' in sub else pd.DataFrame()
    return dict(
        规则=label, 样本=len(sub), 日均=round(len(sub) / days, 1),
        封板率=round(sub.is_zt.mean() * 100, 1),
        触板率=round(sub.is_touch.mean() * 100, 1),
        竞价买入日内=round(sub.r_open_close.mean(), 2),
        打板T1期望=round(pd.Series(np.where(toc.is_zt == 1, toc.r_daban_next, toc.r_daban_hold)).mean(), 2) if len(toc) else np.nan,
        封板次日溢价=round(zt.r_daban_next.mean(), 2) if len(zt) else np.nan,
        一进二=round(pd.to_numeric(zt.next_zt, errors='coerce').mean() * 100, 1) if len(zt) else np.nan,
        持有到断板=round(h.hold_ret.mean(), 2) if len(h) else np.nan,
    )


def show(rows, title):
    log('\n' + '=' * 118)
    log(title)
    log('=' * 118)
    log(pd.DataFrame(rows).to_string(index=False))


# ---------------- 当日 ----------------
def em(url, ref='https://quote.eastmoney.com/'):
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': ref})
            return urllib.request.urlopen(req, timeout=12, context=CTX).read()
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def rt_snapshot(codes):
    rows = []
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        for attempt in range(3):
            try:
                q = ts.realtime_quote(ts_code=','.join(chunk))
                if q is not None and len(q):
                    rows.append(q)
                break
            except Exception as e:
                if attempt == 2:
                    log(f'  chunk {i} 失败 {str(e)[:80]}')
                time.sleep(2)
        time.sleep(0.2)
    q = pd.concat(rows, ignore_index=True).drop_duplicates('TS_CODE')
    for c in ['OPEN', 'PRE_CLOSE', 'PRICE', 'HIGH', 'LOW', 'AMOUNT', 'VOLUME']:
        if c in q.columns:
            q[c] = pd.to_numeric(q[c], errors='coerce')
    return q


def sina_first_min(ts_code):
    """新浪 1 分钟首根（09:31，含集合竞价）：(成交额元, 竞价价)。"""
    sym = ('sh' if ts_code.endswith('.SH') else ('bj' if ts_code.endswith('.BJ') else 'sz')) + ts_code[:6]
    url = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData'
           f'?symbol={sym}&scale=1&ma=no&datalen=320')
    for _ in range(3):
        try:
            txt = em(url, 'https://finance.sina.com.cn/').decode('utf-8', 'ignore')
            m = re.search(r'=\s*\((\[.*\])\)', txt, re.S)
            if not m:
                return (np.nan, np.nan)
            today = pd.Timestamp.today().strftime('%Y-%m-%d')
            for b in json.loads(m.group(1)):
                if b['day'].startswith(today) and b['day'][11:16] in ('09:30', '09:31'):
                    return (float(b['amount']), float(b['open']))
            return (np.nan, np.nan)
        except Exception:
            time.sleep(1.2)
    return (np.nan, np.nan)


def em_zt_pool(date):
    url = ('https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989'
           f'&dpt=wz.ztzt&Pageindex=0&pagesize=600&sort=fbt%3Aasc&date={date}&_=1')
    try:
        js = json.loads(em(url, 'https://quote.eastmoney.com/ztb/detail').decode('utf-8', 'ignore'))
        pool = (js.get('data') or {}).get('pool') or []
    except Exception as e:
        log('涨停池获取失败', str(e)[:120]); return pd.DataFrame()
    rows = [dict(code=p['c'], name=p['n'], zdp=p['zdp'], amount=p['amount'], ltsz=p['ltsz'],
                 hs=p['hs'], lbc=p['lbc'], fbt=p['fbt'], fund=p['fund'], zbc=p['zbc'],
                 hybk=p['hybk']) for p in pool]
    df = pd.DataFrame(rows)
    if len(df):
        df['fbt'] = df['fbt'].apply(lambda v: f'{v//10000:02d}:{v//100%100:02d}:{v%100:02d}')
    return df


def hist_features(yday):
    """缓存日线算：20 日涨停基因、60 日最高收盘、MA5/MA20（截至 yday）。"""
    days = [d for d in trade_days('20260101', yday)[-70:]
            if os.path.exists(os.path.join(CACHE, f'daily_{d}.pkl'))]
    if not days:
        return pd.DataFrame()
    dy = pd.concat([pd.read_pickle(os.path.join(CACHE, f'daily_{d}.pkl')) for d in days], ignore_index=True)
    lm = pd.concat([pd.read_pickle(os.path.join(CACHE, f'limit_{d}.pkl')) for d in days], ignore_index=True)
    d = dy.merge(lm[['ts_code', 'trade_date', 'up_limit']], on=['ts_code', 'trade_date'], how='left')
    d['is_zt'] = (d.close >= d.up_limit - 1e-6).astype(int)
    d = d[d.trade_date <= yday].sort_values(['ts_code', 'trade_date'])
    g = d.groupby('ts_code', sort=False)
    out = pd.DataFrame({'ts_code': d.ts_code.values, 'trade_date': d.trade_date.values,
                        'zt20': g['is_zt'].transform(lambda s: s.tail(20).sum()).values,
                        'hi60': g['close'].transform(lambda s: s.tail(60).max()).values,
                        'ma20': g['close'].transform(lambda s: s.tail(20).mean()).values,
                        'ma5': g['close'].transform(lambda s: s.tail(5).mean()).values})
    return out[out.trade_date == yday]


def mode_today(top):
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,list_date,market')
    sb = sb[sb.ts_code.str[:3].isin(BM) & ~sb.name.str.contains('ST|退')]
    log(f'候选池 {len(sb)} 只，拉实时快照（OPEN = 09:25 集合竞价成交价）…')
    q = rt_snapshot(sb.ts_code.tolist())
    if not len(q):
        log('实时快照失败'); return
    log(f'快照 {len(q)} 行，时间 {q.TIME.iloc[0]}')
    q = q.rename(columns={'TS_CODE': 'ts_code', 'OPEN': 'open', 'PRE_CLOSE': 'pre_close',
                          'PRICE': 'price', 'HIGH': 'high', 'LOW': 'low', 'AMOUNT': 'amount_now'})
    q['gap'] = q.open / q.pre_close - 1
    q['pct_now'] = q.price / q.pre_close - 1
    today = pd.Timestamp.today().strftime('%Y%m%d')
    days = trade_days('20260101', today)
    yday = days[-2] if (days and days[-1] == today) else days[-1]
    y = pro.daily(trade_date=yday).rename(columns={'pct_chg': 'prev_pct', 'amount': 'prev_amount'})
    y = y.merge(pro.stk_limit(trade_date=yday)[['ts_code', 'up_limit']], on='ts_code', how='left')
    y = y.merge(pro.daily_basic(trade_date=yday, fields='ts_code,circ_mv,turnover_rate_f,volume_ratio')
                [['ts_code', 'circ_mv', 'turnover_rate_f', 'volume_ratio']], on='ts_code', how='left')
    y['prev_amount'] = y.prev_amount * 1000
    y['prev_zt'] = (y.close >= y.up_limit - 1e-6).astype(int)
    y['circ_yi'] = y.circ_mv / 1e4
    hf = hist_features(yday)
    df = q.merge(y[['ts_code', 'prev_pct', 'prev_amount', 'prev_zt', 'circ_yi', 'turnover_rate_f']],
                 on='ts_code', how='left').merge(sb[['ts_code', 'name', 'industry', 'list_date']],
                                                 on='ts_code', how='left')
    if len(hf):
        df = df.merge(hf[['ts_code', 'zt20', 'hi60', 'ma20', 'ma5']], on='ts_code', how='left')
    else:
        df['zt20'], df['hi60'], df['ma20'] = 0, np.nan, np.nan
    df['dist_hi60'] = df.open / df.hi60 - 1
    df['dev_ma20'] = df.open / df.ma20 - 1
    df['listed_days'] = (pd.Timestamp.today() - pd.to_datetime(df.list_date, errors='coerce')).dt.days
    df = df[df.listed_days >= 60].fillna({'zt20': 0})
    df['hot'] = (df.gap >= 0.02).astype(int)
    df['ind_hot'] = df.groupby('industry')['hot'].transform('sum')
    limit_pct = np.where(df.ts_code.str[:3].isin(('300', '301')), 1.2, 1.1)
    df['up_limit_today'] = (df.pre_close * limit_pct).round(2)
    df['tradable'] = (df.up_limit_today - df.open) / df.pre_close > 0.003
    log(f'昨日交易日 = {yday}；今日开盘可买（距涨停>0.3%）{int(df.tradable.sum())} 只')
    tiers = [
        ('甲档 高开4~9%+流通20~150亿+20日内有涨停',
         df.gap.between(0.04, 0.09) & df.circ_yi.between(20, 150) & df.prev_pct.between(-5, 9)
         & (df.zt20 >= 1) & df.tradable & (df.prev_zt == 0)),
        ('乙档 高开2~7%+流通20~150亿+20日内有涨停',
         df.gap.between(0.02, 0.07) & df.circ_yi.between(20, 150) & (df.zt20 >= 1)
         & df.tradable & (df.prev_zt == 0)),
    ]
    zt = em_zt_pool(today) if len(df) else pd.DataFrame()
    zset = set(zt.code) if len(zt) else set()
    zidx = zt.set_index('code') if len(zt) else None
    cand_all = pd.DataFrame()
    for title, m in tiers:
        c = df[m].copy()
        if not len(c):
            log(f'\n[{title}] 今日无命中'); continue
        vals = [sina_first_min(x) for x in c.ts_code]
        c['fm_amt'] = [v[0] for v in vals]
        c['fm_wan'] = c.fm_amt / 1e4
        c['fm_ratio'] = c.fm_amt / c.prev_amount * 100
        c['fm_mv'] = c.fm_amt / 1e8 / c.circ_yi * 100
        c['score'] = (0.9 * (c.gap.clip(0, .09) / .09) + 0.9 * c.fm_ratio.rank(pct=True)
                      + 0.4 * c.ind_hot.rank(pct=True) - 0.4 * c.dist_hi60.abs().rank(pct=True))
        c = c.sort_values('score', ascending=False).head(top)
        c['档位'] = title[:2]
        cand_all = pd.concat([cand_all, c], ignore_index=True)
        v = c[['ts_code', 'name', 'industry', 'open', 'gap', 'prev_pct', 'zt20', 'circ_yi',
               'ind_hot', 'dist_hi60', 'fm_wan', 'fm_ratio', 'fm_mv', 'pct_now']].copy()
        v['gap'] = (v.gap * 100).round(2); v['dist_hi60'] = (v.dist_hi60 * 100).round(1)
        v['fm_ratio'] = v.fm_ratio.round(1); v['fm_mv'] = v.fm_mv.round(2)
        v['fm_wan'] = v.fm_wan.round(0); v['pct_now'] = (v.pct_now * 100).round(2)
        v['circ_yi'] = v.circ_yi.round(0); v['prev_pct'] = v.prev_pct.round(2)
        log(f'\n[{title}] 命中 {len(c)} 只：')
        log(v.rename(columns={'ts_code': '代码', 'name': '名称', 'industry': '行业', 'open': '竞价价',
                              'gap': '高开%', 'prev_pct': '昨涨%', 'zt20': '20日板', 'circ_yi': '流通亿',
                              'ind_hot': '行业高开', 'dist_hi60': '距60高%', 'fm_wan': '首分额万',
                              'fm_ratio': '首分/昨额%', 'fm_mv': '首分占流通%', 'pct_now': '现涨%'}
                     ).to_string(index=False))
    if len(zt) and len(cand_all):
        ca = cand_all.drop_duplicates('ts_code').copy()
        ca['封板'] = ca.ts_code.str[:6].isin(zset).map({True: '是', False: ''})
        ca['首封'] = [zidx.loc[x[:6], 'fbt'] if x[:6] in zset else '' for x in ca.ts_code]
        ca['连板'] = [zidx.loc[x[:6], 'lbc'] if x[:6] in zset else 0 for x in ca.ts_code]
        log(f'\n--- 盘中验证（{pd.Timestamp.now():%H:%M}，实时涨停池 {len(zt)} 家 / 全市场 {len(df)} 只 '
            f'= 基线 {len(zt)/len(df)*100:.2f}%）---')
        for tier, g in ca.groupby('档位'):
            log(f'{tier}：命中 {len(g)} 只，封板 {int((g.封板 == "是").sum())} 只'
                f'（{(g.封板=="是").mean()*100:.0f}%），现涨均值 {g.pct_now.mean()*100:.2f}%')
        v = ca[['档位', 'ts_code', 'name', 'gap', '封板', '首封', '连板', 'pct_now']].copy()
        v['gap'] = (v.gap * 100).round(2); v['pct_now'] = (v.pct_now * 100).round(2)
        log(v.to_string(index=False))
        ca.to_pickle(os.path.join(CACHE, f'cand_{today}.pkl'))
    if len(zt):
        log(f'\n今日涨停池 {len(zt)} 家（首封时间 / 连板数 / 行业）：')
        log(zt[['code', 'name', 'fbt', 'lbc', 'zbc', 'hybk']].to_string(index=False))


# ---------------- 回测 ----------------
def mode_backtest(start, end, cal_end, rule_rng):
    df = load_panel(start, end)
    log(f'面板 {len(df)} 行 / {df.trade_date.nunique()} 天 / {df.ts_code.nunique()} 只')
    days = df.trade_date.nunique()

    base = df[(df.prev_zt == 0) & df.tradable]
    rows = []
    for lo, hi in [(-0.05, 0), (0, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05),
                   (0.05, 0.07), (0.07, 0.09), (0.09, 0.20)]:
        s = base[(base.gap >= lo) & (base.gap < hi)]
        if len(s):
            toc = s[s.is_touch == 1]
            rows.append(dict(竞价高开=f'{lo*100:.0f}~{hi*100:.0f}%', 样本=len(s),
                             封板率=round(s.is_zt.mean() * 100, 2),
                             触板率=round(s.is_touch.mean() * 100, 1),
                             竞价买入日内=round(s.r_open_close.mean(), 2),
                             打板T1期望=round(pd.Series(np.where(toc.is_zt == 1, toc.r_daban_next,
                                                              toc.r_daban_hold)).mean(), 2)))
    show(rows, '① 单因子：竞价高开幅度（昨日未涨停 + 开盘可买；三种收益口径分开看）')

    first = df[(df.is_zt == 1) & (df.prev_zt == 0)]
    first_h = hold_to_break(df, first)
    gl = df.gap
    rows = [ev(df[(df.prev_zt == 0) & df.tradable], days, '对照：全市场昨日未涨停')]
    for label, m in [
            ('裸筛：高开2~7%', gl.between(0.02, 0.07)),
            ('+ 流通20~150亿', gl.between(0.02, 0.07) & df.circ_yi.between(20, 150)),
            ('+ 昨涨-3~7%', gl.between(0.02, 0.07) & df.circ_yi.between(20, 150) & df.prev_pct.between(-3, 7)),
            ('+ 20日内有涨停（打板基因）', gl.between(0.02, 0.07) & df.circ_yi.between(20, 150) & (df.zt20 >= 1)),
            ('甲档 高开4~9%+流通20~150亿+20日内有涨停',
             gl.between(0.04, 0.09) & df.circ_yi.between(20, 150) & (df.zt20 >= 1)),
            ('甲档+昨涨-5~9%', gl.between(0.04, 0.09) & df.circ_yi.between(20, 150) & (df.zt20 >= 1)
             & df.prev_pct.between(-5, 9))]:
        rows.append(ev(df[m & (df.prev_zt == 0) & df.tradable], days, label))
    show(rows, '② 规则横向对比（打板口径=触板才成交；持有到断板=首板涨停价买入、持有到断板日收盘）')

    rows = []
    for label, m in [('全部首板（基线）', pd.Series(True, index=df.index)),
                     ('高开2~7%', gl.between(0.02, 0.07)),
                     ('高开2~7%+流通20~150亿+20日内有涨停',
                      gl.between(0.02, 0.07) & df.circ_yi.between(20, 150) & (df.zt20 >= 1)),
                     ('高开4~9%+流通20~150亿+20日内有涨停',
                      gl.between(0.04, 0.09) & df.circ_yi.between(20, 150) & (df.zt20 >= 1))]:
        s = first_h[m.reindex(first_h.index).fillna(False)]
        if len(s):
            h = s.dropna(subset=['hold_ret'])
            rows.append(dict(规则=label, 首板笔数=len(s), 日均=round(len(s) / days, 2),
                             一进二=round((s.cont >= 1).mean() * 100, 1),
                             二进三=round((s.cont >= 2).mean() * 100, 1),
                             持有到断板均收益=round(h.hold_ret.mean(), 2),
                             中位=round(h.hold_ret.median(), 2),
                             正收益占比=round((h.hold_ret > 0).mean() * 100, 1)))
    show(rows, '③ 首板之后：一进二晋级率 与「以首板涨停价买入→持有到断板」')

    cal, oos = df[df.trade_date <= cal_end], df[df.trade_date > cal_end]
    m = lambda d: (d.gap.between(*rule_rng) & d.circ_yi.between(20, 150) & (d.zt20 >= 1)
                   & (d.prev_zt == 0) & d.tradable)
    rows = [ev(df[m(df)], days, '全区间'),
            ev(cal[m(cal)], cal.trade_date.nunique(), f'校准段 ~{cal_end}'),
            ev(oos[m(oos)], oos.trade_date.nunique(), f'样本外 {cal_end}~')]
    show(rows, f'④ 稳定性：甲档规则（高开{rule_rng[0]*100:.0f}~{rule_rng[1]*100:.0f}%）')

    sel_h = hold_to_break(df, df[m(df) & (df.is_zt == 1)])
    for key in ['gap', 'circ_yi', 'zt20']:
        sel_h['b'] = pd.qcut(sel_h[key].rank(method='first'), 4)
        rows = []
        for b, s in sel_h.groupby('b'):
            h = s.dropna(subset=['hold_ret'])
            rows.append(dict(分组=f'{s[key].min():.3g}~{s[key].max():.3g}', 样本=len(s),
                             一进二=round((s.cont >= 1).mean() * 100, 1),
                             持有到断板=round(h.hold_ret.mean(), 2) if len(h) else np.nan,
                             竞价买入日内=round(s.r_open_close.mean(), 2)))
        show(rows, f'⑤ 甲档内按 {key} 四分位')
    sel_h.sort_values('trade_date', ascending=False).to_pickle(os.path.join(CACHE, 'selected_backtest.pkl'))
    log('\n明细已存 ~/.hermes/cache/firstboard/selected_backtest.pkl')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', default='backtest', choices=['backtest', 'today'])
    ap.add_argument('--start', default='20260401')
    ap.add_argument('--end', default='20261021')
    ap.add_argument('--cal-end', default='20260831')
    ap.add_argument('--top', type=int, default=18)
    ap.add_argument('--gap-min', type=float, default=0.04)
    ap.add_argument('--gap-max', type=float, default=0.09)
    a = ap.parse_args()
    if a.mode == 'backtest':
        mode_backtest(a.start, a.end, a.cal_end, (a.gap_min, a.gap_max))
    else:
        mode_today(a.top)


if __name__ == '__main__':
    main()
