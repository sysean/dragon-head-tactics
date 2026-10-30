#!/usr/bin/env python3
"""校准「一进二」量能阈值：首板量能分组 × 二板上板量能分档 → 触板/一字/封板/打板收益。

口径（与 yz-dragon-head-tactics / a-share-limit-up-speed 一致）：
- 涨停价用 pro.stk_limit.up_limit（权威，含 ST 特例）
- 首板 = 当日收盘封住涨停 且 前一交易日未封住涨停
- 触板 = 当日 high 触及涨停价；封板 = 当日 close 在涨停价
- **一字板 = 当日 low 也在涨停价（全天没在涨停价下方成交）→ 打板买不到，必须单列**
- 打板收益只在「可打样本」（触板 且 非一字）里算：
    买价 = 涨停价；封住 → T+2 开盘/收盘卖；炸板 → T+1 收盘卖
- 量能比全部用 daily.amount（千元，比值无量纲）
- 左侧顶量 = 首板前 T-61..T-2 的 amount 最大值
- 前后必须紧邻交易日，停牌缺口直接剔除

已知局限：全天量能 ≠ 上板那一刻的量能（封板后仍有成交），回测只能用全天口径。
"""
import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

CACHE = Path.home() / ".hermes/cache/limitpool"
CACHE.mkdir(parents=True, exist_ok=True)
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")
EPS = 0.005


def pro_api():
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        sys.exit("缺 TUSHARE_TOKEN")
    return ts.pro_api(tok)


def trade_dates(pro, start, end):
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(df.cal_date.tolist())


def load_daily(pro, dates):
    frames = []
    for i, d in enumerate(dates):
        f = CACHE / f"daily_{d}.pkl"
        if f.exists():
            frames.append(pd.read_pickle(f))
            continue
        for _ in range(3):
            try:
                df = pro.daily(trade_date=d)
                if df is not None and len(df):
                    df.to_pickle(f)
                    frames.append(df)
                    break
            except Exception:
                time.sleep(1.5)
        if i % 30 == 0:
            print(f"  daily {d} ({i+1}/{len(dates)})", flush=True)
    return pd.concat(frames, ignore_index=True)


def load_up_limits(pro, dates):
    out = []
    for i, d in enumerate(dates):
        f = CACHE / f"stk_limit_{d}.pkl"
        if f.exists():
            out.append(pd.read_pickle(f))
            continue
        for _ in range(3):
            try:
                df = pro.stk_limit(trade_date=d)
                if df is not None and len(df):
                    df["trade_date"] = d
                    df.to_pickle(f)
                    out.append(df)
                    break
            except Exception:
                time.sleep(1.5)
        if i % 30 == 0:
            print(f"  stk_limit {d} ({i+1}/{len(dates)})", flush=True)
    return pd.concat(out, ignore_index=True)


def load_basic(pro):
    f = CACHE / "stock_basic.pkl"
    if f.exists():
        return pd.read_pickle(f)
    df = pro.stock_basic(exchange="", list_status="L",
                         fields="ts_code,name,industry,list_date,market")
    df.to_pickle(f)
    return df


def p(x):
    return f"{100.0*x:6.1f}%" if pd.notna(x) else "     — "


def grp0(r):
    if r < 0.8:
        return "1 缩量 <0.8"
    if r < 1.2:
        return "2 平量 0.8-1.2"
    if r < 2.0:
        return "3 放量 1.2-2.0"
    if r < 3.0:
        return "4 大放量 2-3"
    return "5 爆量 >3"


def grp1(r):
    if r < 1 / 3:
        return "a <1/3"
    if r < 0.5:
        return "b 1/3-1/2"
    if r < 2 / 3:
        return "c 1/2-2/3"
    if r < 1.0:
        return "d 2/3-1"
    if r < 1.5:
        return "e 1-1.5"
    return "f >1.5"


def grp_peak(r):
    if r < 1 / 3:
        return "a <1/3"
    if r < 0.5:
        return "b 1/3-1/2"
    if r < 2 / 3:
        return "c 1/2-2/3"
    if r < 1.0:
        return "d 2/3-1"
    return "e >1"


def summarize(s, title, note=""):
    """s 需含: touched_next, sealed_next, yizi_next, ret_close, ret_open, ret_break"""
    s = s.copy()
    for c in ["touched_next", "sealed_next", "yizi_next"]:
        s[c] = s[c].fillna(False).astype(bool)
    played = s[s.touched_next & ~s.yizi_next]
    print(f"\n{title}{note}")
    print(f"{'分档':<16}{'首板数':>6}{'触板率':>8}{'一字率':>7}{'可打':>6}"
          f"{'封板率':>8}{'炸板率':>8}{'收盘卖均值':>9}{'收盘卖中位':>9}{'开盘卖均值':>9}")
    for k in sorted(s.gk.dropna().unique()):
        ss = s[s.gk == k]
        pl = played[played.gk == k]
        n = len(ss)
        if n == 0:
            continue
        tc = ss.touched_next.mean()
        yz = ss.yizi_next.mean()
        sr = pl.sealed_next.mean() if len(pl) else np.nan
        print(f"{k:<16}{n:>6}{p(tc):>8}{p(yz):>7}{len(pl):>6}{p(sr):>8}"
              f"{p(1-sr) if pd.notna(sr) else '     — ':>8}"
              f"{p(pl.ret_close.mean()) if len(pl) else '     — ':>9}"
              f"{p(pl.ret_close.median()) if len(pl) else '     — ':>9}"
              f"{p(pl.ret_open.mean()) if len(pl) else '     — ':>9}")
    pl = played
    print(f"{'合计':<16}{len(s):>6}{p(s.touched_next.mean()):>8}{p(s.yizi_next.mean()):>7}"
          f"{len(pl):>6}{p(pl.sealed_next.mean()) if len(pl) else '—':>8}"
          f"{p(1-pl.sealed_next.mean()) if len(pl) else '—':>8}"
          f"{p(pl.ret_close.mean()) if len(pl) else '—':>9}"
          f"{p(pl.ret_close.median()) if len(pl) else '—':>9}"
          f"{p(pl.ret_open.mean()) if len(pl) else '—':>9}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260801")
    ap.add_argument("--end", default="20261029")
    args = ap.parse_args()

    pro = pro_api()
    dates = trade_dates(pro, "20260201", args.end)
    win = [d for d in dates if args.start <= d <= args.end]
    nxt = {dates[i]: dates[i + 1] if i + 1 < len(dates) else None for i in range(len(dates))}
    prv = {dates[i]: dates[i - 1] if i > 0 else None for i in range(len(dates))}

    print(f"取数 {args.start}~{args.end}，{len(win)} 个交易日", flush=True)
    panel = load_daily(pro, dates)
    lims = load_up_limits(pro, dates)
    basic = load_basic(pro)

    panel = panel[panel.ts_code.str[:3].isin(MAIN_PREFIX)]
    panel = panel.merge(basic[["ts_code", "name", "list_date"]], on="ts_code", how="left")
    panel = panel[~panel.name.fillna("").str.contains("ST|退")]
    panel = panel.merge(lims[["ts_code", "trade_date", "up_limit"]],
                        on=["ts_code", "trade_date"], how="left")
    panel["up_limit"] = pd.to_numeric(panel["up_limit"], errors="coerce")
    panel["amount"] = pd.to_numeric(panel["amount"], errors="coerce")
    panel = panel.dropna(subset=["up_limit", "amount"])

    for a, b in [("close", "sealed"), ("high", "touched"), ("low", "yizi")]:
        panel[b] = panel[a] >= panel.up_limit - EPS

    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    g = panel.groupby("ts_code", sort=False)
    for src, dst in [("amount", "amt_prev"), ("trade_date", "date_prev"),
                     ("sealed", "sealed_prev")]:
        panel[dst] = g[src].shift(1)
    for src, dst in [("amount", "amt_next"), ("touched", "touched_next"),
                     ("sealed", "sealed_next"), ("yizi", "yizi_next"),
                     ("up_limit", "up_next"), ("close", "close_next"),
                     ("open", "open_next"), ("trade_date", "date_next")]:
        panel[dst] = g[src].shift(-1)
    for src, dst in [("close", "close_n2"), ("open", "open_n2"),
                     ("trade_date", "date_n2")]:
        panel[dst] = g[src].shift(-2)

    left = (panel.groupby("ts_code", sort=False)["amount"]
            .apply(lambda s: s.shift(1).rolling(60, min_periods=10).max().shift(1))
            .reset_index(level=0, drop=True))
    panel["left_peak"] = left

    first = panel[panel.sealed & ~panel.sealed_prev.fillna(False)
                  & panel.trade_date.isin(win)].copy()
    first = first[first.trade_date.apply(lambda d: prv.get(d)).values == first.date_prev.values]
    first = first[first.trade_date.apply(lambda d: nxt.get(d)).values == first.date_next.values]
    first = first[first.date_next.notna()]
    first["list_ok"] = (pd.to_datetime(first.trade_date, format="%Y%m%d")
                        - pd.to_datetime(first.list_date, format="%Y%m%d")).dt.days >= 60
    first = first[first.list_ok]

    first["r0"] = first.amount / first.amt_prev
    first["r1"] = first.amt_next / first.amount
    first["r1_peak"] = first.amt_next / first.left_peak

    for c in ["touched_next", "sealed_next", "yizi_next"]:
        first[c] = first[c].fillna(False).astype(bool)
    sealed = first.sealed_next.values
    n2_ok = (first.date_next.apply(lambda d: nxt.get(d)).values == first.date_n2.values)
    first["ret_close"] = np.where(sealed & n2_ok, first.close_n2 / first.up_next - 1,
                                  np.where(sealed, np.nan, first.close_next / first.up_next - 1))
    first["ret_open"] = np.where(sealed & n2_ok, first.open_n2 / first.up_next - 1,
                                 np.where(sealed, np.nan, first.close_next / first.up_next - 1))
    # 打板收益只在「可打样本」（触板 且 非一字）里成立；没触板就没有成交，一律置 NaN
    first["ret_next_open"] = np.where(sealed & n2_ok, first.open_n2 / first.up_next - 1, np.nan)
    first["next_open_prem"] = first.open_next / first.close - 1
    playable = (first.touched_next & ~first.yizi_next).values
    first.loc[~playable, ["ret_close", "ret_open", "ret_next_open"]] = np.nan

    print(f"\n首板样本 {len(first)} 笔 / {first.trade_date.nunique()} 个交易日"
          f"（主板+创业板，非 ST，上市 ≥60 日，剔除停牌缺口）")
    print(f"全样本一进二：触板 {p(first.touched_next.mean())}，"
          f"其中一字买不到 {p(first.yizi_next.mean())}，"
          f"可打样本封板率 {p(first[first.touched_next & ~first.yizi_next].sealed_next.mean())}")

    first["gk"] = first.r0.apply(lambda x: grp0(x) if pd.notna(x) else None)
    summarize(first, "【表1】首板自身量能比（今日/昨日）→ 次日一进二",
              "（无未来函数；首板收盘即可算）")

    for label, cond in [("首板放量 r0>=1.2", first.r0 >= 1.2),
                        ("首板缩量 r0<1.0", first.r0 < 1.0)]:
        s = first[cond & first.r1.notna()].copy()
        s["gk"] = s.r1.apply(grp1)
        summarize(s, f"【表2】{label}｜二板上板量能比（T+1/T）分档",
                  f"｜首板 {len(s)} 笔")

    s = first[(first.r0 < 1.0) & first.r1_peak.notna()].copy()
    s["gk"] = s.r1_peak.apply(grp_peak)
    summarize(s, "【表3】首板缩量组｜二板量能 / 左侧60日顶量（万战口径②）", f"｜首板 {len(s)} 笔")

    played = first[first.touched_next & ~first.yizi_next]
    print("\n【表4】可打样本里，二板量能 ≥ 首板 是否「必炸」")
    for lo, hi, name in [(0.0, 2 / 3, "<2/3"), (2 / 3, 1.0, "2/3-1"),
                         (1.0, 1.5, "1-1.5"), (1.5, 99, ">1.5")]:
        ss = played[(played.r1 >= lo) & (played.r1 < hi)]
        if len(ss):
            print(f"  {name:<8} 可打 {len(ss):>4} 笔｜炸板 {int((~ss.sealed_next).sum()):>3} 笔"
                  f"｜炸板率 {p(1-ss.sealed_next.mean())}｜收盘卖均值 {p(ss.ret_close.mean())}")

    out = CACHE / f"calib_first_to_second_{args.start}_{args.end}.csv"
    keep = ["trade_date", "ts_code", "name", "r0", "r1", "r1_peak", "touched_next",
            "yizi_next", "sealed_next", "next_open_prem", "ret_close", "ret_open"]
    first[keep].to_csv(out, index=False)
    print(f"\n逐笔明细：{out}")


if __name__ == "__main__":
    main()
