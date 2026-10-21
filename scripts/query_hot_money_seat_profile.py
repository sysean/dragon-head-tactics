#!/usr/bin/env python3
"""游资席位行为画像 / Hot-money seat behaviour profiler (default: 一瞬流光).

Data: Tushare hm_list (游资→席位名录) + top_inst (席位明细) + daily (个股行情).
Logic: 游资名 → 席位 → 逐交易日扫 top_inst，归一化席位名精确匹配，
       按 (ts_code) 串联买卖日，统计持仓节奏与买后表现。

Usage:
  python3 query_hot_money_seat_profile.py --name 一瞬流光 --months 12
  python3 query_hot_money_seat_profile.py --name 一瞬流光 --start 20250101 --end 20261010 --detail
  python3 query_hot_money_seat_profile.py --name 一瞬流光 --cooccur
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

CACHE = Path(os.environ.get("HOTMONEY_CACHE", Path.home() / ".hermes/cache/hotmoney"))
CACHE.mkdir(parents=True, exist_ok=True)

# 席位名归一化：去掉公司类型词/空白/括号，用于 hm_list 简称 ↔ top_inst 全称 的精确比对。
# 保留“路/部/分公司”等有区分度的后缀词。禁止子串模糊匹配认游资。
NOISE = re.compile(
    r"(证券股份有限公司|证券有限公司|股份有限公司|证券营业部|营业部|分公司|有限公司|证券|公司|\(|\)|（|）|\s)"
)


def norm(s: str) -> str:
    return NOISE.sub("", str(s))


def pro_api():
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        sys.exit("TUSHARE_TOKEN 未设置")
    return ts.pro_api(token)


def resolve_seats(pro, name: str):
    hm = pro.hm_list()
    hit = hm[hm["name"] == name]
    if hit.empty:
        hit = hm[hm["name"].str.contains(name, na=False)]
    if hit.empty:
        sys.exit(f"hm_list 里没有游资 {name!r}")
    print(f"# 游资 {hit.iloc[0]['name']} / 风格: {hit.iloc[0]['desc']}")
    orgs = []
    for v in hit["orgs"]:
        if isinstance(v, str) and v.strip().startswith("["):
            orgs.extend(json.loads(v))
        elif isinstance(v, str):
            orgs.extend([x for x in re.split(r"[，,;；\s]+", v) if x])
    return sorted(set(orgs)), hit


def scan_range(pro, alias_norms: set, start: str, end: str, verbose: bool = True) -> pd.DataFrame:
    tag = "_".join(sorted(alias_norms))[:40]
    # 注意：Python 的 hash() 对 str 每进程随机化，缓存 key 必须用 hashlib
    key = hashlib.md5(tag.encode()).hexdigest()[:10]
    cache_csv = CACHE / f"topinst_{start}_{end}_{key}.csv"
    if cache_csv.exists():
        if verbose:
            print(f"# 命中缓存 {cache_csv}", file=sys.stderr)
        return pd.read_csv(cache_csv, dtype={"trade_date": str, "ts_code": str, "exalter": str})

    dates = sorted(pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")["cal_date"].tolist())
    frames = []
    for i, d in enumerate(dates, 1):
        try:
            df = pro.top_inst(trade_date=d)
        except Exception as exc:  # 限频/抖动退避一次
            time.sleep(3)
            try:
                df = pro.top_inst(trade_date=d)
            except Exception:
                print(f"# {d} 拉取失败: {exc}", file=sys.stderr)
                continue
        if df is not None and not df.empty:
            m = df[df["exalter"].map(norm).isin(alias_norms)]
            if not m.empty:
                frames.append(m)
        if verbose and i % 40 == 0:
            print(f"# 扫描进度 {i}/{len(dates)}", file=sys.stderr)
        time.sleep(0.16)

    if frames:
        out = pd.concat(frames, ignore_index=True)
        # 同一 (代码, 席位, 方向, 上榜原因) 会因多个上榜原因重复
        out = out.drop_duplicates(subset=["trade_date", "ts_code", "exalter", "side", "reason"])
    else:
        out = pd.DataFrame(
            columns=["trade_date", "ts_code", "exalter", "buy", "buy_rate", "sell", "sell_rate", "net_buy", "side", "reason"]
        )
    out.to_csv(cache_csv, index=False)
    return out


def seat_owner_map(pro) -> dict:
    hm = pro.hm_list()
    m = {}
    for _, r in hm.iterrows():
        v = r["orgs"]
        orgs = json.loads(v) if isinstance(v, str) and v.strip().startswith("[") else [v]
        for o in orgs:
            m.setdefault(norm(o), set()).add(r["name"])
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="一瞬流光")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--detail", action="store_true", help="打印逐笔上榜明细")
    ap.add_argument("--cooccur", action="store_true", help="统计同榜出现的其它游资")
    args = ap.parse_args()

    pro = pro_api()
    today = datetime.now().strftime("%Y%m%d")
    end = args.end or today
    start = args.start or (
        datetime.strptime(end, "%Y%m%d") - timedelta(days=args.months * 31 + 10)
    ).strftime("%Y%m%d")

    orgs, _ = resolve_seats(pro, args.name)
    alias = {norm(o) for o in orgs}
    print(f"# 席位简称: {' / '.join(orgs)}")
    print(f"# 扫描区间: {start} ~ {end}\n")

    inst = scan_range(pro, alias, start, end)
    if inst.empty:
        print("区间内无该游资席位上榜记录。")
        return

    seat2who = seat_owner_map(pro)
    g = (
        inst.groupby(["trade_date", "ts_code"])
        .agg(buy=("buy", "sum"), sell=("sell", "sum"), seats=("exalter", lambda s: "、".join(sorted(set(s)))))
        .reset_index()
    )
    g["net"] = g["buy"] - g["sell"]
    g["action"] = ["净买" if n > 0 else ("净卖" if n < 0 else "平") for n in g["net"]]

    print("## 汇总")
    print(f"上榜交易日数: {g['trade_date'].nunique()}  |  涉及个股: {g['ts_code'].nunique()}  |  买卖记录: {len(g)} 条")
    print(f"净买记录: {(g['action'] == '净买').sum()} 条  |  净卖记录: {(g['action'] == '净卖').sum()} 条")
    print(
        f"买入总额: {g['buy'].sum() / 1e8:.2f} 亿  |  卖出总额: {g['sell'].sum() / 1e8:.2f} 亿  |  净额: {g['net'].sum() / 1e8:+.2f} 亿"
    )
    buys = g[g["buy"] > 0]["buy"]
    if len(buys):
        print(f"单笔买入: 中位数 {buys.median() / 1e4:.0f} 万  均值 {buys.mean() / 1e4:.0f} 万  最大 {buys.max() / 1e8:.2f} 亿")

    codes = sorted(g["ts_code"].unique())
    # 全市场区间拉取会被 API 截断，逐票拉更稳
    k = pd.concat([pro.daily(ts_code=c, start_date=start, end_date=end) for c in codes], ignore_index=True)
    if k.empty:
        print("\n（未取到行情数据，跳过买后表现）")
        return
    k = k.sort_values(["ts_code", "trade_date"])
    k["next_pct"] = k.groupby("ts_code")["pct_chg"].shift(-1)
    k["next2_pct"] = k.groupby("ts_code")["pct_chg"].shift(-2)
    m = g.merge(k[["ts_code", "trade_date", "pct_chg", "next_pct", "next2_pct"]], on=["ts_code", "trade_date"], how="left")

    b, s = m[m["buy"] > 0], m[m["sell"] > 0]
    print(f"\n## 买后表现（买入日共 {len(b)} 笔）")
    if len(b) and b["pct_chg"].notna().any():
        print(
            f"买入当日涨幅中位数: {b['pct_chg'].median():+.2f}%  "
            f"(涨停/接近涨停 {(b['pct_chg'] >= 9.7).sum()} 笔, 占比 {100 * (b['pct_chg'] >= 9.7).mean():.0f}%)"
        )
        print(
            f"次日涨幅: 中位数 {b['next_pct'].median():+.2f}%  均值 {b['next_pct'].mean():+.2f}%  "
            f"胜率 {(b['next_pct'] > 0).mean() * 100:.0f}%  最大 {b['next_pct'].max():+.2f}%  最小 {b['next_pct'].min():+.2f}%"
        )
        print(
            f"第3日涨幅: 中位数 {b['next2_pct'].median():+.2f}%  均值 {b['next2_pct'].mean():+.2f}%  "
            f"胜率 {(b['next2_pct'] > 0).mean() * 100:.0f}%"
        )
    if len(s) and s["pct_chg"].notna().any():
        print(
            f"卖出当日涨幅中位数: {s['pct_chg'].median():+.2f}%  "
            f"(当日仍上涨/涨停 {(s['pct_chg'] > 0).sum()} 笔 → 高位派发特征)"
        )

    spans = []
    for code, sub in m.sort_values("trade_date").groupby("ts_code"):
        buy_days = sub[sub["buy"] > 0]["trade_date"].tolist()
        sell_days = sub[sub["sell"] > 0]["trade_date"].tolist()
        for bd in buy_days:
            later = [d for d in sell_days if d > bd]
            spans.append((code, bd, later[0] if later else None))
    if spans:
        cal = sorted(pro.trade_cal(exchange="SSE", start_date=start, end_date=today, is_open="1")["cal_date"].tolist())
        idx = {d: i for i, d in enumerate(cal)}
        days = [idx[e] - idx[b] for _, b, e in spans if b in idx and e in idx]
        print(f"\n## 持仓节奏（买入日 → 上榜卖出日，{len(spans)} 个买入样本，可配对 {len(days)} 个）")
        if days:
            print(
                f"间隔中位数 {pd.Series(days).median():.0f} 个交易日  分布: "
                + "  ".join(f"T+{i}:{days.count(i)}" for i in sorted(set(days)))
            )
        print(f"区间内未见卖出入榜（仍在持仓 / 卖出未触发上榜）: {sum(1 for *_ , e in spans if e is None)} 笔")

    if args.cooccur:
        print("\n## 同榜出现的其它游资（按共现次数）")
        tally = {}
        for d in sorted(inst["trade_date"].unique()):
            try:
                day = pro.top_inst(trade_date=d)
            except Exception:
                continue
            for _, sub in day.groupby("ts_code"):
                if sub[sub["exalter"].map(norm).isin(alias)].empty:
                    continue
                for ex in set(sub["exalter"]):
                    for w in seat2who.get(norm(ex), set()) - {args.name}:
                        tally[w] = tally.get(w, 0) + 1
            time.sleep(0.16)
        for w, c in sorted(tally.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {w}: {c} 次")

    if args.detail:
        print("\n## 逐笔上榜明细")
        show = m.sort_values("trade_date", ascending=False)
        for _, r in show.iterrows():
            nxt = "—" if pd.isna(r["next_pct"]) else f"{r['next_pct']:+.2f}%"
            cur = "—" if pd.isna(r["pct_chg"]) else f"{r['pct_chg']:+.2f}%"
            print(
                f"  {r['trade_date']} {r['ts_code']} {r['seats']}  买{r['buy'] / 1e4:>9.0f}万 卖{r['sell'] / 1e4:>9.0f}万  "
                f"当日{cur} 次日{nxt}"
            )


if __name__ == "__main__":
    main()
