#!/usr/bin/env python3
"""多游资批量席位画像 / Multi hot-money roster profiler.

一次扫描区间内每个交易日的 top_inst（限缩到 hm_list 命中席位）+ limit_list_d(U 池)，
按游资聚合输出「上榜日 / 买入中位 / 买在涨停日 / 买后表现 / 卖出日涨幅 / 板位分布 /
流通市值中位 / 持仓节奏 / 最常做的票」。

逐日结果缓存到 ~/.hermes/cache/hotmoney/（HOTMONEY_CACHE 可覆盖），重复区间几乎秒出：
  - daily_<date>.csv、lu_<date>.csv  逐日行情与涨停池
  - multi_<start>_<end>.csv          命中席位的明细

Usage:
  python3 query_hot_money_roster_profile.py --start 20260401 --end 20261016
  python3 query_hot_money_roster_profile.py --start 20260401 --end 20261016 --names 赵老哥,陈小群,作手新一
  python3 query_hot_money_roster_profile.py --start 20260401 --end 20261016 --min-buy 10   # 只看买入≥N笔
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE = Path(os.environ.get("HOTMONEY_CACHE", Path.home() / ".hermes/cache/hotmoney"))
CACHE.mkdir(parents=True, exist_ok=True)

# 席位名归一化：去掉公司类型词/空白/括号，用于 hm_list 简称 ↔ top_inst 全称 的精确比对。
# 禁止子串模糊匹配认游资。
NOISE = re.compile(
    r"(证券股份有限公司|证券有限公司|股份有限公司|证券营业部|营业部|分公司|有限公司|证券|公司|\(|\)|（|）|\s)"
)

DEFAULT_TARGETS = [
    "赵老哥", "章盟主", "炒股养家", "陈小群", "作手新一", "欢乐海岸",
    "孙哥", "佛山系", "宁波桑田路", "金田路", "92科比", "方新侠",
    "一瞬流光", "上塘路", "湖里大道", "小鳄鱼", "六一路", "古北路",
    "呼家楼", "北京炒家", "流沙河", "消闲派", "湖滨四季", "涅盘重升",
    "著名刺客", "乔帮主", "列夫", "徐晓", "交易猿", "毛老板",
]


def norm(s: str) -> str:
    return NOISE.sub("", str(s))


def pro_api():
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        sys.exit("TUSHARE_TOKEN 未设置")
    return ts.pro_api(token)


def limit_threshold(code: str) -> float:
    """按板块给涨停阈值（留 0.3 个点安全边际）。"""
    c = str(code)
    if c[:3] in ("300", "301", "688", "689"):
        return 19.7
    if c[0] in ("4", "8") or c[:2] == "92":
        return 29.7
    return 9.7


def load_seat_hits(pro, targets: list[str], start: str, end: str) -> pd.DataFrame:
    """逐日扫 top_inst，只留命中目标游资席位的行；逐日缓存行情与涨停池。"""
    hm = pro.hm_list()
    name2norm, seat2names = {}, defaultdict(set)
    for _, r in hm.iterrows():
        v = r["orgs"]
        orgs = json.loads(v) if isinstance(v, str) and v.strip().startswith("[") else ([v] if isinstance(v, str) else [])
        name2norm[r["name"]] = {norm(o) for o in orgs}
        for o in orgs:
            seat2names[norm(o)].add(r["name"])

    wanted = set(targets)
    for t in targets:
        if t not in name2norm:
            print(f"# WARN hm_list 无此名: {t}", file=sys.stderr)
    alias = set()
    for t in wanted:
        alias |= name2norm.get(t, set())

    cache_csv = CACHE / f"multi_{start}_{end}.csv"
    if cache_csv.exists():
        inst = pd.read_csv(cache_csv, dtype={"trade_date": str, "ts_code": str})
        print(f"# 命中缓存 {cache_csv}", file=sys.stderr)
    else:
        dates = sorted(pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")["cal_date"].tolist())
        frames = []
        for i, d in enumerate(dates, 1):
            try:
                df = pro.top_inst(trade_date=d)
            except Exception:
                time.sleep(3)
                try:
                    df = pro.top_inst(trade_date=d)
                except Exception as exc:
                    print(f"# {d} top_inst 失败: {exc}", file=sys.stderr)
                    continue
            if df is not None and not df.empty:
                m = df[df["exalter"].map(norm).isin(alias)].copy()
                if not m.empty:
                    frames.append(m)
            for f, fn in ((f"daily_{d}.csv", lambda: pro.daily(trade_date=d)),
                          (f"lu_{d}.csv", lambda: pro.limit_list_d(trade_date=d, limit_type="U"))):
                p = CACHE / f
                if not p.exists():
                    try:
                        out = fn()
                        if out is not None and not out.empty:
                            out.to_csv(p, index=False)
                    except Exception as exc:
                        print(f"# {f} 失败: {exc}", file=sys.stderr)
                    time.sleep(0.15)
            if i % 25 == 0:
                print(f"# 扫描进度 {i}/{len(dates)}", file=sys.stderr)
            time.sleep(0.16)
        if not frames:
            sys.exit("区间内无命中席位的上榜记录。")
        inst = pd.concat(frames, ignore_index=True)
        # 同一 (代码, 席位, 方向, 上榜原因) 会因多个上榜原因重复
        inst = inst.drop_duplicates(subset=["trade_date", "ts_code", "exalter", "side", "reason"])
        inst.to_csv(cache_csv, index=False)

    inst["who"] = inst["exalter"].map(
        lambda e: "、".join(sorted(seat2names.get(norm(e), set()) & wanted))
    )
    return inst[inst["who"] != ""]


def trading_days(pro, start: str, end: str) -> list[str]:
    return sorted(pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")["cal_date"].tolist())


def load_daily(dates: list[str], start: str, end: str, pro) -> pd.DataFrame:
    """必须按【完整交易日历】取日线，否则 groupby.shift(-1) 会跳过错过的交易日，
    把「次日涨幅」与 T+N 间隔算错。"""
    frames = []
    for d in dates:
        p = CACHE / f"daily_{d}.csv"
        if p.exists():
            frames.append(pd.read_csv(p, dtype={"trade_date": str, "ts_code": str}))
        else:
            try:
                out = pro.daily(trade_date=d)
            except Exception as exc:
                print(f"# daily {d} 失败: {exc}", file=sys.stderr)
                continue
            out.to_csv(p, index=False)
            frames.append(out)
            time.sleep(0.15)
    k = pd.concat(frames, ignore_index=True)
    k = k[(k["trade_date"] >= start) & (k["trade_date"] <= end)]
    k = k.sort_values(["ts_code", "trade_date"])
    k["next_pct"] = k.groupby("ts_code")["pct_chg"].shift(-1)
    k["next2_pct"] = k.groupby("ts_code")["pct_chg"].shift(-2)
    return k


def load_boards(inst: pd.DataFrame, pro) -> dict:
    """(trade_date, ts_code) → 涨停池行；用于板位与流通市值。"""
    board = {}
    for d in sorted(inst["trade_date"].unique()):
        p = CACHE / f"lu_{d}.csv"
        if not p.exists():
            try:
                lu = pro.limit_list_d(trade_date=d, limit_type="U")
            except Exception:
                continue
            if lu is None or lu.empty:
                continue
            lu.to_csv(p, index=False)
            time.sleep(0.15)
        lu = pd.read_csv(p, dtype={"trade_date": str, "ts_code": str})
        for _, r in lu.iterrows():
            board[(r["trade_date"], r["ts_code"])] = r
    return board


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--names", help="逗号分隔的游资名，默认内置龙头战法名录")
    ap.add_argument("--min-buy", type=int, default=4, help="买入笔数下限，低于此不输出")
    args = ap.parse_args()

    targets = [x.strip() for x in args.names.split(",")] if args.names else DEFAULT_TARGETS
    pro = pro_api()
    inst = load_seat_hits(pro, targets, args.start, args.end)
    print(f"# 命中席位明细 {len(inst)} 行；游资 {inst['who'].nunique()} 个", file=sys.stderr)

    recs = []
    for _, r in inst.iterrows():
        for w in [x for x in r["who"].split("、") if x]:
            recs.append((w, r["trade_date"], r["ts_code"], float(r["buy"] or 0), float(r["sell"] or 0)))
    g = pd.DataFrame(recs, columns=["who", "date", "code", "buy", "sell"])
    g = g.groupby(["who", "date", "code"], as_index=False).agg(buy=("buy", "sum"), sell=("sell", "sum"))
    g["net"] = g["buy"] - g["sell"]

    k = load_daily(trading_days(pro, args.start, args.end), args.start, args.end, pro)
    g = g.merge(k[["ts_code", "trade_date", "pct_chg", "next_pct", "next2_pct"]],
                left_on=["code", "date"], right_on=["ts_code", "trade_date"], how="left")
    g["lim"] = g["code"].map(limit_threshold)
    g["lu"] = g["pct_chg"].notna() & (g["pct_chg"] >= g["lim"])

    board = load_boards(inst, pro)
    cal = trading_days(pro, args.start, args.end)
    idx = {d: i for i, d in enumerate(cal)}

    print(f"\n# 区间 {args.start}~{args.end}（{len(cal)} 个交易日）｜hm_list 席位 ∩ top_inst，按 (日期,代码) 汇总｜金额单位：元\n")
    print("| 游资 | 上榜日 | 个股 | 买入/卖出笔 | 买入中位 | 买入合计(亿) | 净额(亿) | 买在涨停日 | 买日涨幅中位 | "
          "次日中位 | 次日胜率 | 第3日中位 | 卖日涨幅中位 | 卖在上涨日 | 板位分布 | 流通市值中位(亿) | 持仓节奏 | 最常做 |")
    print("|" + "---|" * 18)

    rows = []
    for w, sub in g.groupby("who"):
        b, s = sub[sub["buy"] > 0], sub[sub["sell"] > 0]
        if len(b) < args.min_buy:
            continue
        bp, fms = {}, []
        for _, r in b.iterrows():
            t = board.get((r["date"], r["code"]))
            if t is not None:
                bp[int(t["limit_times"] or 1)] = bp.get(int(t["limit_times"] or 1), 0) + 1
                if pd.notna(t.get("float_mv")):
                    fms.append(float(t["float_mv"]))
        spans = []
        for _, ss in sub.sort_values("date").groupby("code"):
            buys = ss[ss["buy"] > 0]["date"].tolist()
            sells = ss[ss["sell"] > 0]["date"].tolist()
            for x in buys:
                later = [y for y in sells if y > x]
                spans.append((x, later[0] if later else None))
        dd = [idx[e] - idx[x] for x, e in spans if x in idx and e in idx]
        rows.append(dict(
            who=w, days=sub["date"].nunique(), codes=sub["code"].nunique(), nbuy=len(b), nsell=len(s),
            medb=b["buy"].median(), totb=b["buy"].sum(), net=sub["buy"].sum() - sub["sell"].sum(),
            lu=b["lu"].mean() * 100, bday=b["pct_chg"].median(),
            nx=b["next_pct"].median(), nwr=(b["next_pct"] > 0).mean() * 100, n2=b["next2_pct"].median(),
            sday=s["pct_chg"].median() if len(s) else float("nan"),
            slu=s["lu"].mean() * 100 if len(s) else float("nan"),
            tn=" ".join(f"T+{n}:{dd.count(n)}" for n in sorted(set(dd))) if dd else "—",
            unpaired=sum(1 for _, e in spans if e is None),
            bp=" ".join(f"{a}板:{v}" for a, v in sorted(bp.items())),
            fmv=pd.Series(fms).median() if fms else float("nan"),
            topl=" ".join(f"{c.split('.')[0]}({v / 1e4:.0f}万)"
                          for c, v in b.groupby("code")["buy"].sum().sort_values(ascending=False).head(3).items()),
        ))

    for r in sorted(rows, key=lambda x: -x["totb"]):
        f = r["fmv"]
        print(f"| {r['who']} | {r['days']} | {r['codes']} | {r['nbuy']}/{r['nsell']} | {r['medb'] / 1e4:.0f}万 | "
              f"{r['totb'] / 1e8:.2f} | {r['net'] / 1e8:+.2f} | {r['lu']:.0f}% | {r['bday']:+.2f}% | {r['nx']:+.2f}% | "
              f"{r['nwr']:.0f}% | {r['n2']:+.2f}% | {r['sday']:+.2f}% | {r['slu']:.0f}% | {r['bp']} | "
              f"{'' if pd.isna(f) else f'{f / 1e8:.0f}亿'} | {r['tn']} | {r['topl']} |")


if __name__ == "__main__":
    main()
