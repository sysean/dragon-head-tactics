#!/usr/bin/env python3
"""盘中重扫：实时全市场广度 + 昨日梯队/关注票的现价、量能、是否封板。

用途：9:25 盘前扫描给了结论后，盘中（尤其开盘 10-30 分钟）复核用。
数据源：Tushare 爬虫 ts.realtime_quote（批量 500 只/次，秒级，无限频）。

用法:
  python3 intraday_rescan.py                 # 全市场广度 + 内置关注票
  python3 intraday_rescan.py --no-market     # 只看关注票（省 10 次调用）
"""
import os
import sys
import time
import datetime as dt

import pandas as pd
import tushare as ts

WATCH = {
    # 昨日连板 ≥2
    "002708.SZ": "光洋股份4板", "001219.SZ": "青岛食品4板", "600715.SH": "文投控股3板",
    "601956.SH": "东贝集团3板", "002943.SZ": "宇晶股份2板", "002172.SZ": "澳洋健康2板",
    "603131.SH": "上海沪工2板", "002526.SZ": "山东矿机2板", "603006.SH": "联明股份2板",
    "002862.SZ": "实丰文化2板",
    # 昨日主线观察：卫星/军工
    "600118.SH": "中国卫星", "600879.SH": "航天电子", "601698.SH": "中国卫通",
    "600501.SH": "航天晨光", "688818.SH": "电科蓝天", "301306.SZ": "西测测试",
    # 昨日最高标/炸板
    "000678.SZ": "襄阳轴承(昨5板断)",
}
IDX = {"000001.SH": "上证", "399001.SZ": "深成", "399006.SZ": "创业板", "000688.SH": "科创50"}


def token():
    t = os.environ.get("TUSHARE_TOKEN")
    if not t:
        for line in open(os.path.expanduser("~/.hermes/.env")):
            if line.startswith("TUSHARE_TOKEN"):
                t = line.split("=", 1)[1].strip()
    return t


def batch_quote(codes, size=500):
    out = []
    for i in range(0, len(codes), size):
        r = ts.realtime_quote(ts_code=",".join(codes[i:i + size]))
        if r is not None and len(r):
            out.append(r)
        time.sleep(0.15)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main():
    tok = token()
    ts.set_token(tok)
    pro = ts.pro_api(tok)
    today = dt.date.today().strftime("%Y%m%d")
    print(f"=== 盘中重扫 {dt.datetime.now():%H:%M:%S} ===")

    # 指数
    try:
        iq = ts.realtime_quote(ts_code=",".join(IDX))
        if iq is not None and len(iq):
            for r in iq.itertuples():
                nm = IDX.get(r.TS_CODE, r.NAME)
                print(f"  指数 {nm:6s} {float(r.PRICE):9.2f}  {(float(r.PRICE)/float(r.PRE_CLOSE)-1)*100:+.2f}%")
    except Exception as e:
        print("  指数取数失败:", e)

    market = "--no-market" not in sys.argv
    up_limit = {}
    if market:
        basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code")
        codes = basic.ts_code.tolist()
        print(f"  [拉全市场 {len(codes)} 只 ...]")
        q = batch_quote(codes)
        if len(q):
            q["PRICE"] = pd.to_numeric(q["PRICE"], errors="coerce")
            q["PRE_CLOSE"] = pd.to_numeric(q["PRE_CLOSE"], errors="coerce")
            q["pct"] = (q.PRICE / q.PRE_CLOSE - 1) * 100
            q = q[q.PRICE > 0]
            lim = pro.stk_limit(trade_date=today)
            if not len(lim):
                prev = pro.trade_cal(exchange="SSE", end_date=today, start_date="20260101", is_open="1")
                pday = [d for d in sorted(prev.cal_date) if d < today][-1]
                lim = pro.stk_limit(trade_date=pday)
            up_limit = dict(zip(lim.ts_code, lim.up_limit))
            q["up"] = q.TS_CODE.map(up_limit)
            zt = q[(q.up.notna()) & (q.PRICE >= q.up - 0.005)]
            dn = q[(q.up.notna()) & (q.PRICE <= 2 * q.PRE_CLOSE - q.up + 0.005)]
            print(f"  全市场 涨 {int((q.pct>0).sum())} / 跌 {int((q.pct<0).sum())} / "
                  f"中位 {q.pct.median():+.2f}% / 均 {q.pct.mean():+.2f}% / "
                  f"涨停 {len(zt)} / 跌停 {len(dn)} / 涨≥5% {int((q.pct>=5).sum())} / 跌≤-5% {int((q.pct<=-5).sum())}")
            amt = pd.to_numeric(q["AMOUNT"], errors="coerce").sum() / 1e8
            print(f"  两市成交 {amt:.0f} 亿")
        else:
            print("  全市场实时取数失败")

    # 关注票
    wq = batch_quote(list(WATCH))
    if len(wq):
        wq["PRICE"] = pd.to_numeric(wq["PRICE"], errors="coerce")
        wq["PRE_CLOSE"] = pd.to_numeric(wq["PRE_CLOSE"], errors="coerce")
        wq["pct"] = (wq.PRICE / wq.PRE_CLOSE - 1) * 100
        wq["amt亿"] = pd.to_numeric(wq["AMOUNT"], errors="coerce") / 1e8
        wq["tag"] = wq.TS_CODE.map(WATCH)
        wq["state"] = wq.apply(
            lambda r: "封" if r.TS_CODE in up_limit and r.PRICE >= up_limit[r.TS_CODE] - 0.005
            else ("跌停" if r.TS_CODE in up_limit and r.PRICE <= 2 * r.PRE_CLOSE - up_limit[r.TS_CODE] + 0.005 else ""),
            axis=1)
        print()
        print(f"  {'标的':14s} {'现价':>7s} {'涨幅':>7s} {'成交亿':>7s} {'买一量':>9s}  状态")
        for r in wq.itertuples():
            b1 = pd.to_numeric(getattr(r, "B1_V", None), errors="coerce")
            print(f"  {r.tag:14s} {r.PRICE:7.2f} {r.pct:+7.2f}% {r.amt亿:7.2f} "
                  f"{(b1 if pd.notna(b1) else 0):9.0f}  {r.state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
