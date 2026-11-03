#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""情绪总龙扫描：不按连板高度排名，按「辨识度」四条实证线索找市场情绪总龙。

四条判据（见 SKILL.md 第一节「先认角色，再选尺子」）：
  1. 高频涨停基因：窗口内涨停次数 / up_stat（如 7/12 = 12 天 7 板）
  2. 断板不死：每次断板后 3-5 日内是否反包、断板后 5 日最大涨幅
  3. 容量与人气：成交额全市场排名、自由流通换手
  4. 空间：窗口累计涨幅、是否仍在 MA10 上方、距区间高点

用法：
  python3 screen_emotion_dragon.py --days 45 --top 15
  python3 screen_emotion_dragon.py --days 45 --detail 001219.SZ,002172.SZ

输出：候选表（按辨识度分排序）+ 逐只细节（涨停日序列 / 断板事件与反包结果 / 成交额排名序列）
缓存：~/.hermes/cache/emotion/（逐日 limit_list_d + daily，重跑秒出）
"""
import argparse
import datetime as dt
import os
import sys
import time
import warnings

try:
    import pandas as pd
    import tushare as ts
except ModuleNotFoundError:
    _ALT = "/usr/local/bin/python3"
    if os.path.exists(_ALT) and not os.environ.get("_EMO_REEXEC"):
        os.environ["_EMO_REEXEC"] = "1"
        os.execv(_ALT, [_ALT, os.path.abspath(__file__), *sys.argv[1:]])
    raise

warnings.filterwarnings("ignore")
CACHE = os.path.expanduser("~/.hermes/cache/emotion")


def pro_api():
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        try:
            with open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("TUSHARE_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    if not tok:
        raise SystemExit("TUSHARE_TOKEN 未设置")
    os.environ["TUSHARE_TOKEN"] = tok
    return ts.pro_api(tok)


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
            time.sleep(62 if "频率" in str(exc) or "每分钟" in str(exc) else 2 + i * 2)
    return pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--end", default=None)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--detail", default=None, help="逗号分隔的 ts_code，打逐只细节")
    args = ap.parse_args()

    pro = pro_api()
    end = args.end or dt.date.today().strftime("%Y%m%d")
    start = (dt.date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}") - dt.timedelta(days=args.days * 2 + 20)).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    days = sorted(cal[cal.is_open == 1].cal_date.tolist())[-args.days:]
    print(f"窗口 {days[0]}~{days[-1]}（{len(days)} 个交易日）")

    pools, dailies = {}, {}
    for d in days:
        u = load(pro, "U", d, lambda d=d: pro.limit_list_d(trade_date=d, limit_type="U"))
        if len(u):
            u["lt"] = pd.to_numeric(u["limit_times"], errors="coerce").fillna(0).astype(int)
            pools[d] = u
        dl = load(pro, "daily", d, lambda d=d: pro.daily(trade_date=d))
        if len(dl):
            dailies[d] = dl

    # 全市场成交额排名（每日）
    rank = {}
    for d, dl in dailies.items():
        s = dl.set_index("ts_code")["amount"].rank(ascending=False)
        rank[d] = s
    # 面板：收盘价
    close = {}
    for d, dl in dailies.items():
        for c, v in zip(dl["ts_code"], dl["close"]):
            close[(c, d)] = v

    # 每只股票的涨停日集合
    lu_days = {}
    for d, u in pools.items():
        for c in u["ts_code"]:
            lu_days.setdefault(c, []).append(d)

    rows, breaks_by_code = [], {}
    for c, ds in lu_days.items():
        if len(ds) < 2:
            continue
        ds = sorted(ds)
        seq = [days.index(x) for x in ds]
        # 最长连续涨停
        best = run = 1
        for i in range(1, len(seq)):
            run = run + 1 if seq[i] == seq[i - 1] + 1 else 1
            best = max(best, run)
        # 当前连板：只有最后一个涨停就发生在窗口最后一个交易日，才算「当前」连板
        cur_run = 0
        if ds[-1] == days[-1]:
            cur_run = 1
            for i in range(len(seq) - 1, 0, -1):
                if seq[i] == seq[i - 1] + 1:
                    cur_run += 1
                else:
                    break
        last_lu = (len(days) - 1) - days.index(ds[-1])
        # 断板事件：涨停日之后那天没涨停
        breaks = []
        for i, d in enumerate(ds):
            k = days.index(d)
            if k + 1 < len(days):
                nxt = days[k + 1]
                if nxt not in lu_days.get(c, []):
                    fwd = [close.get((c, days[j])) for j in range(k + 1, min(k + 6, len(days)))]
                    base = close.get((c, d))
                    if base and any(fwd):
                        peak = max([x for x in fwd if x] or [base])
                        re_seal = any(days[j] in lu_days.get(c, []) for j in range(k + 1, min(k + 6, len(days))))
                        breaks.append({"date": d, "peak_pct": (peak / base - 1) * 100, "rebound": re_seal})
        alive = sum(1 for b in breaks if b["rebound"])
        # 涨幅 / 位置
        c0 = close.get((c, days[0]))
        c1 = close.get((c, days[-1]))
        chg = (c1 / c0 - 1) * 100 if c0 and c1 else None
        cl = [close.get((c, d)) for d in days]
        cl = [x for x in cl if x]
        ma10 = sum(cl[-10:]) / len(cl[-10:]) if len(cl) >= 10 else None
        above_ma10 = (c1 / ma10 - 1) * 100 if ma10 else None
        # 成交额
        amts = []
        for d in days[-5:]:
            if d in dailies and c in set(dailies[d]["ts_code"]):
                amts.append(dailies[d].set_index("ts_code").loc[c, "amount"] / 1e5)
        amt5 = sum(amts) / len(amts) if amts else 0
        rk = rank[days[-1]].get(c) if days[-1] in rank else None
        u_last = pools.get(days[-1])
        up_stat = None
        if u_last is not None and c in set(u_last["ts_code"]):
            up_stat = u_last.set_index("ts_code").loc[c, "up_stat"]
        rows.append({
            "code": c, "name": (pools[ds[-1]].set_index("ts_code").loc[c, "name"] if c in set(pools[ds[-1]]["ts_code"]) else ""),
            "industry": (pools[ds[-1]].set_index("ts_code").loc[c, "industry"] if c in set(pools[ds[-1]]["ts_code"]) else ""),
            "lu_n": len(ds), "cur_run": cur_run, "max_run": best, "up_stat": up_stat, "last_lu": last_lu,
            "chg": chg, "amt5": amt5, "rank": rk, "above_ma10": above_ma10,
            "breaks_n": len(breaks), "rebound_n": alive,
            "rebound_rate": alive / len(breaks) * 100 if breaks else None,
        })
        breaks_by_code[c] = breaks

    df = pd.DataFrame(rows)
    if not len(df):
        print("窗口内没有连续出现的涨停股")
        return 0
    # 辨识度分：高频涨停 + 连板高度 + 断板存活 + 成交额排名 + 空间
    df["score"] = (
        df["lu_n"].clip(0, 12) * 6
        + df["max_run"].clip(0, 8) * 4
        + df["rebound_rate"].fillna(0) * 0.25
        + (df["rank"].fillna(3000).rsub(1200).clip(lower=0) / 100)
        + (df["chg"].fillna(0).clip(0, 150) / 10)
    )
    df = df.sort_values("score", ascending=False)
    show = df.head(args.top).copy()
    show["chg"] = show["chg"].round(1)
    show["amt5"] = show["amt5"].round(1)
    show["above_ma10"] = show["above_ma10"].round(1)
    show["rebound_rate"] = show["rebound_rate"].round(0)
    show["rank"] = show["rank"].astype("Int64")
    show["score"] = show["score"].round(1)
    show = show[["code", "name", "industry", "lu_n", "cur_run", "max_run", "last_lu", "up_stat",
                 "chg", "amt5", "rank", "above_ma10", "breaks_n", "rebound_n", "rebound_rate", "score"]]
    show.columns = ["代码", "名称", "行业", "涨停次数", "当前连板", "最长连板", "距末次涨停天",
                    "up_stat", "窗口涨幅%", "5日均额亿", "今日成交额排名", "距MA10%",
                    "断板次数", "反包次数", "反包率%", "辨识度分"]
    print("\n== 情绪总龙候选（按辨识度分排序，不按板高）==")
    print(show.to_string(index=False))

    if args.detail:
        for c in [x.strip() for x in args.detail.split(",") if x.strip()]:
            r = df[df["code"] == c]
            if not len(r):
                print(f"\n{c}: 窗口内不在候选里")
                continue
            r = r.iloc[0]
            print(f"\n== {c} {r['name']}（{r['industry']}）==")
            print(f"  涨停 {r['lu_n']}/{len(days)} 天｜当前 {r['cur_run']} 连板｜最长 {r['max_run']}｜up_stat {r['up_stat']}")
            print(f"  窗口涨幅 {r['chg']:+.1f}%｜距 MA10 {r['above_ma10']:+.1f}%｜5日均额 {r['amt5']:.1f}亿｜今日成交额排名 {r['rank']}")
            print(f"  断板 {r['breaks_n']} 次，其中 5 日内反包 {r['rebound_n']} 次（{r['rebound_rate']:.0f}%）")
            for b in breaks_by_code.get(c, []):
                print(f"    断板 {b['date']} → 之后 5 日最高 {b['peak_pct']:+.1f}%｜{'已反包' if b['rebound'] else '未反包'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
