#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""交易日 09:35 一进二量能确认 + 综合判断（配合 9:25 静态排序）。

做什么
------
1. 取昨日首板（limit_list_d U，limit_times==1），剔除今日一字/涨停开
2. 09:35 快照：开盘价（算高开）、现价（是否上板）、累计成交额（≈开盘 5 分钟量能）
3. 综合打分 = 9:25 静态分（首板额/板块共振/高开/封板质量/首封）+ 量能分
   量能分：开盘 5 分钟成交 ≥ 首板全天 8% → +2；5–8% → +1；<5% → −2（缩量档胜率仅 26.6%）
4. 输出 A/B/C 重排 + 每只「上板与否 / 量能占比 / 现涨幅」，并给一句结论动作

用法
----
python3 intraday_935_confirm.py            # 正常（09:35 后跑）
python3 intraday_935_confirm.py --prev 20261102
"""
import os
import sys
import argparse
import datetime

sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from premarket_dragon_scan import pro, fetch_quotes, limit_pct, last_prev_day  # noqa: E402
import pandas as pd  # noqa: E402
import warnings  # noqa: E402

warnings.filterwarnings('ignore')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prev', default=None)
    ap.add_argument('--min-amt', type=float, default=10.0, help='量能门槛 %%（竞价+开盘5分钟 / 首板全天，默认 10%%≈量能比1.5）')
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

    U = pro.limit_list_d(trade_date=prev, limit_type='U')
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
        p2 = pro.limit_list_d(trade_date=pdays[-2], limit_type='U')
        promo = (len(set(U['ts_code']) & set(p2['ts_code'])) / max(len(p2), 1) * 100) if p2 is not None and len(p2) else float('nan')
    except Exception:
        promo = float('nan')
    relay_ok = (lu_n >= 60) and (maxb >= 6) and (red_pct == red_pct and red_pct >= 40)
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
    # 09:31 快照：用来把「累计成交额」还原成「纯开盘 5 分钟成交额」
    base, label = None, ''
    for f, lab in ((f'snap_925_{today}.pkl', '集合竞价 + 开盘 5 分钟（09:25→09:35 相减）'), 
                   (f'snap_931_{today}.pkl', '纯 09:30–09:35 连续竞价（09:31→09:35）')):
        fp = os.path.expanduser(f'~/.hermes/cache/f2s_rank/{f}')
        if os.path.exists(fp):
            try:
                dfr = pd.read_pickle(fp)
                # 兼容两种存档形状：带 code 列（新）/ code 作索引（旧）
                base = (dfr.set_index('code') if 'code' in dfr.columns else dfr)['amount']
                label = lab
                break
            except Exception as _e:
                p(f"⚠ {f} 读取失败（{_e}），改用下一档基线")
                base = None
    if base is not None:
        p(f"量能口径：{label}，存档 {len(base)} 只")
    else:
        p("⚠ 未找到 09:25 快照（premarket_dragon_scan.py 未在 9:25 跑或快照超时）："
          "量能退回累计成交额近似，含竞价会高估，门槛请按 10% 严判。")
    tsnap = str(q.attrs.get('ts', '?'))
    p(f"数据源 {q.attrs.get('src', '?')}；快照时间 {tsnap}；昨日首板 {len(first)} 只")
    if tsnap[:5] > '09:40':
        p('⚠ 快照晚于 09:40：累计成交额已含盘中成交，量能一栏会失真（本脚本设计为 09:35 运行），'
          '请只看「是否上板/涨幅/静态分」，量能需按开盘 5 分钟口径重算。')

    rows = []
    for _, r in first.iterrows():
        code = r['ts_code']
        if code not in qi.index:
            continue
        x = qi.loc[code]
        pre = float(x['pre']) if x['pre'] else 0
        px = float(x['price']) if x['price'] == x['price'] else 0
        op = float(x['open']) if x.get('open') == x.get('open') else 0
        if pre <= 0 or px <= 0:
            continue
        lim = round(pre * (1 + limit_pct(code)), 2)
        if op and abs(op - lim) < 0.011:          # 今日一字/涨停开，买不到
            continue
        gap = (op / pre - 1) * 100 if op else 0.0
        amt_prev = (r['amount'] or 0) / 1e8
        fd = (r['fd_amount'] / r['amount'] * 100) if r['amount'] else 999
        tover = float(r['turnover_ratio'] or 0)
        ft = int(str(r['first_time']).zfill(6)) if str(r['first_time']).isdigit() else 235959
        ind = ind_cnt.get(r['industry'], 0)
        amt_now = float(x['amount']) if x['amount'] == x['amount'] else float('nan')
        if base is not None and code in base.index and amt_now == amt_now:
            amt5 = amt_now - float(base.loc[code])
            if amt5 < 0:
                amt5 = amt_now
        else:
            amt5 = amt_now
        vol5 = (amt5 / r['amount'] * 100) if r['amount'] else float('nan')

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
        if vol5 == vol5:
            if vol5 >= a.min_amt: sc += 2; why.append(f'<量能{vol5:.0f}%✓>')
            elif vol5 >= 8: sc += 1; why.append(f'量能{vol5:.0f}%偏弱')
            else: sc -= 2; why.append(f'<缩量{vol5:.0f}%✗>')
        seal = px >= lim - 0.011
        rows.append(dict(sc=sc, code=code, name=r['name'], ind=r['industry'], gap=gap,
                         amt_prev=amt_prev, vol5=vol5, pct=(px / pre - 1) * 100, seal=seal,
                         px=px, pre=pre, lim=lim, why=' / '.join(why)))

    if not rows:
        p('没有符合条件的候选（全部一字/涨停开或无数据）。'); print('\n'.join(out)); return
    df = pd.DataFrame(rows).sort_values('sc', ascending=False)
    df['tier'] = df['sc'].apply(lambda s: 'A' if s >= 8 else ('B' if s >= 6 else 'C'))
    p('\n== 综合排序（9:25 静态分 + 09:35 量能分）==')
    for _, r in df.head(8).iterrows():
        p(f"[{r.tier} {int(r.sc)}分] {r['name']} {r['code']}｜现 {r['px']:.2f} {r['pct']:+.2f}%"
          f"{' 已封板' if r['seal'] else ''}｜量能 {r['vol5']:.0f}%（门槛 {a.min_amt:.0f}%）｜{r['why']}")
    nA = int((df['tier'] == 'A').sum()); nB = int((df['tier'] == 'B').sum())
    p(f"\n合计：A 档 {nA} 只（可执行）｜B 档 {nB} 只（半仓/观察）｜C 档 {len(df) - nA - nB} 只不做")
    if nA:
        top = df[df['tier'] == 'A'].iloc[0]
        stop = top['px'] * 0.95
        p(f"\n动作：优先 {top['name']}（{top['code']}）。买点＝现价附近（{top['px']:.2f}）或回踩开盘价 "
          f"{top['pre'] * (1 + top['gap'] / 100):.2f} 不破；仓位 ≤10%；止损 {stop:.2f}（-5%）或跌破开盘价放弃。")
    else:
        p('\n动作：A 档 0 只 → 今天不新开仓；只在 B 档里做半仓观察，或直接空仓等明天。')
    p('\n【口径】量能＝09:35 累计成交额 ÷ 首板全天成交额（≈开盘 5 分钟占昨全天）；'
      '未过 10% 视为缩量（10% ≈ 量能比 1.5，实测 38 个首板样本校准），缩量档历史胜率仅 26.6%。')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
