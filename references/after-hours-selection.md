# 盘后定标的：自算口径配方

适用：收盘后 `limit_list_d` / `kpl_list` / `top_list` 仍为空（约 18:00 后逐接口落地），用户问「按龙头战法今天/明天该做哪只」。全部片段只依赖 `daily` + `daily_basic` + `trade_cal` + **前一交易日**的 `kpl_list`。

## 0. 先确认池子没落地

```python
for api in ('daily', 'limit_list_d', 'kpl_list', 'top_list'):
    print(api, len(getattr(pro, api)(trade_date=today)))
```

`daily` ~5500 行而其余为 0 → 走自算，报告头写「板数由 daily 自算，封单/首封时间/席位待补」。**不要**用 `--fallback-recent` 让脚本静默回退到上一交易日。

## 0.1 取数字段坑（写脚本前先 `print(list(df.columns))`）

- **`limit_list_d` 要按 `limit_type` 分取**：不带参数只回 `limit_type='U'`（涨停），而且返回表里**没有 `limit_type` 列** —— `L[L.limit_type=='U']` 直接 AttributeError；跌停/炸板会静默算成 0，炸板率跟着失真。跌停 `'D'`、炸板 `'Z'` 单独取，缓存按 `{date}_{type}.pkl` 分文件（共用一个文件名会被不同类型互相覆盖）。
- **`top_inst` 的列是 `buy` / `sell` / `net_buy` / `buy_rate` / `sell_rate` / `side` / `reason`**，没有 `l_buy`/`l_sell`；同一 (席位, 方向, 上榜原因) 会重复行，先按 `(ts_code, exalter, side, reason)` 去重再加总。
- **`top_inst.side` 是字符串 `'0'`/`'1'`，不是整型**：`df[df.side == 0]` 不报错、直接返回空表，看起来像「该股买方没有游资」。要么 `astype(str)`，要么直接拿 `'0'`/`'1'` 比。
- **`top_inst` 的 `buy` / `sell` 只有一侧有值，另一侧是 `NaN`**：按 `(buy - sell)` 算净额再排序，大额席位会整列变 `nan` 并被排到末尾——报出来的「买方前五」实际是空。买方榜按 `buy` 排序、卖方榜按 `sell` 排序分别看；要算净额先 `fillna(0)`。
- **`disclosure_date` 只回 `ts_code / ann_date / end_date / pre_date / actual_date`**，没有 `modify_date`；预约日取 `pre_date`。季报/年报预约日常落在**周六**，「披露前 1-2 个交易日清仓」要按日历反推——周六披露等于周五收盘前必须清，别把周六读成「还有两天」。
- **连板列别命名为 `lt` 后直接用 `.lt`**：`df.lt` 是 DataFrame 的方法，`lad[lad.lt>=2]` 会 TypeError，一律写 `df['lt']`。
- **席位反查用 `hm_list.orgs` 的关键词只是候选**：单检索 `彩田路` 会同时命中「红岭路」和「温州帮」（百余席大包），`西大街` 会命中「量化打板」。必须落到具体席位全称再报人名，否则会把百席大包里的任意营业部说成某个顶级游资。**归一化后只做正向包含 `o in tn`（席位全称里含名录简称），不要做反向 `tn in o`**——短名会命中长席位的任意片段，`华泰证券深圳彩田路` 就是这样被误判成「红岭路」的。
- **第三方涨停天梯（同花顺/东财）与 Tushare 池子会差 1-2 家**（连板计数口径不同）：报家数只用 `limit_list_d` 并写清口径，别把两个来源的数字混进同一句话。

## 1. 情绪序列（0 token）

```bash
python3 <a-share-limit-up-speed>/scripts/reconstruct_emotion.py --date today --days 8
```

看四组相对量，不看单日绝对值：最高板 / 炸板率 / 昨涨停溢价中位 / 连板晋级率。

## 2. 板数：沿完整交易日历往回数

```python
def boards(code, day, days):        # days = trade_cal(is_open=1) 升序
    n = 0
    for d in reversed([x for x in days if x <= day]):
        if code in up_set[d]:
            n += 1
        else:
            break
    return n
```

`up_set[d]` 由「`close` ≥ `stk_limit.up_limit`」得出；无 `stk_limit` 权限才退回阈值法（主板 10%/创业科创 20%/北交所 30%），并在输出里标明用的哪种。当日池子为空时没有 `limit_times` 可用，也不能只在「有记录的日期」上 `shift`。

## 3. 板块广度 + 板块晋级率（`reconstruct_emotion.py` 不出的两项）

- **广度**：涨停集按 `stock_basic.industry` 计数，列近 3 日看增减。家数连续放大 + 长出 2 板以上 = 主线形成；家数 15→10→3 这种衰减 = 主线在死。
- **新方向的梯队往往还没形成，数涨停家数会把它排到最后**：旧主线崩塌日，资金新选的方向可能只有 1 家涨停、其余是 4%-12% 的大涨（半导体/元器件一片跌停的那天，逆势行业里往往只有一只封板）。这时按「行业均值涨幅 + 上涨家数 + 行业成交额」三列扫全市场行业，**唯一均值翻红的成长行业**就是换仓方向（银行/保险/煤炭也会翻红，按「是不是成长行业」把它排掉，它们是风格切换不是主线）。命中后再逐只核链上有没有大票同时在放量——该股进全市场成交额前列且涨幅 >10% 才算「资金真进场」；只有行业均值翻红而链上无一只大票放量，写「超跌反抽」而不是「主线切换」。
- **晋级率**：昨日涨停集 join 今日 `pct_chg`，按行业 `groupby` 算「再涨停家数 / 家数」，再与全市场比。

```python
y = 昨日涨停集.copy()
y['t'] = y.ts_code.map(今日pct_chg)
g = y.groupby('ind').agg(n=('t', 'size'), 均涨=('t', 'mean'), 中位=('t', 'median'),
                         再涨停=('t', lambda s: int((s >= 9.7).sum())))
```

板块晋级率显著高于全市场且仍有批量首板 = 主线切换；两者都低 = 真退潮。同一批数字顺带产出全市场昨涨停溢价（均涨/中位）与晋级率。

## 4. 给连板取名：用前一交易日的 `kpl_list`

今日 `kpl_list` 为空时，`kpl_list(trade_date=上一交易日)` 的 `theme` / `lu_desc` 覆盖了今天 2 板以上票的绝大多数（今天 3-4 板的票昨天基本都上过榜），逐一对号，比用申万行业名猜题材准得多：

```python
k = pro.kpl_list(trade_date=prev_day, tag='涨停')
k = k[k.ts_code.isin(今日连板代码)]
```

同时读 `status`（几天几板，`limit_times` 会漏间隔式涨停）、`turnover_rate` 与 `free_float`（自由流通换手，判筹码是否被一天换光）、`lu_limit_order`（封单，元）。

## 4.5 判身份：这只票是板块龙还是情绪龙

**只看当日的 `lu_desc` + 申万 `industry` 一定不够**：前者是当天资金找的由头，后者是分类残留。要回看 10 个交易日：

```python
for d in past_10_trade_days:
    k = pro.kpl_list(trade_date=d, tag='涨停')
    if k is None or not len(k):
        continue
    x = k[k.ts_code == code]
    if len(x):
        r = x.iloc[0]
        print(d, r.lu_desc, r.theme, r.status, r.lu_time, r.turnover_rate)   # status = 几天几板

l = pro.limit_list_d(trade_date=昨, limit_type='U')      # up_stat 是身份的硬证据
print(l[l.ts_code == code][['up_stat', 'limit_times', 'industry']])
```

判据（命中两条以上即按情绪龙处理）：

- `status` / `up_stat` 显示**长周期高频涨停**（形如 `7/12`＝12 天 7 板、`6连板`）；
- `lu_desc` / `theme` **逐日漂移**（机器人→平台经济→家电一路换），说明无板块归属；
- 出现**断板后高位横盘 4-5 日不死、再涨停**（二波结构）；
- `turnover_rate_f` 连续多日 35%-60%；
- 区间涨幅已达 +80%~+150% 且公司发过异动澄清公告、市场照涨。

判成情绪龙后，行业广度与高换手都不再是利空（见 SKILL.md 第一节角色对照表）；改用「情绪温度计」口径：它封板/断板定全场档位，自己只做短打 ≤20%。

## 4.6 财报披露日：先查，再谈买点

情绪龙/妖股多半业绩与估值背离，定期报告披露日是比任何形态都优先的硬约束：

- **先用 `actual_date` 两分，再谈形态**：`pro.disclosure_date(ts_code=..., end_date=<报告期>)` 里 `pre_date` 是预约日、`actual_date` 为空表示**尚未披露**。同一批候选里这时会出现两种结论完全相反的票，混着写必然给反结论：
  - `actual_date` 有值（已披露，尤其**今天刚披露就涨停**）→ 财报关已解除，可以进次日候选。刚披露就涨停说明业绩被市场当场确认，是业绩驱动而非纯情绪，不该再拿「高标不过夜」一刀切掉。
  - `pre_date` 落在**下一个交易日**（`actual_date` 为空）→ 硬否决，无论板数多高、封得多硬。高板 + 天量换手 + 次日披露三条并列，不需要预判业绩好坏就足够出局。
  - 同理，**昨最高标当天恰是披露日 = 断板与财报关叠加**，这种断板不能读成普通分歧，次日直接按 A 杀对待。
- 取公告口径的披露日（`web_search`「股票名 + 三季报/年报 + 披露日期」，或读交易所异动公告原文），确认是周几；周五盘后或周六披露 = 本周五收盘前必须清仓；
- 叠加检查：半年报/季报营收与净利同比、是否单季转亏、动态 PE 与行业均值差、机构目标均价与现价的差距。四条里三条不利 → 仓位再砍半，且不过夜。

## 4.7 一只票能不能做：四个硬数字（先算，再写结论）

被问「XX 呢 / 明天做哪只」时不要先铺技术分析。这四个数算出来，前两条命中就直接进不碰清单：

```python
d = pro.daily(ts_code=code, start_date=区间起点, end_date=今日).sort_values('trade_date')
区间涨幅 = d.close.iloc[-1] / d.low.min() - 1        # 起点用区间最低价，不是最低收盘
b = pro.daily_basic(ts_code=code, trade_date=今日)
自由换手, 流通亿 = b.turnover_rate_f.iloc[0], b.circ_mv.iloc[0] / 1e4
f = pro.fina_indicator(ts_code=code, start_date='年初', end_date='今日',
                       fields='end_date,netprofit_yoy,or_yoy,grossprofit_margin')
封单占比 = U.loc[U.ts_code == code, 'fd_amount'].iloc[0] / U.loc[U.ts_code == code, 'amount'].iloc[0]
```

| 数字 | 硬线 | 口径坑 |
|---|---|---|
| 区间涨幅 | >100% 且净利润同比为负 = 纯题材高标，不碰 | 起点取**区间最低价**，取最低收盘能差出 10 个点 |
| 自由流通换手 | ≥65% = 死亡换手，不碰 | 必须 `turnover_rate_f`；`turnover_rate` 会把 67% 显示成 33%，漏判一半 |
| 净利润同比 | `netprofit_yoy` 为负 + 涨幅 >100% → 硬否决 | 只认 `fina_indicator` 已披露报表；业绩预告/快报不能替 |
| 封单/成交额 | ≥8% 才算合格容量龙 | 同日横截面看：非核心票普遍 3%-6%，5.4%~5.7% 的次日就是分歧。封住了 ≠ 封得硬 |

封单占比只能**同日排序**比，不要跨日比绝对值——情绪好的日子整体封单都厚。

## 5. 个股定位

`daily_basic`（`turnover_rate`、`turnover_rate_f`、`volume_ratio`、`circ_mv`）+ `cyq_perf`（`winner_rate`、`cost_5pct/95pct`，判上方套牢区）+ 近 6-12 日 `daily`（首板日是冰点逆势还是顺势、是不是一字/缩量、当日低点破了没有）。历史席位用前几日的 `top_list`/`top_inst`；今日席位 18:00 后才有，缺失就写「席位待补」，不要用「没上榜」推断资金干净。

## 6. 催化面

`web_search` 查「股票名 + 涨停原因 / 澄清 / 异动」：题材归属、公司是否澄清「相关业务收入占比低」、是否提示非理性炒作、是否收到异动问询。**澄清 + 非理性炒作提示 = 高危信号**，必须进风险栏。这是检索口径，不受「行情数据只走 Tushare」的约束。反过来，**澄清之后照样涨停 = 情绪龙的确认信号**（公司基本面证伪无效，纯人气博弈），此时不要再拿基本面否定它的走势，只在风险栏写明估值与业绩背离。

## 输出顺序

环境（周期档位 + 指数是否配合 → 仓位区间）→ 主线排序（涨停家数 / 连板梯队 / 大票放量，并指出谁在派发）→ **角色识别（当日有无情绪龙，单列「情绪温度计」一栏）** → 龙头（五要素逐条过，缺哪条直说）→ 一只主攻 + 唯一买法（竞价与分时阈值 + 可试仓/不打/不做三分支）→ 备选（带确认条件，不给预测）→ 财报/公告硬约束 → 不碰清单 → 免责。
