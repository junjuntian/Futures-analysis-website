"""平稳性检验工具(无 statsmodels/scipy,手写 OLS 版)。

**为什么自己写**:这台机器没有 statsmodels/scipy。ADF 与 KPSS 的实现都很短,
关键是临界值要用对(ADF 是非标准分布,不能拿正态表)。两个检验的原假设**相反**,
一起看才可靠:ADF 拒绝 = 平稳;KPSS 不拒绝 = 平稳;两者一致才下结论。
"""
import numpy as np

# Dickey-Fuller 临界值(MacKinnon 大样本近似)
ADF_CRIT = {"c": {1: -3.43, 5: -2.86, 10: -2.57},      # 含常数
            "ct": {1: -3.96, 5: -3.41, 10: -3.12},     # 含常数+趋势
            "n": {1: -2.57, 5: -1.94, 10: -1.62}}      # 无常数
KPSS_CRIT = {"c": {10: 0.347, 5: 0.463, 1: 0.739},     # 水平平稳
             "ct": {10: 0.119, 5: 0.146, 1: 0.216}}    # 趋势平稳


def _ols(X, y):
    """返回 (系数, 标准误)。用 lstsq 避免 X'X 病态。"""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    if n <= k:
        return beta, np.full(k, np.nan)
    s2 = float(resid @ resid) / (n - k)
    xtx_inv = np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.maximum(np.diag(xtx_inv) * s2, 0))
    return beta, se


def adf(y, trend="c", lags=None):
    """增广 Dickey-Fuller。H0 = 有单位根(非平稳)。统计量越负越拒绝 H0。"""
    y = np.asarray(y, float)
    y = y[np.isfinite(y)]
    n = len(y)
    if n < 30:
        return {"stat": np.nan, "lags": 0, "n": n, "verdict": "样本太短"}
    if lags is None:                                  # Schwert 上限 + 经验缩减
        lags = min(int(np.ceil(12 * (n / 100) ** 0.25)), n // 10, 24)
    dy = np.diff(y)
    rows, target = [], []
    for t in range(lags, len(dy)):
        row = [y[t]]                                  # y_{t-1}(dy[t] = y[t+1]-y[t])
        row += [dy[t - i] for i in range(1, lags + 1)]
        if trend in ("c", "ct"):
            row.append(1.0)
        if trend == "ct":
            row.append(float(t))
        rows.append(row)
        target.append(dy[t])
    X, Y = np.array(rows), np.array(target)
    beta, se = _ols(X, Y)
    stat = beta[0] / se[0] if np.isfinite(se[0]) and se[0] > 0 else np.nan
    crit = ADF_CRIT[trend]
    verdict = ("平稳(1%)" if stat < crit[1] else "平稳(5%)" if stat < crit[5]
               else "平稳(10%)" if stat < crit[10] else "**非平稳**")
    return {"stat": float(stat), "lags": lags, "n": n, "verdict": verdict, "crit5": crit[5]}


def kpss(y, trend="c"):
    """KPSS。H0 = 平稳。统计量越大越拒绝 H0(即越可能非平稳)。"""
    y = np.asarray(y, float)
    y = y[np.isfinite(y)]
    T = len(y)
    if T < 30:
        return {"stat": np.nan, "n": T, "verdict": "样本太短"}
    t = np.arange(T, dtype=float)
    X = np.column_stack([np.ones(T), t]) if trend == "ct" else np.ones((T, 1))
    beta, _ = _ols(X, y)
    e = y - X @ beta
    S = np.cumsum(e)
    L = int(4 * (T / 100) ** 0.25)                    # Newey-West 带宽
    g0 = float(e @ e) / T
    lr = g0
    for j in range(1, L + 1):
        gj = float(e[j:] @ e[:-j]) / T
        lr += 2 * (1 - j / (L + 1)) * gj
    stat = float((S @ S) / (T ** 2) / lr) if lr > 0 else np.nan
    crit = KPSS_CRIT[trend]
    verdict = ("**非平稳**(1%)" if stat > crit[1] else "**非平稳**(5%)" if stat > crit[5]
               else "**非平稳**(10%)" if stat > crit[10] else "平稳")
    return {"stat": stat, "n": T, "verdict": verdict, "crit5": crit[5]}


def verdict_pair(a, k):
    """ADF+KPSS 合读。两者一致才下结论,矛盾时如实说'不确定'。"""
    a_stat, k_stat = a.get("stat"), k.get("stat")
    if not (np.isfinite(a_stat) and np.isfinite(k_stat)):
        return "数据不足"
    a_st = a_stat < a["crit5"]           # ADF 拒绝单位根 = 平稳
    k_st = k_stat < k["crit5"]           # KPSS 不拒绝 = 平稳
    if a_st and k_st:
        return "平稳 ✓"
    if (not a_st) and (not k_st):
        return "**非平稳 ✗**"
    return "不确定(两检验冲突)"
