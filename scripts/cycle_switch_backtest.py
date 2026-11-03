#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""周期择时：什么周期做一进二（首板→二板），什么周期做高位接力（≥3 板）。

口径
----
- 信号日 d：limit_list_d(U) 当日涨停池
- 买入：d+1 开盘价（9:25 竞价即可成交，非条件化样本，不筛「是否摸板」）
- 卖出：d+2 收盘价
- 分组用的周期指标 —— 全部在 d+1 的 9:25 可得：
    * 涨停家数(d)、最高板(d)
    * 昨涨停池在 d 日的溢价中位（即 d-1 池在 d 日的涨跌中位）
    * 晋级率(d) = |U(d-1) ∩ U(d)| / |U(d-1)|
    * 竞价红盘率(d+1)、竞价中位(d+1)（9:25 直接读）
- 分组对比两套打法：一进二 = lt==1；高位接力 = lt>=3（另给 lt>=2 作参考）

用法
----
python3 cycle_switch_backtest.py --start 20260824 --end 20261031
"""
import argparse
import os
import sys
import warnings

try:
    import pandas as pd
except ModuleNotFoundError:
    _ALT = "/usr/local/bin/python3"
    if os.path.exists(_ALT) and not os.environ.get("_CYC_REEXEC"):
        os.environ["_CYC_REEXEC"] = "1"
        os.execv(_ALT, [_ALT, os.path.abspath(__file__), *sys.argv[1:]])
    raise

warnings.filterwarnings("ignore")
CACHE = os.path.expanduser("~/.hermes/cache/emotion")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260824")
    ap.add_argument("--end", default="20261031")
    args = ap.parse_args()

    days = sorted(f.split("_")[1].replace(".pkl", "") for f in os.listdir(CACHE) if f.startswith("daily_"))
    days = [d for d in days if args.start >= d or d >= args.start]
    days = [d for d in days if d >= args.start and d <= args.end]
    idx = {d: i for i, d in enumerate(days)}
    D, U = {}, {}
    for d in days:
        D[d] = pd.read_pickle(os.path.join(CACHE, f"daily_{d}.pkl"))
        p = os.path.join(CACHE, f"U_{d}.pkl")
        if os.path.exists(p):
            u = pd.read_pickle(p)
            u["lt"] = pd.to_numeric(u["limit_times"], errors="coerce").fillna(0).astype(int)
            U[d] = u

    # 周期指标
    cyc = {}
    for d in days:
        if d not in U:
            continue
        u = U[d]
        row = {"lu": len(u), "max_board": int(u["lt"].max())}
        i = idx[d]
        if i > 0:
            prev = days[i - 1]
            if prev in U:
                pm = D[d].set_index("ts_code")["pct_chg"]
                codes = U[prev]["ts_code"]
                row["prev_prem"] = pm.reindex(codes).median()
                row["promo"] = len(set(codes) & set(u["ts_code"])) / max(len(codes), 1) * 100
        r = (D[d].open / D[d].pre_close - 1) * 100
        row["gap_red"] = (D[d].open > D[d].pre_close).mean() * 100
        row["gap_med"] = r.median()
        cyc[d] = row

    rows = []
    for d in days[:-2]:
        if d not in U:
            continue
        d1, d2 = days[idx[d] + 1], days[idx[d] + 2]
        if d1 not in D or d2 not in D:
            continue
        a, b = D[d1].set_index("ts_code"), D[d2].set_index("ts_code")
        c = cyc.get(d)
        if not c:
            continue
        for _, r in U[d].iterrows():
            code = r["ts_code"]
            if code not in a.index or code not in b.index:
                continue
            ret = (b.loc[code, "close"] / a.loc[code, "open"] - 1) * 100
            rows.append({"signal": d, "buy_day": d1, "lt": int(r["lt"]), "ret": ret,
                         "lu": c["lu"], "max_board": c["max_board"],
                         "prev_prem": c.get("prev_prem"), "promo": c.get("promo"),
                         "gap_red": c["gap_red"]})
    df = pd.DataFrame(rows)
    if not len(df):
        print("无样本")
        return 1

    def grp(col, cuts, labels, title):
        out = []
        s = df.copy()
        s["b"] = pd.cut(s[col], cuts, labels=labels)
        for name, m in [("一进二(lt=1)", s["lt"] == 1), ("高位接力(lt≥3)", s["lt"] >= 3), ("连板≥2", s["lt"] >= 2)]:
            t = s[m].groupby("b", observed=True).agg(
                n=("ret", "size"), 胜率=("ret", lambda x: round((x > 0).mean() * 100, 1)),
                均值=("ret", lambda x: round(x.mean(), 2)))
            t.columns = pd.MultiIndex.from_product([[name], t.columns])
            out.append(t)
        res = pd.concat(out, axis=1)
        print(f"\n【{title}】")
        print(res.to_string())

    print(f"样本 {len(df)} 笔｜{df['signal'].nunique()} 个信号日")
    print(f"一进二(lt=1) n={(df['lt']==1).sum()}｜连板≥2 n={(df['lt']>=2).sum()}｜高位≥3板 n={(df['lt']>=3).sum()}")
    grp("lu", [0, 40, 60, 80, 200], ["<40", "40-60", "60-80", "≥80"], "按信号日涨停家数")
    grp("max_board", [0, 3, 5, 7, 20], ["≤3板", "4-5板", "6-7板", "≥8板"], "按最高板高度")
    grp("prev_prem", [-100, 0, 2, 5, 100], ["<0%", "0-2%", "2-5%", "≥5%"], "按昨涨停池溢价中位")
    grp("promo", [-1, 15, 25, 40, 101], ["<15%", "15-25%", "25-40%", "≥40%"], "按晋级率")
    grp("gap_red", [-1, 30, 40, 60, 101], ["<30%", "30-40%", "40-60%", "≥60%"], "按买入日竞价红盘率")
    return 0


if __name__ == "__main__":
    sys.exit(main())
