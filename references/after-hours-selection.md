# 盘后定标的：自算口径配方

适用：收盘后 `limit_list_d` / `kpl_list` / `top_list` 仍为空（约 18:00 后逐接口落地），用户问「按龙头战法今天/明天该做哪只」。全部片段只依赖 `daily` + `daily_basic` + `trade_cal` + **前一交易日**的 `kpl_list`。

## 0. 先确认池子没落地

```python
for api in ('daily', 'limit_list_d', 'kpl_list', 'top_list'):
    print(api, len(getattr(pro, api)(trade_date=today)))
```

`daily` ~5500 行而其余为 0 → 走自算，报告头写「板数由 daily 自算，封单/首封时间/席位待补」。**不要**用 `--fallback-recent` 让脚本静默回退到上一交易日。

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

- 取公告口径的披露日（`web_search`「股票名 + 三季报/年报 + 披露日期」，或读交易所异动公告原文），确认是周几；周五盘后或周六披露 = 本周五收盘前必须清仓；
- 叠加检查：半年报/季报营收与净利同比、是否单季转亏、动态 PE 与行业均值差、机构目标均价与现价的差距。四条里三条不利 → 仓位再砍半，且不过夜。

## 5. 个股定位

`daily_basic`（`turnover_rate`、`turnover_rate_f`、`volume_ratio`、`circ_mv`）+ `cyq_perf`（`winner_rate`、`cost_5pct/95pct`，判上方套牢区）+ 近 6-12 日 `daily`（首板日是冰点逆势还是顺势、是不是一字/缩量、当日低点破了没有）。历史席位用前几日的 `top_list`/`top_inst`；今日席位 18:00 后才有，缺失就写「席位待补」，不要用「没上榜」推断资金干净。

## 6. 催化面

`web_search` 查「股票名 + 涨停原因 / 澄清 / 异动」：题材归属、公司是否澄清「相关业务收入占比低」、是否提示非理性炒作、是否收到异动问询。**澄清 + 非理性炒作提示 = 高危信号**，必须进风险栏。这是检索口径，不受「行情数据只走 Tushare」的约束。反过来，**澄清之后照样涨停 = 情绪龙的确认信号**（公司基本面证伪无效，纯人气博弈），此时不要再拿基本面否定它的走势，只在风险栏写明估值与业绩背离。

## 输出顺序

环境（周期档位 + 指数是否配合 → 仓位区间）→ 主线排序（涨停家数 / 连板梯队 / 大票放量，并指出谁在派发）→ **角色识别（当日有无情绪龙，单列「情绪温度计」一栏）** → 龙头（五要素逐条过，缺哪条直说）→ 一只主攻 + 唯一买法（竞价与分时阈值 + 可试仓/不打/不做三分支）→ 备选（带确认条件，不给预测）→ 财报/公告硬约束 → 不碰清单 → 免责。
