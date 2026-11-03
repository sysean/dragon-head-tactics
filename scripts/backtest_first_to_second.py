#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""一进二量价回测：首板（T-1）质量 + 竞价（T 日开盘价/高开）→ 打板或竞价买入的可执行收益。

口径
----
信号：T-1 日首板（limit_list_d U 池 limit_times==1）
排除：T 日开盘即涨停（一字/涨停开，买不到）——开盘价距涨停 <0.3%
买点两种，分开报：
  A 打板口径：T 日触及涨停 → 以涨停价成交；收盘封住 → T+1 开盘卖，炸板 → T 日收盘卖
  B 竞价口径：T 日开盘价买 → T 日收盘卖
特征（全部在 T 日 9:25 可算，量能比除外）：
  封成比 = 封单额 / 成交额（T-1）
  首封时间、炸板次数（当日开板次数）、换手率、成交额、流通市值（T-1）
  高开幅度 = T 日开盘价 / T-1 收盘 - 1（9:25 竞价即可知）
  量能比 = T 日成交额 / T-1 成交额（收盘后才知道，报告中标明为事后指标）

用法
----
python3 backtest_first_to_second.py --start 20260824 --end 20261031
python3 backtest_first_to_second.py --start 20260824 --end 20261031 --bins
"""
import argparse
import os
import sys
import time
import warnings

try:
    import pandas as pd
except ModuleNotFoundError:
    _ALT = "/usr/local/bin/python3"
    if os.path.exists(_ALT) and not os.environ.get("_F2S_REEXEC"):
        os.environ["_F2S_REEXEC"] = "1"
        os.execv(_ALT, [_ALT, os.path.abspath(__file__), *sys.argv[1:]])
    raise

warnings.filterwarnings("ignore")
CACHE = os.path.expanduser("~/.hermes/cache/emotion")


def load(pro, name, key, fn, retry=3):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{name}_{key}.pkl")
    if os.path.exists(path):
        try:
            return pd.read_pickle(path)
        except Exception:  # noqa: BLE001
            pass
    for i in range(retry):
        try:
            df = fn()
            df = pd.DataFrame() if df is None else df
            df.to_pickle(path)
            return df
        except Exception as exc:  # noqa: BLE001
            time.sleep(62 if ("频率" in str(exc) or "每分钟" in str(exc)) else 2 + i * 2)
    return pd.DataFrame()


def limit_ratio(code: str) -> float:
    c = code[:3]
    if code.startswith("688") or code.startswith("30"):
        return 1.20
    if code.startswith("8") or code.startswith("4") or code.startswith("92"):
        return 1.30
    return 1.10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260824")
    ap.add_argument("--end", default="20261031")
    ap.add_argument("--bins", action="store_true", help="打印各维度分档")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    import tushare as ts
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        for line in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
            if line.startswith("TUSHARE_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    os.environ["TUSHARE_TOKEN"] = tok
    pro = ts.pro_api(tok)

    cal = pro.trade_cal(exchange="SSE", start_date="20260801",
                        end_date=(pd.to_datetime(args.end) + pd.Timedelta(days=15)).strftime("%Y%m%d"))
    days = sorted(cal[cal.is_open == 1].cal_date.tolist())
    days = [d for d in days if d >= args.start]
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}

    rows = []
    for d in days[:-2]:
        pool = pd.read_pickle(os.path.join(CACHE, f"U_{d}.pkl")) if os.path.exists(os.path.join(CACHE, f"U_{d}.pkl")) else pd.DataFrame()
        if not len(pool):
            continue
        pool["lt"] = pd.to_numeric(pool["limit_times"], errors="coerce").fillna(0).astype(int)
        first = pool[pool["lt"] == 1]
        if not len(first):
            continue
        t1, t2 = nxt.get(d), nxt.get(nxt.get(d)) if d in nxt else None
        if not t1 or not t2:
            continue
        d1 = load(pro, "daily", t1, lambda t1=t1: pro.daily(trade_date=t1))
        d2 = load(pro, "daily", t2, lambda t2=t2: pro.daily(trade_date=t2))
        if not len(d1) or not len(d2):
            continue
        i1 = d1.set_index("ts_code")
        i2 = d2.set_index("ts_code")
        for _, r in first.iterrows():
            c = r["ts_code"]
            if c not in i1.index or c not in i2.index:
                continue
            x, y = i1.loc[c], i2.loc[c]
            up = round(x["pre_close"] * limit_ratio(c), 2)
            if x["open"] >= up - 0.011:          # 一字/涨停开 → 买不到，剔除
                continue
            hit = x["high"] >= up - 0.011          # 当天是否摸到板
            sealed = x["close"] >= up - 0.011
            # 打板口径只在「摸到板」时成立（没摸到就没有买入价）
            pnl_board = (((y["open"] if sealed else x["close"]) / up - 1) * 100) if hit else None
            pnl_auction = (x["close"] / x["open"] - 1) * 100
            rows.append({
                "signal": d, "code": c, "name": r["name"], "industry": r["industry"],
                "fd_ratio": (r["fd_amount"] / r["amount"] * 100) if r["amount"] else None,
                "first_time": int(str(r["first_time"]).zfill(6)),
                "open_times": int(pd.to_numeric(r["open_times"], errors="coerce") or 0),
                "turnover": pd.to_numeric(r["turnover_ratio"], errors="coerce"),
                "amt_yi": r["amount"] / 1e8,
                "float_yi": r["float_mv"] / 1e8,
                "gap": (x["open"] / x["pre_close"] - 1) * 100,
                "vol_ratio": (x["amount"] * 1000 / r["amount"]) if r["amount"] else None,
                "hit": bool(hit), "sealed": bool(sealed),
                "pnl_board": pnl_board, "pnl_auction": pnl_auction,
            })

    df = pd.DataFrame(rows)
    if not len(df):
        print("无样本")
        return 1
    out = os.path.expanduser("~/.hermes/cache/emotion/f2s_trades.csv")
    df.to_csv(out, index=False)
    b = df[df["hit"]].dropna(subset=["pnl_board"])
    print(f"全样本 {len(df)} 笔（T-1 首板 且 T 日非一字/涨停开）｜{df['signal'].nunique()} 个信号日")
    print(f"  其中当天摸到板 {len(b)} 笔 = 摸板率 {len(b)/len(df)*100:.1f}%")
    print(f"[竞价口径·全样本，9:25 挂单即成交] 胜率 {(df['pnl_auction']>0).mean()*100:.1f}%"
          f"｜均值 {df['pnl_auction'].mean():+.2f}%｜中位 {df['pnl_auction'].median():+.2f}%")
    print(f"[打板口径·仅摸板样本] 封板率 {b['sealed'].mean()*100:.1f}%｜胜率 {(b['pnl_board']>0).mean()*100:.1f}%"
          f"｜均值 {b['pnl_board'].mean():+.2f}%｜中位 {b['pnl_board'].median():+.2f}%")

    if args.bins:
        def show(col, cuts, labels, title):
            s = df.copy()
            s["b"] = pd.cut(s[col], cuts, labels=labels)
            g = s.groupby("b", observed=True).agg(
                n=("pnl_auction", "size"), 摸板率=("hit", lambda x: round(x.mean() * 100, 1)),
                竞价胜率=("pnl_auction", lambda x: round((x > 0).mean() * 100, 1)),
                竞价均值=("pnl_auction", lambda x: round(x.mean(), 2)),
                竞价中位=("pnl_auction", lambda x: round(x.median(), 2)),
                打板均值=("pnl_board", lambda x: round(x.mean(), 2)))
            print(f"\n【{title}】")
            print(g.to_string())

        show("gap", [-100, 0, 2, 4, 6, 9.9], ["≤0", "0-2", "2-4", "4-6", "6-10"], "次日高开幅度 %")
        show("fd_ratio", [0, 1, 3, 10, 1000], ["<1", "1-3", "3-10", ">10"], "封成比（封单/成交）")
        show("first_time", [0, 100000, 103000, 113000, 235959], ["<10:00", "10:00-10:30", "10:30-11:30", "午后"], "首板首封时间")
        show("open_times", [-1, 0, 1, 3, 99], ["0次", "1次", "2-3次", ">3次"], "首板当日炸板次数")
        show("turnover", [0, 5, 10, 20, 100], ["<5%", "5-10%", "10-20%", ">20%"], "首板换手率")
        show("amt_yi", [0, 2, 5, 10, 1000], ["<2亿", "2-5亿", "5-10亿", ">10亿"], "首板成交额")
        show("vol_ratio", [0, 1, 1.5, 2.5, 100], ["≤1.0", "1.0-1.5", "1.5-2.5", ">2.5"], "二板/首板量能比（事后）")
    print(f"\n逐笔明细已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
