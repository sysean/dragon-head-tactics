#!/usr/local/bin/python3
# -*- coding: utf-8 -*-
"""首板一年样本的特征分析（纯 pandas/numpy，不依赖 sklearn）。

口径：T+1 开盘价买入；胜负 = T1_win（T+1 收盘>买价）/ T2_win（T+2 收盘>买价）/ T2_intraday_win（T+2 最高>买价）。
做什么：
  1. 基线（全样本 + 按月）
  2. 单特征分箱胜率（五分位 + 关键阈值），按「T2_win 极差」与 Spearman(T2_ret) 排序
  3. 现行 9:25 打分规则在新样本上的表现（档位 / 硬门槛）
  4. 两特征交叉
  5. 时间切分的逻辑回归（numpy IRLS）+ 测试集十分位
  6. 规则搜索（单/双条件，Wilson 下界排序，训练/测试一致才留）
用法：python3 analyze_first_board_features.py [--csv path] [--out report.md] [--label T2_win]
"""
import os
import argparse
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
pd.set_option('display.width', 220)


def _fmt(v):
    if isinstance(v, float):
        if v != v:
            return ''
        return f'{v:.2f}' if abs(v) < 1000 else f'{v:.0f}'
    return str(v)


def md(df, index=True):
    """不依赖 tabulate 的 markdown 表格。"""
    if isinstance(df, pd.Series):
        df = df.to_frame()
    d = df.reset_index() if index else df
    cols = [str(c) for c in d.columns]
    lines = ['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)]
    for _, r in d.iterrows():
        lines.append('| ' + ' | '.join(_fmt(v) for v in r.tolist()) + ' |')
    return '\n'.join(lines)


pd.DataFrame.to_markdown = lambda self, index=True, **k: md(self, index)
pd.Series.to_markdown = lambda self, index=True, **k: md(self, index)

LABELS = ['T1_win', 'T1_intraday_win', 'T2_win', 'T2_intraday_win', 'T3_win']
RETS = ['T1_ret', 'T2_ret', 'T2_max_ret', 'T3_ret']

# 9:25 可知的特征（只用这些做预测；T+1 盘后字段只作对照）
FEATS_925 = [
    'au_gap', 'au_ratio_prev', 'au_turnover', 'au_volume_ratio', 'au_amount_yi', 'au_float_pct',
    'au_mkt_red', 'au_mkt_med', 'au_pool_red', 'au_pool_med',
    'lu_amount', 'lu_fd_pct', 'lu_turnover', 'lu_first_min', 'lu_last_min', 'lu_open_times', 'lu_float_mv_yi',
    'b_volume_ratio', 'vr5', 'vr20', 'amt_vs_max20', 'b_turnover_rate_f', 'mkt_share_pct',
    'd_gap_T', 'd_low_pct_T', 'd_amp_T', 'prev_pct', 'prev2_pct', 'up_days',
    'dev_ma5', 'dev_ma10', 'dev_ma20', 'dev_ma60', 'dist_hi60', 'dist_lo60', 'dist_hi20',
    'ret5', 'ret10', 'ret20', 'ret60', 'zt20', 'zt60', 'touch20',
    'ind_cnt', 'ind_max_board', 'ind_first_cnt', 'theme_cnt_max', 'theme_board_max', 'peer_cnt', 'peer_board_max',
    'mkt_n_up', 'mkt_n_zha', 'mkt_zha_rate', 'mkt_max_board', 'mkt_n_first', 'mkt_n_2plus', 'mkt_n_dt', 'mkt_amount_yi',
    'mkt_med_pct', 'mkt_red_pct', 'mkt_bigdrop', 'mkt_promo_first', 'mkt_promo_all',
    'mf_net_lg_pct', 'mf_net_pct', 'cyq_winner_rate', 'cyq_close_vs_avg',
    'tf_rsi_6', 'tf_rsi_12', 'tf_kdj_j', 'tf_macd', 'tf_cci',
    'top_list', 'top_net_rate', 'b_pe_ttm', 'b_pb', 'listed_days',
]
# 关键阈值（与 SKILL 现行规则对齐，便于直接对照）
FIXED_BINS = {
    'au_gap': [-99, 0, 2, 3, 4, 5, 6, 7, 9, 99],
    'au_ratio_prev': [0, 1, 2, 3, 5, 8, 12, 20, 35, 1e9],
    'b_volume_ratio': [0, 1, 1.5, 1.8, 2, 3, 5, 7, 1e9],
    'vr5': [0, 1, 1.5, 1.8, 2, 3, 5, 7, 1e9],
    'lu_amount': [0, 1e8, 2e8, 3e8, 5e8, 10e8, 20e8, 1e12],
    'lu_fd_pct': [0, 5, 10, 20, 40, 80, 1e9],
    'lu_turnover': [0, 3, 5, 10, 15, 20, 30, 1e9],
    'lu_first_min': [-1, 0, 5, 15, 30, 60, 120, 180, 240],
    'lu_open_times': [-1, 0, 1, 2, 3, 99],
    'peer_cnt': [-1, 1, 2, 4, 6, 10, 999],
    'peer_board_max': [-1, 1, 2, 3, 4, 99],
    'dev_ma10': [-99, 0, 5, 10, 15, 20, 30, 40, 999],
    'dist_hi60': [-99, -30, -20, -10, -5, -1e-9, 999],
    'mkt_n_up': [0, 30, 40, 50, 60, 80, 100, 9999],
    'mkt_promo_first': [-1, 10, 15, 20, 25, 30, 101],
    'au_mkt_red': [-1, 30, 40, 50, 60, 70, 101],
    'au_turnover': [0, 0.15, 0.3, 0.5, 0.8, 1.2, 2, 999],
    'au_volume_ratio': [0, 1, 1.5, 2, 3, 5, 8, 999],
    'cyq_winner_rate': [-1, 50, 70, 85, 95, 101],
    'mkt_share_pct': [0, 0.005, 0.01, 0.02, 0.04, 0.08, 9],
}


def wilson_low(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    return (p + z * z / (2 * n) - z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def spearman(x, y):
    m = x.notna() & y.notna()
    if m.sum() < 30:
        return np.nan
    return x[m].rank().corr(y[m].rank())


def auc(score, y):
    s = pd.Series(score).rank().values; y = np.asarray(y)
    n1 = y.sum(); n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    return (s[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def bin_table(df, col, label, bins=None, q=5):
    x = df[col]
    if x.notna().sum() < 50:
        return None
    if bins is not None:
        b = pd.cut(x, bins=bins)
    else:
        try:
            b = pd.qcut(x, q=q, duplicates='drop')
        except Exception:
            return None
    gcols = [label, 'T1_win', 'T2_intraday_win', 'T1_ret', 'T2_ret']
    t = df.groupby(b, observed=True).agg(n=(label, 'size'), **{c: (c, 'mean') for c in gcols})
    for c in {label, 'T1_win', 'T2_intraday_win'}:
        t[c] = (t[c] * 100).round(1)
    t['T1_ret'] = t['T1_ret'].round(2); t['T2_ret'] = t['T2_ret'].round(2)
    t = t[t.n >= 20]
    return t


def logit_irls(X, y, l2=1.0, iters=50):
    X = np.column_stack([np.ones(len(X)), X]); w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w)); W = p * (1 - p) + 1e-9
        H = X.T @ (X * W[:, None]) + l2 * np.eye(X.shape[1]); H[0, 0] -= l2
        g = X.T @ (y - p) - l2 * np.r_[0, w[1:]]
        step = np.linalg.solve(H, g); w += step
        if np.abs(step).max() < 1e-6:
            break
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=os.path.expanduser('~/.hermes/cache/firstboard/first_board_1y.csv'))
    ap.add_argument('--out', default=os.path.expanduser('~/.hermes/cache/firstboard/first_board_1y_report.md'))
    ap.add_argument('--label', default='T2_win')
    ap.add_argument('--split', default=None, help='训练/测试切分日（默认按样本 2/3 处）')
    a = ap.parse_args()
    L = a.label
    O = []
    w = O.append

    D = pd.read_csv(a.csv, dtype={'trade_date': str, 'T1_date': str, 'n1_date': str, 'n2_date': str, 'n3_date': str})
    w(f'# 首板一年样本特征分析（{D.trade_date.min()} ~ {D.trade_date.max()}）\n')
    w(f'原始样本 {len(D)} 条。主标签 **{L}**；买价 = T+1 开盘价。')
    # 可执行样本
    U = D[(D.is_st == 0) & (D.board_type.isin(['10cm', '20cm'])) & (D.listed_days >= 60) & (D.t1_date_ok == 1)
          & D.buy.notna() & D.n2_close.notna()].copy()
    yizi_T1 = U[U.t1_tradable == 0]
    U = U[U.t1_tradable == 1].copy()
    U['ym'] = U.trade_date.str[:6]
    w(f'过滤后（剔 ST/北交所、上市<60 日、停牌错位、T+2 未出、T+1 一字开 {len(yizi_T1)} 只买不到）：**可执行样本 {len(U)} 条，{U.trade_date.nunique()} 个交易日，日均 {len(U)/U.trade_date.nunique():.1f} 只**\n')

    # ---- 1. 基线 ----
    w('## 一、基线\n')
    base = {k: U[k].mean() * 100 for k in LABELS}
    w('| 指标 | 值 |\n|---|---|')
    for k in LABELS:
        w(f'| {k} 胜率 | {base[k]:.1f}% |')
    for k in RETS:
        w(f'| {k} 均值 | {U[k].mean():+.2f}% （中位 {U[k].median():+.2f}%） |')
    w(f'| T+1 封板率 | {U.T1_seal.mean()*100:.1f}% |')
    w(f'| T+1 触板率 | {U.T1_touch.mean()*100:.1f}% |')
    w('')
    w('按月（看稳定性，别拿单月说事）：\n')
    m = U.groupby('ym').agg(n=(L, 'size'), T1_win=('T1_win', 'mean'), T2_win=('T2_win', 'mean'), T2_intraday=('T2_intraday_win', 'mean'),
                            T1_ret=('T1_ret', 'mean'), T2_ret=('T2_ret', 'mean'), 封板=('T1_seal', 'mean'), 涨停家数=('mkt_n_up', 'mean'))
    for c in ('T1_win', 'T2_win', 'T2_intraday', '封板'):
        m[c] = (m[c] * 100).round(1)
    m['T1_ret'] = m['T1_ret'].round(2); m['T2_ret'] = m['T2_ret'].round(2); m['涨停家数'] = m['涨停家数'].round(0)
    w(m.to_markdown()); w('')

    # ---- 2. 单特征 ----
    w('## 二、单特征分箱（9:25 可知的特征）\n')
    w(f'每个特征按五分位（或固定阈值）分箱，列出样本数、{L} 胜率、T1 胜率、T2 盘中胜率、T1/T2 均值收益。'
      '排序依据：分箱胜率极差（最高箱 − 最低箱）；并给 Spearman(特征, T2_ret)。只列极差 ≥6 个百分点的。\n')
    feats = [f for f in FEATS_925 if f in U.columns and U[f].notna().sum() >= 200]
    scores = []
    tables = {}
    for f in feats:
        t = bin_table(U, f, L, bins=FIXED_BINS.get(f))
        if t is None or len(t) < 2:
            continue
        spread = float(t[L].max() - t[L].min())
        rho = spearman(U[f], U['T2_ret'])
        rho1 = spearman(U[f], U['T1_ret'])
        scores.append((f, spread, rho, rho1, int(t.n.sum())))
        tables[f] = t
    SC = pd.DataFrame(scores, columns=['feature', f'{L}极差pct', 'spearman_T2_ret', 'spearman_T1_ret', 'n']).sort_values(f'{L}极差pct', ascending=False)
    w('### 2.1 特征排行\n'); w(SC.round(3).to_markdown(index=False)); w('')
    w('### 2.2 分箱明细（极差 ≥6pct 的特征）\n')
    for f, spread, rho, rho1, n in SC.values:
        if spread < 6:
            continue
        w(f'**{f}**（极差 {spread:.1f}pct，ρ(T2_ret)={rho:+.3f}）\n'); w(tables[f].to_markdown()); w('')

    # ---- 3. 现行 9:25 打分在新样本上的表现 ----
    w('## 三、现行 9:25 打分规则（满分 10）在本样本上的表现\n')
    V = U.copy()
    ratio = V.b_volume_ratio.where(V.b_volume_ratio > 0, V.vr5)
    V['ratio'] = ratio
    gate = (V.lu_amount >= 1e8) & (ratio >= 1.8) & (V.au_ratio_prev >= 2)
    sc = pd.Series(0, index=V.index)
    pc = V.peer_cnt
    sc += np.where((pc >= 5) & (pc <= 10), 2, np.where((pc >= 3) & (pc <= 4), 1, np.where(pc > 10, 1, 0)))
    sc += np.where(V.peer_board_max >= 2, 2, 0)
    vp = V.au_ratio_prev
    sc += np.where((vp >= 8) & (vp <= 20), 3, np.where((vp >= 3) & (vp < 8), 2, np.where(vp <= 35, 1, 0)))
    sc += np.where(ratio >= 7, 1, np.where(ratio >= 3, 2, np.where(ratio >= 2, 1, 0)))
    sc += np.where((V.lu_fd_pct < 10) & (V.lu_turnover >= 10), 1, 0)
    g = V.au_gap
    sc -= np.where(g > 7, 2, np.where(g > 6, 1, np.where(g < 0, 2, 0)))
    sc -= np.where(V.mkt_share_pct < 0.01, 1, 0)
    V['score'] = sc.clip(lower=0)
    V['tier'] = np.where(~gate, 'X(硬门槛剔除)', np.where(V.score >= 8, 'A', np.where(V.score >= 5, 'B', 'C')))
    t = V.groupby('tier').agg(n=(L, 'size'), T1_win=('T1_win', 'mean'), T2_win=('T2_win', 'mean'), T2_intraday=('T2_intraday_win', 'mean'),
                              T1_ret=('T1_ret', 'mean'), T2_ret=('T2_ret', 'mean'), 封板=('T1_seal', 'mean'))
    for c in ('T1_win', 'T2_win', 'T2_intraday', '封板'):
        t[c] = (t[c] * 100).round(1)
    t['T1_ret'] = t['T1_ret'].round(2); t['T2_ret'] = t['T2_ret'].round(2)
    w(t.to_markdown()); w('')
    t2 = V[gate].groupby('score').agg(n=(L, 'size'), T1_win=('T1_win', 'mean'), T2_win=('T2_win', 'mean'), T1_ret=('T1_ret', 'mean'), T2_ret=('T2_ret', 'mean'))
    for c in ('T1_win', 'T2_win'):
        t2[c] = (t2[c] * 100).round(1)
    t2['T1_ret'] = t2['T1_ret'].round(2); t2['T2_ret'] = t2['T2_ret'].round(2)
    w('按分数逐档（过硬门槛的）：\n'); w(t2.to_markdown()); w('')
    # 闸门
    V['gate_open'] = (V.mkt_n_up < 60) | (V.mkt_promo_first < 15)
    t3 = V.groupby(['gate_open', 'tier']).agg(n=(L, 'size'), T2_win=('T2_win', 'mean'), T2_ret=('T2_ret', 'mean'))
    t3['T2_win'] = (t3['T2_win'] * 100).round(1); t3['T2_ret'] = t3['T2_ret'].round(2)
    w('叠加周期闸门（涨停<60 或 晋级率<15% 才开仓）：\n'); w(t3.to_markdown()); w('')
    # 每日只买一只（A 档第一名，同分按竞昨比）
    AA = V[(V.tier == 'A') & V.gate_open].sort_values(['trade_date', 'score', 'au_ratio_prev'], ascending=[True, False, False]).drop_duplicates('trade_date')
    if len(AA):
        w(f'每日只买 A 档第一名（闸门开的日子）：出手 {len(AA)} 天，T1 胜率 {AA.T1_win.mean()*100:.1f}%，T2 胜率 {AA.T2_win.mean()*100:.1f}%，'
          f'T2 盘中胜率 {AA.T2_intraday_win.mean()*100:.1f}%，T1 均 {AA.T1_ret.mean():+.2f}%，T2 均 {AA.T2_ret.mean():+.2f}%，最差 T2 {AA.T2_ret.min():+.1f}%\n')
    else:
        w('每日只买 A 档第一名：本样本无 A 档。\n')

    # ---- 4. 交叉 ----
    w('## 四、两特征交叉（取单特征排行前 4 两两交叉，三分位）\n')
    top4 = [f for f in SC.feature.head(6).tolist() if f in U.columns][:4]
    for i in range(len(top4)):
        for j in range(i + 1, len(top4)):
            f1, f2 = top4[i], top4[j]
            try:
                b1 = pd.qcut(U[f1], 3, duplicates='drop', labels=False); b2 = pd.qcut(U[f2], 3, duplicates='drop', labels=False)
            except Exception:
                continue
            ct = U.groupby([b1, b2])[L].agg(['size', 'mean'])
            ct['mean'] = (ct['mean'] * 100).round(1)
            piv = ct['mean'].unstack(); pivn = ct['size'].unstack()
            w(f'**{f1}（行，低→高）× {f2}（列，低→高）** {L} 胜率 %：\n'); w(piv.to_markdown()); w(''); w('样本数：\n'); w(pivn.to_markdown()); w('')

    # ---- 5. 逻辑回归（时间切分） ----
    w('## 五、逻辑回归（numpy IRLS，L2），训练/测试按时间切分\n')
    cand = [f for f in SC.feature.head(20).tolist() if f in U.columns]
    X = U[cand].copy()
    for c in cand:  # 对重尾特征取对数/裁剪
        if (X[c] > 0).all() and X[c].max() / max(X[c].median(), 1e-9) > 50:
            X[c] = np.log1p(X[c])
        lo, hi = X[c].quantile(0.01), X[c].quantile(0.99)
        X[c] = X[c].clip(lo, hi)
    X = X.fillna(X.median())
    split = a.split or sorted(U.trade_date.unique())[int(U.trade_date.nunique() * 2 / 3)]
    tr = U.trade_date < split; te = ~tr
    mu, sd = X[tr].mean(), X[tr].std().replace(0, 1)
    Xs = ((X - mu) / sd).values; y = U[L].astype(float).values
    wts = logit_irls(Xs[tr], y[tr], l2=2.0)
    p_tr = 1 / (1 + np.exp(-(np.column_stack([np.ones(tr.sum()), Xs[tr]]) @ wts)))
    p_te = 1 / (1 + np.exp(-(np.column_stack([np.ones(te.sum()), Xs[te]]) @ wts)))
    w(f'特征 {len(cand)} 个；训练 {tr.sum()} 条（< {split}），测试 {te.sum()} 条。AUC 训练 {auc(p_tr, y[tr]):.3f} / 测试 {auc(p_te, y[te]):.3f}（0.5 = 随机）。\n')
    coef = pd.DataFrame({'feature': cand, 'coef(标准化)': wts[1:].round(3)}).sort_values('coef(标准化)', key=abs, ascending=False)
    w(coef.to_markdown(index=False)); w('')
    TE = U[te].copy(); TE['p'] = p_te
    TE['decile'] = pd.qcut(TE.p.rank(method='first'), 10, labels=False) + 1
    other = 'T2_win' if L == 'T1_win' else 'T1_win'
    dq = TE.groupby('decile').agg(n=(L, 'size'), **{L: (L, 'mean'), other: (other, 'mean')}, T1_ret=('T1_ret', 'mean'), T2_ret=('T2_ret', 'mean'))
    dq[L] = (dq[L] * 100).round(1); dq[other] = (dq[other] * 100).round(1); dq['T1_ret'] = dq['T1_ret'].round(2); dq['T2_ret'] = dq['T2_ret'].round(2)
    w('测试集按预测概率十分位（10 = 最看好）：\n'); w(dq.to_markdown()); w('')
    TE1 = TE.sort_values(['trade_date', 'p'], ascending=[True, False]).drop_duplicates('trade_date')
    w(f'测试集每日只买预测概率最高的 1 只：{len(TE1)} 天，{L} {TE1[L].mean()*100:.1f}%，T1 胜率 {TE1.T1_win.mean()*100:.1f}%，T1 均 {TE1.T1_ret.mean():+.2f}%，T2 均 {TE1.T2_ret.mean():+.2f}%\n')

    # ---- 6. 规则搜索 ----
    w('## 六、规则搜索（单条件 / 双条件；训练集 Wilson 下界排序，测试集要同向）\n')
    conds = []
    for f in [x for x in SC.feature.head(25).tolist() if x in U.columns]:
        x = U[f]
        qs = x.quantile([0.2, 0.4, 0.6, 0.8]).unique()
        for q in qs:
            conds.append((f'{f}>={q:.3g}', x >= q)); conds.append((f'{f}<{q:.3g}', x < q))
    res = []
    base_tr = y[tr].mean()
    for name, mask in conds:
        mtr = mask & tr; mte = mask & te
        if mtr.sum() < 150 or mte.sum() < 60:
            continue
        ktr = U.loc[mtr, L].sum(); kte = U.loc[mte, L].sum()
        res.append((name, int(mtr.sum()), ktr / mtr.sum() * 100, wilson_low(ktr, mtr.sum()) * 100, int(mte.sum()), kte / mte.sum() * 100,
                    U.loc[mte, 'T2_ret'].mean(), U.loc[mte, 'T1_ret'].mean()))
    R1 = pd.DataFrame(res, columns=['规则', '训练n', '训练胜率', '训练Wilson下界', '测试n', '测试胜率', '测试T2均', '测试T1均']).sort_values('训练Wilson下界', ascending=False)
    R1 = R1[R1.测试胜率 > base_tr * 100].head(15)
    w(f'训练集基线 {base_tr*100:.1f}%。单条件 Top 15（测试集胜率需高于基线）：\n'); w(R1.round(1).to_markdown(index=False)); w('')
    # 双条件：取单条件前 12 两两 AND
    top_c = [c for c in conds if c[0] in set(R1.规则.head(12))]
    res2 = []
    for i in range(len(top_c)):
        for j in range(i + 1, len(top_c)):
            if top_c[i][0].split('>')[0].split('<')[0] == top_c[j][0].split('>')[0].split('<')[0]:
                continue
            mask = top_c[i][1] & top_c[j][1]
            mtr = mask & tr; mte = mask & te
            if mtr.sum() < 100 or mte.sum() < 40:
                continue
            ktr = U.loc[mtr, L].sum(); kte = U.loc[mte, L].sum()
            res2.append((top_c[i][0] + ' & ' + top_c[j][0], int(mtr.sum()), ktr / mtr.sum() * 100, wilson_low(ktr, mtr.sum()) * 100,
                         int(mte.sum()), kte / mte.sum() * 100, U.loc[mte, 'T2_ret'].mean(), U.loc[mte, 'T1_ret'].mean()))
    if res2:
        R2 = pd.DataFrame(res2, columns=['规则', '训练n', '训练胜率', '训练Wilson下界', '测试n', '测试胜率', '测试T2均', '测试T1均']).sort_values('训练Wilson下界', ascending=False)
        R2 = R2[R2.测试胜率 > base_tr * 100].head(15)
        w('双条件 Top 15：\n'); w(R2.round(1).to_markdown(index=False)); w('')

    # ---- 7. 对照：T+1 盘后字段（不可用于选股，只看解释力） ----
    w('## 七、对照：T+1 盘中/盘后字段（买入时不可知，只看它们解释了多少）\n')
    for f in ('T1_gap', 'T1_amt_ratio', 't1_turnover_rate', 't1_volume_ratio'):
        if f in U.columns:
            t = bin_table(U, f, L)
            if t is not None:
                w(f'**{f}**\n'); w(t.to_markdown()); w('')

    txt = '\n'.join(O)
    with open(a.out, 'w') as f:
        f.write(txt)
    print(txt)
    print(f'\n[写出] {a.out}')


if __name__ == '__main__':
    main()
