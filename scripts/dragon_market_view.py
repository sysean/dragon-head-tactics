#!/usr/bin/env python3
"""龙头战法·盘面全景：周期定档 + 梯队 + 主线广度 + 昨日涨停溢价。

盘中/盘后都能跑。盘中没有官方涨停池，用实时价 vs stk_limit 自算封板与炸板；
板数 = 昨日池 limit_times + 1（不在昨日池＝首板）。

用法:
  python3 dragon_market_view.py              # 今天，实时口径
  python3 dragon_market_view.py --top 20     # 涨停明细多列几只
"""
import os
import sys
import time
import datetime as dt

import pandas as pd
import tushare as ts

IDX = {"000001.SH": "上证", "399001.SZ": "深成", "399006.SZ": "创业板",
       "000688.SH": "科创50", "000300.SH": "沪深300", "000852.SH": "中证1000"}


def token():
    t = os.environ.get("TUSHARE_TOKEN")
    if not t:
        for line in open(os.path.expanduser("~/.hermes/.env")):
            if line.startswith("TUSHARE_TOKEN"):
                t = line.split("=", 1)[1].strip()
    return t


def main():
    top = 20
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
    tok = token()
    ts.set_token(tok)
    pro = ts.pro_api(tok)
    today = dt.date.today().strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", end_date=today, start_date="20260101", is_open="1")
    days = sorted(cal.cal_date.tolist())
    if today not in days:
        print(f"{today} 非交易日"); return 3
    prev = [d for d in days if d < today][-1]

    print(f"=== 龙头战法盘面全景  {dt.datetime.now():%Y-%m-%d %H:%M:%S}  (口径 {today}) ===")

    # 指数（今日走实时，历史走 index_daily —— 盘中 index_daily 当日无数据）
    iq = ts.realtime_quote(ts_code=",".join(IDX))
    live = {}
    if iq is not None and len(iq):
        for r in iq.itertuples():
            c = r.TS_CODE if getattr(r, "TS_CODE", None) in IDX else None
            if c is None:
                continue
            try:
                live[c] = (float(r.PRICE), (float(r.PRICE) / float(r.PRE_CLOSE) - 1) * 100)
            except (TypeError, ValueError):
                pass
    for c, n in IDX.items():
        hist = pro.index_daily(ts_code=c, start_date=days[-7], end_date=prev)
        hist = hist.sort_values("trade_date") if len(hist) else hist
        pre = " ".join(f"{x:+.2f}" for x in hist.tail(2).pct_chg) if len(hist) else ""
        if c in live:
            px, pct = live[c]
            print(f"  {n:6s} {px:9.2f}  今日 {pct:+.2f}%  前2日 {pre}")
        elif len(hist):
            print(f"  {n:6s} {hist.iloc[-1].close:9.2f}  (实时未取到) 前2日 {pre}")

    # 全市场实时
    basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    out = []
    codes = basic.ts_code.tolist()
    for i in range(0, len(codes), 500):
        r = ts.realtime_quote(ts_code=",".join(codes[i:i + 500]))
        if r is not None and len(r):
            out.append(r)
        time.sleep(0.15)
    q = pd.concat(out, ignore_index=True)
    for c in ("PRICE", "PRE_CLOSE", "HIGH", "LOW", "AMOUNT"):
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q = q[q.PRICE > 0].merge(basic, left_on="TS_CODE", right_on="ts_code", how="left")
    lim = pro.stk_limit(trade_date=today)
    if not len(lim):
        lim = pro.stk_limit(trade_date=prev)
    q["up"] = q.TS_CODE.map(dict(zip(lim.ts_code, lim.up_limit)))
    q["dn"] = q.TS_CODE.map(dict(zip(lim.ts_code, lim.down_limit)))
    q["pct"] = (q.PRICE / q.PRE_CLOSE - 1) * 100
    q["amt亿"] = q.AMOUNT / 1e8
    sealed = (q.PRICE >= q.up - 0.005) & q.up.notna()
    touched = (q.HIGH >= q.up - 0.005) & q.up.notna()
    dn_sealed = (q.PRICE <= q.dn + 0.005) & q.dn.notna()

    print()
    print("【全市场】")
    print(f"  涨 {int((q.pct>0).sum())} / 跌 {int((q.pct<0).sum())} / 中位 {q.pct.median():+.2f}% / 均 {q.pct.mean():+.2f}%")
    print(f"  涨停 {int(sealed.sum())} / 跌停 {int(dn_sealed.sum())} / 触板 {int(touched.sum())} / "
          f"炸板率 {int((touched&~sealed).sum())/max(int(touched.sum()),1)*100:.1f}%")
    print(f"  涨≥5% {int((q.pct>=5).sum())} / 跌≤-5% {int((q.pct<=-5).sum())} / 成交 {q['amt亿'].sum():.0f} 亿")

    # 昨日涨停池今日表现（溢价 / 晋级率）
    ypool = pro.limit_list_d(trade_date=prev, limit_type="U")
    if len(ypool):
        ypool = ypool.merge(basic[["ts_code", "industry"]], on="ts_code", how="left", suffixes=("", "_b"))
        ym = q[q.TS_CODE.isin(ypool.ts_code)]
        re_seal = ym[ym.PRICE >= ym.up - 0.005]
        print()
        print(f"【昨日涨停 {len(ypool)} 家 今日表现】")
        print(f"  均 {ym.pct.mean():+.2f}% / 中位 {ym.pct.median():+.2f}% / 续封 {len(re_seal)} "
              f"/ 晋级率 {len(re_seal)/max(len(ypool),1)*100:.1f}% / 今日跌停 {int((ym.PRICE<=ym.dn+0.005).sum())}")
        ind = ypool.groupby("industry_b").agg(
            n=("ts_code", "size"),
            pct=("ts_code", lambda s: ym[ym.TS_CODE.isin(s)].pct.median()),
            seal=("ts_code", lambda s: int((ym[ym.TS_CODE.isin(s)].PRICE >= ym[ym.TS_CODE.isin(s)].up - 0.005).sum())))
        ind["晋级率%"] = (ind.seal / ind.n * 100).round(0)
        print("  分行业（n≥2）: ")
        print(ind[ind.n >= 2].sort_values("pct", ascending=False).round(2).to_string())

    # 今日涨停梯队
    zt = q[sealed].copy()
    if len(ypool):
        zt["board"] = zt.TS_CODE.map(dict(zip(ypool.ts_code, ypool.limit_times))).fillna(0).astype(int) + 1
    else:
        zt["board"] = 1
    print()
    print(f"【今日涨停 {len(zt)} 家 梯队】")
    print("  " + " | ".join(f"{b}板:{int((zt.board==b).sum())}" for b in sorted(zt.board.unique(), reverse=True)))
    print()
    cols = ["TS_CODE", "name", "industry", "board", "amt亿", "pct"]
    print(zt.sort_values(["board", "amt亿"], ascending=[False, False]).head(top)[cols].to_string(index=False))
    print()
    print("【行业广度 top10（今日涨停）】")
    print(zt.groupby("industry").agg(n=("TS_CODE", "size"), amt=("amt亿", "sum")).round(1)
          .sort_values("n", ascending=False).head(10).to_string())
    print()
    print("【炸板（触板未封）成交额 top10】")
    zb = q[touched & ~sealed].nlargest(10, "amt亿")
    print(zb[["TS_CODE", "name", "industry", "pct", "amt亿"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
