#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""盘前（9:25 集合竞价后）龙头战法扫描：昨日完整情绪+梯队+席位 / 今日竞价强度。

用法:
  python3 premarket_dragon_scan.py                       # 正常：昨日完整数据 + 今日竞价
  python3 premarket_dragon_scan.py --no-realtime         # 只出昨日数据（调试/非盘中）
  python3 premarket_dragon_scan.py --prev 20261019       # 指定"昨日"
  python3 premarket_dragon_scan.py --extra 603893.SH,603636.SH
数据源：昨日盘后一律 Tushare Pro 接口；今日实时用 Tushare 爬虫接口 ts.realtime_quote（批量、秒级、无限频），
失败回落东财 push2。不用 rt_min（5次/分钟+10次/小时）与 stk_mins（1次/小时）。
"""
import os, sys
# cron 调用方可能用 PATH 里的 python3（pyenv shim 无 pandas），先自举到带依赖的解释器
if sys.executable != '/usr/local/bin/python3':
    try:
        import pandas  # noqa: F401
    except ImportError:
        os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3'] + sys.argv)
import re, json, time, argparse, datetime, warnings, urllib.request
warnings.filterwarnings('ignore')
import pandas as pd
import tushare as ts

TOK = os.environ.get('TUSHARE_TOKEN')
if not TOK:
    for line in open(os.path.expanduser('~/.hermes/.env')):
        if line.startswith('TUSHARE_TOKEN'):
            TOK = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
pro = ts.pro_api(TOK)


def y(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else x


def yi(v):  # 元 -> 亿
    try:
        return float(v) / 1e8
    except Exception:
        return float('nan')


def wan(v):
    try:
        return float(v) / 1e4
    except Exception:
        return float('nan')


def norm(s):
    s = re.sub(r'[（）()\s]', '', str(s))
    for k in ('证券股份有限公司', '证券营业部', '有限公司', '营业部', '分公司', '证券'):
        s = s.replace(k, '')
    return s


def seat_map():
    m = {}
    try:
        hm = pro.hm_list(fields='name,orgs')
        for _, r in hm.dropna(subset=['orgs']).iterrows():
            for o in str(r.orgs).replace('，', ',').split(','):
                m[norm(o)] = r['name']
    except Exception:
        pass
    return m


def limit_pct(ts_code):
    c = str(ts_code)
    if c.endswith('.BJ'):
        return 0.30
    if c.startswith(('300', '301', '688', '689')):
        return 0.20
    return 0.10


# ---------------- 实时快照（爬虫接口，批量秒级；不碰 rt_min / stk_mins） ----------------
def _rt_tushare(codes, chunk=600):
    ts.set_token(TOK)
    frames = []
    for i in range(0, len(codes), chunk):
        try:
            d = ts.realtime_quote(ts_code=','.join(codes[i:i + chunk]))
        except Exception:
            continue
        if d is not None and len(d):
            frames.append(d)
    if not frames:
        return None
    d = pd.concat(frames, ignore_index=True)
    bid = pd.to_numeric(d['B1_V'], errors='coerce') if 'B1_V' in d.columns else float('nan')
    return pd.DataFrame({
        'code': d['TS_CODE'].astype(str),
        'name': d['NAME'].astype(str),
        'price': pd.to_numeric(d['PRICE'], errors='coerce'),
        'pre': pd.to_numeric(d['PRE_CLOSE'], errors='coerce'),
        'open': pd.to_numeric(d['OPEN'], errors='coerce'),
        'high': pd.to_numeric(d['HIGH'], errors='coerce'),
        'low': pd.to_numeric(d['LOW'], errors='coerce'),
        'vol_hand': pd.to_numeric(d['VOLUME'], errors='coerce') / 100.0,   # 股 -> 手
        'amount': pd.to_numeric(d['AMOUNT'], errors='coerce'),             # 元
        'bid1': bid,                                                       # 买一挂单(手)
        'time': d['TIME'].astype(str),
    })


def _rt_em(codes, chunk=80):
    rows = []
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        secids = ','.join(('1.' if c.endswith('.SH') else '0.') + c[:6] for c in part)
        url = ('https://push2.eastmoney.com/api/qt/ulist.np/get?ut=fa5fd1943c7b386f172d6893dbfba10b'
               '&fltt=2&fields=f2,f3,f5,f6,f12,f13,f14,f15,f16,f17,f18&secids=' + secids)
        try:
            req = urllib.request.Request(url, headers={'Referer': 'https://quote.eastmoney.com/',
                                                       'User-Agent': 'Mozilla/5.0'})
            js = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
        except Exception:
            continue
        for it in ((js.get('data') or {}).get('diff') or []):
            rows.append({
                'code': ('%s.SH' if it.get('f13') == 1 else '%s.SZ') % it.get('f12'),
                'name': it.get('f14'), 'price': it.get('f2'), 'pre': it.get('f18'),
                'open': it.get('f17'), 'high': it.get('f15'), 'low': it.get('f16'),
                'vol_hand': it.get('f5'), 'amount': it.get('f6'),
                'bid1': float('nan'), 'time': datetime.datetime.now().strftime('%H:%M:%S')})
    return pd.DataFrame(rows) if rows else None


def fetch_quotes(codes, need_time=True):
    """优先 Tushare 爬虫接口 ts.realtime_quote（批量、秒级、无限频），失败回落东财 push2。"""
    out, src = None, None
    for nm, fn in (('Tushare爬虫', _rt_tushare), ('东财push2', _rt_em)):
        try:
            r = fn(codes)
        except Exception:
            r = None
        if r is not None and len(r):
            out, src = r, nm
            break
    if out is None or not len(out):
        return None
    out = out.drop_duplicates(subset=['code'])
    if need_time:      # 9:25 刚过时快照可能还停在 09:24，等到跨过 09:25
        deadline = time.time() + 40
        while time.time() < deadline:
            tmax = str(out['time'].max())
            if tmax >= '09:25' or tmax < '09:00':
                break
            time.sleep(3)
            try:
                r = _rt_tushare(codes)
                if r is not None and len(r):
                    out = r.drop_duplicates(subset=['code'])
            except Exception:
                pass
    out.attrs['src'] = src
    out.attrs['ts'] = str(out['time'].max())
    return out


def trading_days(start, end):
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
    return sorted(cal.cal_date.tolist())


def last_prev_day(today):
    """返回 (是否交易日, 上一个交易日)"""
    start = (datetime.datetime.strptime(today, '%Y%m%d') - datetime.timedelta(days=20)).strftime('%Y%m%d')
    days = trading_days(start, today)
    if not days or days[-1] != today:
        return False, (days[-1] if days else None)
    return True, (days[-2] if len(days) > 1 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prev', default=None)
    ap.add_argument('--no-realtime', action='store_true')
    ap.add_argument('--extra', default='603636.SH,603893.SH')  # 默认盯：南威软件(吉大唯一涨停小弟)、瑞芯微(端侧AI人气核心)
    ap.add_argument('--max-wait', type=int, default=210)
    ap.add_argument('--market', dest='market', action='store_true', default=True)
    ap.add_argument('--no-market', dest='market', action='store_false')
    a = ap.parse_args()

    today = datetime.datetime.now().strftime('%Y%m%d')
    is_td, prev = last_prev_day(today)
    if a.prev:
        prev, is_td = a.prev, True

    out = []
    p = out.append
    p(f"【盘前龙头扫描】{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  今日={today}  昨日口径={prev}")
    if not is_td:
        p("今天不是 A 股交易日：停止，不做任何推演。")
        print('\n'.join(out)); return

    hm = seat_map()

    # ---------- 昨日涨停池 ----------
    try:
        U = pro.limit_list_d(trade_date=prev, limit_type='U')
    except Exception as e:
        U = pd.DataFrame()
        p(f"[缺口] limit_list_d 取数失败: {e}")
    try:
        Z = pro.limit_list_d(trade_date=prev, limit_type='Z')
    except Exception:
        Z = pd.DataFrame()
    try:
        K = pro.kpl_list(trade_date=prev, tag='涨停')
    except Exception:
        K = pd.DataFrame()
    try:
        AL = pro.daily(trade_date=prev)
    except Exception:
        AL = pd.DataFrame()
    try:
        BS = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,market').set_index('ts_code')
    except Exception:
        BS = pd.DataFrame()
    try:
        DB = pro.daily_basic(trade_date=prev, fields='ts_code,turnover_rate,volume_ratio,circ_mv,free_share')
    except Exception:
        DB = pd.DataFrame()

    if U.empty and AL.empty:
        p(f"昨日({prev}) 涨停池与日线均为空：数据未发布，无法推演。")
        print('\n'.join(out)); return

    # 炸板率/跌停/大面（daily 口径）
    if not AL.empty:
        sl = pro.stk_limit(trade_date=prev)
        m = AL.merge(sl[['ts_code', 'up_limit', 'down_limit']], on='ts_code', how='left')
        m['up'] = m.close >= m.up_limit - 0.001
        m['touch'] = m.high >= m.up_limit - 0.001
        m['dn'] = m.close <= m.down_limit + 0.001
        nu, nz = int(m.up.sum()), int((m.touch & ~m.up).sum())
        p(f"昨日情绪: 涨停 {nu} / 炸板 {nz} (炸板率 {nz/max(nu+nz,1)*100:.0f}%) / 跌停 {int(m.dn.sum())} / "
          f"大面 {int((m.pct_chg<=-8).sum())} / 全市场均涨 {m.pct_chg.mean():+.2f}%")

    # ---------- 板数与梯队 ----------
    if not U.empty:
        U = U.drop_duplicates(subset=['ts_code'])
        U['name'] = U.ts_code.map(BS.name) if len(BS) else U.get('name')
        U['ind'] = U.ts_code.map(BS.industry) if len(BS) else U.get('industry')
        U['board'] = pd.to_numeric(U.limit_times, errors='coerce').fillna(1).astype(int)
        if not K.empty:
            kk = K.drop_duplicates(subset=['ts_code']).set_index('ts_code')
            for src, dst in (('lu_time', '首封'), ('open_time', '末封'), ('status', '几天几板'), ('theme', '题材'),
                             ('lu_limit_order', '封单亿'), ('turnover_rate', '换手kpl')):
                U[dst] = U.ts_code.map(kk[src])
            U['封单亿'] = pd.to_numeric(U['封单亿'], errors='coerce').apply(yi)
        U['成交亿'] = U.amount.apply(yi)
        p(f"连板家数 {int((U.board>=2).sum())} 家，最高板 {int(U.board.max())} 板"
          f"（{U.loc[U.board.idxmax(),'name']}）")

        ladder = U[U.board >= 2].sort_values(['board', 'amount'], ascending=[False, False])
        p("\n== 梯队(>=2板)：板/首封/封单/成交/换手/流通/题材 ==")
        for _, r in ladder.iterrows():
            rev = ''
            try:
                tl = pro.top_list(ts_code=r.ts_code, trade_date=prev)
                if len(tl):
                    rev = f" 龙虎净{wan(tl.net_amount.sum()):+.0f}万"
                    ti = pro.top_inst(ts_code=r.ts_code, trade_date=prev).drop_duplicates(subset=['exalter', 'side', 'reason'])
                    if len(ti):
                        ti['nb'] = ((pd.to_numeric(ti.buy, errors='coerce').fillna(0)
                                     - pd.to_numeric(ti.sell, errors='coerce').fillna(0)) / 1e4)
                        top = ti.sort_values('nb', ascending=False).head(3)
                        seg = []
                        for _, s2 in top.iterrows():
                            who = hm.get(norm(s2.exalter), '')
                            label = who or str(s2.exalter).replace('证券股份有限公司', '').replace('证券营业部', '')[:12]
                            seg.append(f"{label}{s2.nb:+.0f}万")
                        rev += " | " + '；'.join(seg)
            except Exception:
                pass
            tr = db = ''
            if len(DB):
                row = DB[DB.ts_code == r.ts_code]
                if len(row):
                    tr = f"{row.iloc[0].turnover_rate:.1f}%"
                    db = f"{row.iloc[0].circ_mv/1e4:.0f}亿"
            feng = r.get('封单亿')
            fengs = f"{feng:.2f}亿" if isinstance(feng, float) and feng == feng else "n/a"
            ktr = pd.to_numeric(r.get('换手kpl'), errors='coerce')
            form = '一字' if str(r.get('首封', '')).startswith('09:2') and (ktr != ktr or ktr < 8) else '换手板'
            p(f"{int(r.board)}板 {r.ts_code} {r['name']} [{r.get('ind','')}] {form} 首封{r.get('首封','?')} "
              f"{r.get('几天几板','')} 封单{fengs} 成交{r['成交亿']:.2f}亿 换手{tr} 流通{db} 题材:{r.get('题材','')}{rev}")

        hi = U.board.max()
        hirow = U[U.board == hi]
        if not K.empty:
            kk = K.drop_duplicates(subset=['ts_code'])
            theme = str(hirow.iloc[0].get('题材', '') or '')
            key = [t for t in re.split(r'[、,，]', theme) if t]
            if key and len(kk):
                fol = kk[kk.theme.apply(lambda s: any(t in str(s) for t in key))]
                fol = fol[~fol.ts_code.isin(U[U.board >= 2].ts_code)]
                if len(fol):
                    p(f"\n== 最高板同题材跟风（{theme}，昨日涨停的首板）==")
                    p(' ; '.join(f"{r.ts_code}{r['name']}({r.get('lu_time','')})" for _, r in fol.iterrows()))
            # 端侧AI/机器人 这类主线广度
            for th in ('端侧AI', '机器人', 'AI安全', '低空'):
                sub = kk[kk.theme.apply(lambda s: th in str(s))]
                if len(sub) >= 3:
                    p(f"主线广度·{th}: 涨停 {len(sub)} 家")

    if not Z.empty:
        Z = Z.drop_duplicates(subset=['ts_code']).copy()
        Z['name'] = Z.ts_code.map(BS.name) if len(BS) else Z.get('name')
        Z['amt'] = pd.to_numeric(Z.amount, errors='coerce')
        ztop = Z.sort_values('amt', ascending=False).head(8)
        p("\n== 昨日炸板(触板未封)前8：明日弱转强观察 ==")
        p(' ; '.join(f"{r.ts_code}{r['name']}({yi(r.amt):.1f}亿)" for _, r in ztop.iterrows()))

    # ---------- 今日 09:25 竞价：Tushare 爬虫接口（批量、秒级、无限频） ----------
    pool = []
    if not U.empty:
        L = U[U.board >= 2].sort_values(['board', 'amount'], ascending=[False, False])
        pool = [(r.ts_code, r['name'], int(r.board)) for _, r in L.iterrows()]
    for c in [x.strip() for x in a.extra.split(',') if x.strip()]:
        if c not in [q[0] for q in pool]:
            pool.append((c, (BS.name.get(c, '') if len(BS) else ''), 0))

    idx_codes = ['000001.SH', '399001.SZ', '399006.SZ']
    watch = list(dict.fromkeys([c for c, _, _ in pool] +
                               (U.ts_code.tolist() if not U.empty else []) +
                               (Z.ts_code.tolist() if not Z.empty else []) + idx_codes))

    p("\n== 今日 09:25 竞价（Tushare 爬虫快照）==")
    if a.no_realtime:
        p("（--no-realtime 已跳过）")
        print('\n'.join(out)); return

    q = fetch_quotes(watch, need_time=True)
    if q is None:
        p("[缺口] 竞价快照取数失败（爬虫与东财兜底都不可用）。")
        print('\n'.join(out)); return
    p("数据源 %s；快照时间 %s；覆盖 %d 只" % (q.attrs.get('src', '?'), q.attrs.get('ts', '?'), len(q)))
    qi = q.set_index('code')

    for c in idx_codes:
        if c in qi.index:
            r = qi.loc[c]
            p("指数 %s %.2f %+.2f%%" % (r['name'], r['price'], (r['price'] / r['pre'] - 1) * 100))

    prev_vol = dict(zip(AL.ts_code, AL.vol))
    p("\n-- 候选（昨日连板≥2 + 关注票）--")
    rows = []
    for code, name, bd in pool:
        if code not in qi.index:
            rows.append((bd, "%s %s 竞价未取到" % (name or '', code))); continue
        r = qi.loc[code]
        pre = float(r['pre']) if r['pre'] else None
        if not pre:
            rows.append((bd, "%s %s 无昨收" % (name, code))); continue
        px = float(r['price'])
        if not px or px != px:      # 竞价无成交(停牌/无撮合)
            rows.append((bd, "%s %s 竞价无成交价" % (name, code))); continue
        pct = (px / pre - 1) * 100
        lim = round(pre * (1 + limit_pct(code)), 2)
        one = abs(px - lim) < 0.011
        flag = '涨停开/一字' if one else ('高开' if pct >= 3 else ('平开' if pct > -1 else '低开'))
        vp = (float(r['vol_hand']) / prev_vol[code] * 100) if prev_vol.get(code) else float('nan')
        bid = r.get('bid1')
        feng = (float(bid) * px * 100 / 1e8) if bid == bid and bid else float('nan')
        tag = ("%d板 %s %s" % (bd, name, code)) if bd else ("%s %s" % (name or '', code))
        rows.append((bd, "%s 竞价 %.2f %+.2f%% %s 竞价额%.2f亿(占昨全天%.1f%%) 买一挂单%.2f亿"
                     % (tag, px, pct, flag, float(r['amount']) / 1e8, vp, feng)))
    for _, line in sorted(rows, key=lambda x: -x[0]):
        p(line)

    # ---------- 一进二候选（昨日首板 + 今日竞价），按 A/B/C 优先级排序 ----------
    if not U.empty:
        U2 = U.copy()
        U2['lt'] = pd.to_numeric(U2['limit_times'], errors='coerce').fillna(0).astype(int)
        ind_cnt = U2.groupby('industry')['ts_code'].count().to_dict()
        first = U2[U2['lt'] == 1]
        cand2 = []
        for _, r in first.iterrows():
            code = r['ts_code']
            if code not in qi.index:
                continue
            q0 = qi.loc[code]
            pre = float(q0['pre']) if q0['pre'] else 0.0
            px = float(q0['price']) if q0['price'] == q0['price'] else 0.0
            if pre <= 0 or px <= 0:
                continue
            lim = round(pre * (1 + limit_pct(code)), 2)
            if abs(px - lim) < 0.011:          # 一字/涨停开：买不到，不进候选
                continue
            gap = (px / pre - 1) * 100
            amt = (r['amount'] or 0) / 1e8
            fd = (r['fd_amount'] / r['amount'] * 100) if r['amount'] else 999
            ind = ind_cnt.get(r['industry'], 0)
            vp = (float(q0['amount']) / r['amount'] * 100) if r['amount'] else float('nan')
            ft = int(str(r['first_time']).zfill(6)) if str(r['first_time']).isdigit() else 235959
            tover = float(r['turnover_ratio'] or 0)
            sc, why = 0, []
            if amt >= 5:
                sc += 2; why.append('额%.1f亿' % amt)
            elif amt >= 2:
                sc += 1; why.append('额%.1f亿' % amt)
            else:
                sc -= 2; why.append('额仅%.1f亿' % amt)
            if ind >= 5:
                sc += 2; why.append('同行业涨停%d家' % ind)
            elif ind >= 3:
                sc += 1; why.append('同行业涨停%d家' % ind)
            else:
                why.append('板块仅%d家' % ind)
            if vp == vp:
                if vp >= 15:
                    sc += 2; why.append('竞价占昨全天%.0f%%' % vp)
                elif vp >= 8:
                    sc += 1; why.append('竞价占昨全天%.0f%%' % vp)
                else:
                    why.append('竞价缩量%.0f%%' % vp)
            if 0 <= gap <= 6:
                sc += 1; why.append('高开%+.1f%%' % gap)
            elif gap > 7:
                sc -= 3; why.append('高开%+.1f%%＞7%%不追' % gap)
            elif gap > 6:
                sc -= 1; why.append('高开%+.1f%%偏高' % gap)
            else:
                sc -= 2; why.append('高开%+.1f%%低开不做' % gap)
            if fd < 10 and tover >= 10:
                sc += 1; why.append('薄封单+换手%.0f%%' % tover)
            if ft <= 103000:
                sc += 1; why.append('首封%02d:%02d' % (ft // 10000, ft % 10000 // 100))
            tier = 'A' if sc >= 6 else ('B' if sc >= 4 else 'C')
            cand2.append((tier, sc, code, r['name'], gap, amt, fd, tover, ind, vp, why))
        if cand2:
            p("\n-- 一进二候选（昨日首板 ∩ 今日非一字，按优先级排序）--")
            ranked = sorted(cand2, key=lambda x: (-x[1], -(x[9] if x[9] == x[9] else 0)))
            for tier, sc, code, name, gap, amt, fd, tover, ind, vp, why in ranked[:6]:
                p("[%s %d分] %s %s 高开%+.1f%%｜首板额%.2f亿 封成比%.1f%% 换手%.1f%%｜%s"
                  % (tier, sc, name, code, gap, amt, fd, tover, ' / '.join(why)))
            nA = sum(1 for c in cand2 if c[0] == 'A'); nB = sum(1 for c in cand2 if c[0] == 'B')
            p("合计：A 档 %d 只（可买，需 9:35 量能二次确认：开盘5分钟成交 ≥ 昨日全天8%%）｜B 档 %d 只（半仓/观察）｜其余 C 档不做" % (nA, nB))

    # 板块竞价强度：昨日涨停股按题材分组，看今日竞价中位
    if not K.empty and not U.empty:
        kb = K.drop_duplicates(subset=['ts_code']).set_index('ts_code')
        recs = []
        for code in U.ts_code.tolist():
            if code not in qi.index or code not in kb.index:
                continue
            pre = qi.loc[code, 'pre']; px = qi.loc[code, 'price']
            if not pre or not px or px != px:
                continue
            recs.append({'theme': str(kb.loc[code, 'theme']), 'pct': (px / pre - 1) * 100})
        if recs:
            df = pd.DataFrame(recs)
            agg = []
            for th in ('端侧AI', '机器人', 'AI安全', '低空', '军工', '算力', '飞行汽车', '物理AI'):
                sub = df[df.theme.apply(lambda s: th in s)]
                if len(sub) >= 2:
                    agg.append((th, len(sub), sub.pct.median(), int((sub.pct > 0).sum())))
            if agg:
                p("\n-- 板块竞价强度（昨日涨停股今日竞价中位）--")
                for th, n, med, red in sorted(agg, key=lambda x: -x[2]):
                    p("%s: n=%d 中位%+.2f%% 红盘%d" % (th, n, med, red))

    if not Z.empty:
        zs = []
        for code in Z.ts_code.tolist():
            if code in qi.index:
                pre = qi.loc[code, 'pre']; px = qi.loc[code, 'price']
                if pre and px and px == px:
                    zs.append((code, qi.loc[code, 'name'], (px / pre - 1) * 100))
        if zs:
            zs.sort(key=lambda x: -x[2])
            p("\n-- 昨日炸板票竞价（弱转强观察，前8）--")
            p(' ; '.join("%s %s %+.2f%%" % (n, c, v) for c, n, v in zs[:8]))

    # 全市场竞价分布（可选）
    if a.market and len(BS):
        allq = fetch_quotes(BS.index.tolist(), need_time=False)
        if allq is not None:
            allq = allq.dropna(subset=['price', 'pre'])
            allq = allq[allq.pre > 0]
            allq['pct'] = (allq.price / allq.pre - 1) * 100
            n = len(allq)
            p("\n-- 全市场竞价（%d 只）--" % n)
            p("红盘 %d 家（%.0f%%）/ 高开≥3%% %d 家 / 跌幅≤-3%% %d 家 / 中位 %+.2f%%"
              % (int((allq.pct > 0).sum()), (allq.pct > 0).mean() * 100,
                 int((allq.pct >= 3).sum()), int((allq.pct <= -3).sum()), allq.pct.median()))
            top = allq.sort_values('pct', ascending=False).head(12)
            p("竞价涨幅前12: " + ' ; '.join("%s(%s) %+.1f%%" % (r['name'], r['code'], r.pct) for _, r in top.iterrows()))

    # ---------- 决策摘要：今天做哪种打法 + 两套各自首选 ----------
    try:
        pol, pol_reason = [], []
        lu_n = int(len(U)) if not U.empty else 0
        maxb = int(pd.to_numeric(U['board'], errors='coerce').fillna(0).max()) if not U.empty else 0
        red_pct = float((allq.pct > 0).mean() * 100) if ('allq' in dir() and allq is not None and len(allq)) else float('nan')
        prev2 = pro.limit_list_d(trade_date=trading_days('20260101', prev)[-2] if False else pro.trade_cal(exchange='SSE', start_date='20260601', end_date=prev, is_open='1')['cal_date'].tolist()[-2], limit_type='U')
        promo = (len(set(U['ts_code']) & set(prev2['ts_code'])) / max(len(prev2), 1) * 100) if (not U.empty and prev2 is not None and len(prev2)) else float('nan')
    except Exception:
        lu_n, maxb, red_pct, promo = 0, 0, float('nan'), float('nan')

    relay_ok = (lu_n >= 60) and (maxb >= 6) and (red_pct == red_pct and red_pct >= 40)
    yi_ok = (lu_n < 60) or (promo == promo and promo < 15)
    if relay_ok:
        play = '高位接力'
    elif yi_ok:
        play = '一进二'
    else:
        play = '空仓'
    p("\n== 决策摘要（今天做哪种打法）==")
    p("周期三数：昨日涨停 %d 家 / 最高板 %d 板 / 昨日涨停池今日竞价红盘 %.0f%% / 晋级率 %s"
      % (lu_n, maxb, red_pct if red_pct == red_pct else -1,
         ('%.1f%%' % promo) if promo == promo else '未取到'))
    p("高位接力条件（涨停≥60 且 最高板≥6 且 红盘率≥40%%）：%s" % ('满足' if relay_ok else '不满足'))
    p("一进二闸门（涨停<60 或 晋级率<15%%）：%s" % ('满足' if yi_ok else '不满足'))
    p("→ 今天适合：【%s】%s" % (play, '' if play != '空仓' else '（无高标也无合适首板环境，不开新仓）'))

    # 龙头战法首选：昨日 ≥3 板里板数最高，今日非一字，取成交额最大
    p("\n-- 龙头战法首选（昨日连板 ≥3）--")
    relay = []
    if not U.empty:
        for _, r in U[pd.to_numeric(U['board'], errors='coerce').fillna(0) >= 3].sort_values(
                ['board', 'amount'], ascending=[False, False]).iterrows():
            code = r['ts_code']
            if code not in qi.index:
                continue
            x = qi.loc[code]
            pre = float(x['pre']) if x['pre'] else 0
            px = float(x['price']) if x['price'] == x['price'] else 0
            if pre <= 0 or px <= 0:
                continue
            lim = round(pre * (1 + limit_pct(code)), 2)
            one = abs(px - lim) < 0.011
            relay.append((int(r['board']), code, r['name'], (px / pre - 1) * 100, one,
                          (r['amount'] or 0) / 1e8, r.get('industry', '')))
        if relay:
            b, code, name, gap, one, amt, ind = relay[0]
            # 位置：距 MA10 偏离（>+40% 属温度计型高风险，只减不加）
            pos_txt = ''
            try:
                h = pro.daily(ts_code=code, start_date=trading_days('20260101', prev)[-25], end_date=prev,
                              fields='trade_date,close')
                if h is not None and len(h) >= 10:
                    ma10 = float(h['close'].head(10).mean())
                    dev = (float(h['close'].iloc[0]) / ma10 - 1) * 100
                    pos_txt = '｜距 MA10 %+.1f%%%s' % (dev, '（>40% 温度计型，只减不加）' if dev > 40 else '')
            except Exception:
                pass
            p("首选：[%d 板] %s %s｜竞价 %+.1f%%%s｜昨日成交 %.2f 亿｜题材 %s%s"
              % (b, name, code, gap, '（一字/涨停开，买不到，只观察开板回封）' if one else '', amt, ind, pos_txt))
            for b2, c2, n2, g2, o2, a2, i2 in relay[1:4]:
                p("备选：[%d 板] %s %s｜竞价 %+.1f%%%s｜成交 %.2f 亿"
                  % (b2, n2, c2, g2, '（一字）' if o2 else '', a2))
        else:
            p("昨日无 ≥3 板标的 → 龙头战法今天没有接力对象")
    # 一进二首选：已算好的 cand2 第 1 名
    p("\n-- 一进二首选（昨日首板，静态分最高）--")
    if 'cand2' in dir() and cand2:
        ranked2 = sorted(cand2, key=lambda x: (-x[1], -(x[9] if x[9] == x[9] else 0)))
        t, sc, code, name, gap, amt, fd, tover, ind, vp, why = ranked2[0]
        p("首选：[%s 档 %d 分] %s %s｜高开 %+.1f%%｜首板 %.2f 亿｜封成比 %.1f%%｜换手 %.1f%%｜同行业涨停 %d 家"
          % (t, sc, name, code, gap, amt, fd, tover, ind))
        p("理由：%s" % ' / '.join(why))
        if len(ranked2) > 1:
            t2, sc2, c3, n3, g3 = ranked2[1][0], ranked2[1][1], ranked2[1][2], ranked2[1][3], ranked2[1][4]
            p("次选：[%s 档 %d 分] %s %s｜高开 %+.1f%%" % (t2, sc2, n3, c3, g3))
        p("执行门槛：9:35 开盘 5 分钟成交 ≥ 首板全天 8%%，未过不买")
    else:
        p("昨日首板无合格候选（或全为一字）")

    p("\n【口径】昨日涨停/板数/封单/首封/席位 = Tushare 完整口径；今日=09:25 成交竞价快照"
      "（爬虫接口，非 rt_min，无 5次/分钟 限频）；未取到的不做假设。")
    print('\n'.join(out))


if __name__ == '__main__':
    main()
