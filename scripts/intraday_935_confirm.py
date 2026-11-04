#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""交易日 09:35 一进二量能确认 + 综合判断（配合 9:25 静态排序）。

做什么
------
1. 读 9:25 落盘的一进二排行榜 rank_925_YYYYMMDD.pkl（静态分 / 档位 / 理由 / 硬门槛剔除都在里面），
   不再自己重算静态分；找不到榜单才回退到本脚本的旧公式（满分 6，档位与 9:25 卡片不可比，会告警）
2. 09:35 快照：现价（是否上板）、开盘价（复核一字）、累计成交额
   **09:35 仍处于涨停（现价 ≥ 涨停−0.011）的票整只剔除** —— 买点是 9:25 竞价价/开盘价，
   已封板时这个买点已经消失，留着只会把人引到禁止的「涨停价排板」（sean_yang 2026-11-04 定）。
3. 量能 =（09:35 累计成交额 − 集合竞价成交额）÷ 首板全天成交额 = 纯 09:30–09:35 连续竞价（sean_yang 2026-11-04 定：
   用不含竞价口径）。含竞价的「累计 ÷ 首板全天」只随行显示，不进门槛。
   竞价额基线缺失时才退回含竞价累计额并告警。
   量能分：≥10% → +2；8–10% → +1；<8% → −2
4. 综合分 = 9:25 静态分 + 量能分；A 档 = 综合 ≥8 且量能 ≥10%（量能不过 10% 不买，没有「先买一半」）；B 5–7；其余 C

用法
----
python3 intraday_935_confirm.py            # 正常（09:35 后跑）
python3 intraday_935_confirm.py --prev 20261102
python3 intraday_935_confirm.py --no-rank  # 强制走回退（测试旧公式路径）
"""
import os
import sys
import argparse
import datetime

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from premarket_dragon_scan import pro, fetch_quotes, limit_pct, last_prev_day, fetch_auction, load_limit_pool  # noqa: E402
import pandas as pd  # noqa: E402
import warnings  # noqa: E402

warnings.filterwarnings('ignore')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prev', default=None)
    ap.add_argument('--min-amt', type=float, default=10.0, help='量能门槛 %%（(09:35 累计 − 竞价额) / 首板全天，默认 10%%≈量能比1.5）')
    ap.add_argument('--no-rank', action='store_true', help='不读 9:25 排行榜，强制用旧公式重算（测试用）')
    a = ap.parse_args()

    today = datetime.datetime.now().strftime('%Y%m%d')
    is_td, prev = last_prev_day(today)
    if a.prev:
        prev = a.prev
    out = []
    p = out.append
    p(f"【09:35 一进二量能确认】{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  今日={today}  昨日={prev}")
    if not is_td and not a.prev:
        p('今天不是 A 股交易日：停止。')
        print('\n'.join(out)); return

    U = load_limit_pool(prev, 'U')
    if U is None or not len(U):
        p(f'[缺口] {prev} 涨停池为空，无法确认。'); print('\n'.join(out)); return
    U['lt'] = pd.to_numeric(U['limit_times'], errors='coerce').fillna(0).astype(int)
    ind_cnt = U.groupby('industry')['ts_code'].count().to_dict()
    first = U[U['lt'] == 1]
    if not len(first):
        p('昨日没有首板。'); print('\n'.join(out)); return

    # ---- 周期三数（9:25 已定，9:35 复核）----
    full_codes = U['ts_code'].tolist()
    qa = fetch_quotes(full_codes, need_time=False)
    red_pct = float('nan')
    if qa is not None and len(qa):
        red_pct = float((qa['price'] > qa['pre']).mean() * 100)
    lu_n = int(len(U)); maxb = int(U['lt'].max())
    try:
        pdays = sorted(pro.trade_cal(exchange='SSE', start_date='20260601', end_date=prev, is_open='1')['cal_date'].tolist())
        p2 = load_limit_pool(pdays[-2], 'U')
        promo = (len(set(U['ts_code']) & set(p2['ts_code'])) / max(len(p2), 1) * 100) if p2 is not None and len(p2) else float('nan')
    except Exception:
        promo = float('nan')
    relay_ok = (lu_n >= 60) and (red_pct == red_pct and red_pct >= 40)   # 最高板≥6 已于 v1.5.1 取消（sean_yang 定）
    yi_ok = (lu_n < 60) or (promo == promo and promo < 15)
    play = '高位接力' if relay_ok else ('一进二' if yi_ok else '空仓')
    p("\n== 决策摘要（09:35 复核）==")
    p("周期：昨日涨停 %d 家 / 最高板 %d 板 / 涨停池今日红盘 %.0f%% / 晋级率 %s"
      % (lu_n, maxb, red_pct if red_pct == red_pct else -1, ('%.1f%%' % promo) if promo == promo else '未取到'))
    p("高位接力条件(≥60家 且 ≥6板 且 红盘≥40%%)：%s｜一进二闸门(<60家 或 晋级<15%%)：%s → 今天做【%s】"
      % ('满足' if relay_ok else '不满足', '满足' if yi_ok else '不满足', play))

    # ---- 龙头战法首选（昨日 ≥3 板）----
    p("\n-- 龙头战法首选（昨日连板 ≥3，09:35 读数）--")
    relay_rows = []
    if qa is not None and len(qa):
        qai = qa.set_index('code')
        for _, r in U[U['lt'] >= 3].sort_values(['lt', 'amount'], ascending=[False, False]).iterrows():
            code = r['ts_code']
            if code not in qai.index:
                continue
            x = qai.loc[code]
            pre2 = float(x['pre']) if x['pre'] else 0
            px2 = float(x['price']) if x['price'] == x['price'] else 0
            if pre2 <= 0 or px2 <= 0:
                continue
            lim2 = round(pre2 * (1 + limit_pct(code)), 2)
            relay_rows.append((int(r['lt']), code, r['name'], (px2 / pre2 - 1) * 100,
                               px2 >= lim2 - 0.011, (r['amount'] or 0) / 1e8, r.get('industry', '')))
        if relay_rows:
            b, code, name, g2, seal2, a2, i2 = relay_rows[0]
            ma_txt = ''
            try:
                hh = pro.daily(ts_code=code, start_date=sorted(pro.trade_cal(exchange='SSE', start_date='20260601', end_date=prev, is_open='1')['cal_date'].tolist())[-25],
                               end_date=prev, fields='trade_date,close')
                if hh is not None and len(hh) >= 10:
                    dev = (float(hh['close'].iloc[0]) / float(hh['close'].head(10).mean()) - 1) * 100
                    ma_txt = '｜距MA10 %+.1f%%%s' % (dev, '（温度计型，只减不加）' if dev > 40 else '')
            except Exception:
                pass
            p("首选：[%d 板] %s %s｜09:35 %+.1f%%%s｜昨日成交 %.2f 亿｜%s%s"
              % (b, name, code, g2, ' 已封板' if seal2 else '', a2, i2, ma_txt))
            for b2, c2, n2, gg, se2, aa, ii in relay_rows[1:4]:
                p("备选：[%d 板] %s %s｜%+.1f%%%s｜成交 %.2f 亿" % (b2, n2, c2, gg, ' 已封板' if se2 else '', aa))
        else:
            p("昨日无 ≥3 板标的 → 无接力对象")

    q = fetch_quotes(first['ts_code'].tolist(), need_time=False)
    if q is None:
        p('[缺口] 实时快照取数失败。'); print('\n'.join(out)); return
    qi = q.set_index('code')
    # 竞价额基线：把「09:35 累计额」减成「纯 09:30–09:35 连续竞价」，这是门槛口径。三个来源按优先级：
    #   ① 官方集合竞价表 stk_auction（9:26 起可取）② snap_925_*.pkl（9:25 快照，数值与 ① 相等）③ snap_931_*.pkl
    base, label = None, ''
    AU = fetch_auction(today)
    if AU is not None and 'amount' in AU.columns:
        base = AU['amount'].dropna()
        label = '官方竞价额（stk_auction）'
    if base is None or not len(base):
        base, label = None, ''
        for f, lab in ((f'snap_925_{today}.pkl', '9:25 快照竞价额'),
                       (f'snap_931_{today}.pkl', '09:31 快照（拆出来的是 09:31–09:35）')):
            fp = os.path.expanduser(f'~/.hermes/cache/f2s_rank/{f}')
            if os.path.exists(fp):
                try:
                    dfr = pd.read_pickle(fp)
                    base = (dfr.set_index('code') if 'code' in dfr.columns else dfr)['amount']
                    label = lab
                    break
                except Exception as _e:
                    p(f"⚠ {f} 读取失败（{_e}），改用下一档基线")
                    base = None
    if base is not None:
        p(f"竞价额基线：{label}，{len(base)} 只（门槛 = (09:35 累计 − 竞价额) ÷ 首板全天）")
    else:
        p("⚠ 竞价额基线缺失（stk_auction 与 9:25 快照都没有）：量能退回 09:35 累计额含竞价近似，会高估，门槛请按 10% 严判。")
    tsnap = str(q.attrs.get('ts', '?'))
    p(f"数据源 {q.attrs.get('src', '?')}；快照时间 {tsnap}；昨日首板 {len(first)} 只")
    if tsnap[:5] > '09:40':
        p('⚠ 快照晚于 09:40：累计成交额已含盘中成交，量能一栏会失真（本脚本设计为 09:35 运行），'
          '请只看「是否上板/涨幅/静态分」，量能需按开盘 5 分钟口径重算。')

    # ---- 静态分来源：优先 9:25 落盘的排行榜 ----
    pool, src_mode = [], ''
    rp = os.path.expanduser(f'~/.hermes/cache/f2s_rank/rank_925_{today}.pkl')
    if not a.no_rank and os.path.exists(rp):
        try:
            RK = pd.read_pickle(rp)
            if str(RK.get('trade_date')) == today and str(RK.get('prev')) == prev and RK.get('rows'):
                for rr in RK['rows']:
                    pool.append(dict(code=rr['code'], name=rr['name'], sc=int(rr['sc']), why=list(rr['why']),
                                     gap=float(rr['gap']), amt_prev=float(rr['amt']), ind=int(rr['ind'])))
                src_mode = '9:25 排行榜 rank_925_%s.pkl（快照 %s，满分 10）' % (today, RK.get('snap_ts', '?'))
                p('静态分来源：%s，%d 只；9:25 硬门槛已剔除 首板额<1亿 %d / 竞昨比<2%% %d'
                  % (src_mode, len(pool), RK.get('n_gate', 0), RK.get('n_low_vp', 0)))
            else:
                p('⚠ rank_925_%s.pkl 日期不匹配（trade_date=%s prev=%s），不用它' % (today, RK.get('trade_date'), RK.get('prev')))
        except Exception as _e:
            p(f'⚠ 9:25 排行榜读取失败（{_e}）')
            pool = []
    if not pool:
        # 回退：9:25 没跑或没落盘。用本脚本旧公式重算静态分（首板额 2 / 板块 2 / 高开 1 / 封板 1，满分 6），
        # 没有量比 / 竞昨比 / 板块高度，也没做 9:25 的硬门槛 —— 档位与 9:25 卡片不可比，卡片必须注明。
        src_mode = '回退：旧公式重算（满分 6，无量比/竞昨比/板块高度，未做 9:25 硬门槛）'
        p('⚠ 未找到 9:25 排行榜 rank_925_%s.pkl（premarket_dragon_scan.py 未在 9:25 跑？）→ %s；'
          '档位与 9:25 卡片不可比，结论只看量能 / 是否封板。' % (today, src_mode))
        for _, r in first.iterrows():
            code = r['ts_code']
            if code not in qi.index:
                continue
            if code.endswith('.BJ') or 'ST' in str(r.get('name') or ''):
                continue
            x = qi.loc[code]
            pre = float(x['pre']) if x['pre'] else 0
            op = float(x['open']) if x.get('open') == x.get('open') else 0
            if pre <= 0 or op <= 0:
                continue
            gap = (op / pre - 1) * 100
            amt_prev = (r['amount'] or 0) / 1e8
            fd = (r['fd_amount'] / r['amount'] * 100) if r['amount'] else 999
            tover = float(r['turnover_ratio'] or 0)
            ft = int(str(r['first_time']).zfill(6)) if str(r['first_time']).isdigit() else 235959
            ind = ind_cnt.get(r['industry'], 0)
            sc, why = 0, []
            if amt_prev >= 5: sc += 2; why.append(f'首板{amt_prev:.1f}亿')
            elif amt_prev >= 2: sc += 1; why.append(f'首板{amt_prev:.1f}亿')
            else: sc -= 2; why.append(f'首板仅{amt_prev:.1f}亿')
            if ind >= 5: sc += 2; why.append(f'同行业{ind}家')
            elif ind >= 3: sc += 1; why.append(f'同行业{ind}家')
            else: why.append(f'板块仅{ind}家')
            if 0 <= gap <= 6: sc += 1; why.append(f'高开{gap:+.1f}%')
            elif gap > 7: sc -= 3; why.append(f'高开{gap:+.1f}%>7%')
            elif gap > 6: sc -= 1; why.append(f'高开{gap:+.1f}%偏高')
            else: sc -= 2; why.append(f'高开{gap:+.1f}%低开')
            if fd < 10 and tover >= 10: sc += 1; why.append(f'薄封单{fd:.1f}%+换手{tover:.0f}%')
            if ft > 0: why.append(f'首封{ft//10000:02d}:{ft%10000//100:02d}(不计分)')
            pool.append(dict(code=code, name=r['name'], sc=sc, why=why, gap=gap, amt_prev=amt_prev, ind=ind))

    # ---- 09:35 刷新：现价 / 是否封板 / 一字复核 / 量能分 ----
    full_amt = dict(zip(first['ts_code'], pd.to_numeric(first['amount'], errors='coerce')))   # 首板全天成交额（元）
    ind_of = dict(zip(first['ts_code'], first['industry']))
    rows = []
    n_seal = 0            # 09:35 已涨停被剔除的只数
    for c in pool:
        code = c['code']
        if code not in qi.index:
            continue
        x = qi.loc[code]
        pre = float(x['pre']) if x['pre'] else 0
        px = float(x['price']) if x['price'] == x['price'] else 0
        op = float(x['open']) if x.get('open') == x.get('open') else 0
        if pre <= 0 or px <= 0:
            continue
        lim = round(pre * (1 + limit_pct(code)), 2)
        if op and abs(op - lim) < 0.011:          # 一字/涨停开复核（9:25 已按官方竞价价剔过一次）
            continue
        if px >= lim - 0.011:                     # 09:35 已涨停 → 开盘价买点已消失，剔除（sean_yang 2026-11-04 定）
            n_seal += 1
            continue
        full = full_amt.get(code)
        amt_now = float(x['amount']) if x['amount'] == x['amount'] else float('nan')
        # 参考口径：09:35 累计额（含集合竞价）÷ 首板全天，只显示
        vol_all = (amt_now / full * 100) if (full and full == full and amt_now == amt_now) else float('nan')
        # 门槛口径：纯 09:30–09:35 连续竞价 =（累计 − 竞价额）÷ 首板全天；无基线时退回含竞价近似
        vol_pure, approx = float('nan'), False
        if base is not None and code in base.index and amt_now == amt_now and full:
            d5 = amt_now - float(base.loc[code])
            if d5 >= 0:
                vol_pure = d5 / full * 100
        if vol_pure != vol_pure and vol_all == vol_all:
            vol_pure, approx = vol_all, True
        sc, why = int(c['sc']), list(c['why'])
        vsc = 0
        if vol_pure == vol_pure:
            tag = '(含竞价近似)' if approx else ''
            if vol_pure >= a.min_amt: vsc = 2; why.append(f'<量能{vol_pure:.0f}%✓{tag}>')
            elif vol_pure >= 8: vsc = 1; why.append(f'量能{vol_pure:.0f}%偏弱{tag}')
            else: vsc = -2; why.append(f'<缩量{vol_pure:.0f}%✗{tag}>')
        else:
            why.append('量能未取到')
        if vol_all == vol_all and not approx:
            why.append(f'含竞价{vol_all:.0f}%')
        total = sc + vsc
        seal = False                     # 09:35 已涨停的票上面已整只剔除，进榜的都是还能按开盘价买的
        if total >= 8 and vol_pure == vol_pure and vol_pure >= a.min_amt:
            tier = 'A'
        elif total >= 5:
            tier = 'B'
        else:
            tier = 'C'
        rows.append(dict(tier=tier, total=total, sc=sc, vsc=vsc, code=code, name=c['name'], ind=ind_of.get(code, ''),
                         gap=c['gap'], amt_prev=c['amt_prev'], vol=vol_pure, pct=(px / pre - 1) * 100, seal=seal,
                         px=px, pre=pre, lim=lim, why=' / '.join(why)))

    if not rows:
        p(f'没有符合条件的候选（全部一字/涨停开，或 09:35 已涨停被剔除 {n_seal} 只，或无数据）。'); print('\n'.join(out)); return
    df = pd.DataFrame(rows).sort_values(['total', 'vol'], ascending=[False, False])
    p('\n== 综合排序（9:25 静态分 + 09:35 量能分；静态分来源：%s）==' % src_mode)
    for _, r in df.head(8).iterrows():
        vtxt = f"{r['vol']:.0f}%" if r['vol'] == r['vol'] else '未取到'
        p(f"[{r['tier']} {int(r['total'])}分 = 静态{int(r['sc'])}{int(r['vsc']):+d}] {r['name']} {r['code']}｜现 {r['px']:.2f} {r['pct']:+.2f}%"
          f"{' 已封板' if r['seal'] else ''}｜量能 {vtxt}（门槛 {a.min_amt:.0f}%，不含竞价）｜{r['why']}")
    nA = int((df['tier'] == 'A').sum()); nB = int((df['tier'] == 'B').sum())
    p(f"\n剔除：09:35 已涨停（开盘价买点已消失）{n_seal} 只")
    p(f"合计：A 档 {nA} 只（可执行：综合 ≥8 且量能 ≥{a.min_amt:.0f}%）｜B 档 {nB} 只（半仓/观察）｜C 档 {len(df) - nA - nB} 只不做")
    if nA:
        top = df[df['tier'] == 'A'].iloc[0]
        stop = top['px'] * 0.95
        p(f"\n动作：优先 {top['name']}（{top['code']}）。买点＝现价附近（{top['px']:.2f}）或回踩开盘价 "
          f"{top['pre'] * (1 + top['gap'] / 100):.2f} 不破；仓位 ≤10%；止损 {stop:.2f}（-5%）或跌破开盘价放弃。")
    else:
        p('\n动作：A 档 0 只 → 今天不新开仓；只在 B 档里做半仓观察，或直接空仓等明天。')
    p('\n【口径】量能＝（09:35 累计成交额 − 集合竞价成交额）÷ 首板全天成交额 = 纯 09:30–09:35 连续竞价；'
      '≥10% +2（≈量能比 1.5）、8–10% +1、<8% −2（缩量档历史胜率仅 26.6%）；'
      '「含竞价」= 09:35 累计 ÷ 首板全天，只显示不进门槛；竞价额基线缺失时量能退回含竞价近似并标注。A 档 = 9:25 静态分 + 量能分 ≥8 且量能 ≥10%。')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
