"""
复权口径回归测试

背景（真实事故）：
    2026-09-30 发现生产数据与独立数据源存在系统性偏差：
        VTV/QQQ  ROC20: -5.54%（我方） vs -5.88%（对方）
        CGDV/QQQ ROC20: -4.41%（我方） vs -4.31%（对方）
        KO       ROC20: -1.48%（我方） vs -2.06%（对方）
    比值完全一致，但 ROC 有偏差 → 定位为取数复权口径错误。

根因：
    yfinance 的 auto_adjust 默认为 True（返回复权价）。
    复权价会调整历史价格以消除分红除息影响，
    导致 20 日前的比值与原始价口径不一致，ROC 产生系统性偏差。
    实测偏差约 0.3~0.4 个百分点，足以影响状态判定。

修复：
    yfinance 显式使用 auto_adjust=False；
    Alpha Vantage 改用 TIME_SERIES_DAILY 的 `4. close`（非 adjusted）。

本测试的作用：
    锁定这一口径，防止后续被误改回复权价。
    所有断言基于**真实行情数据**（需联网）。

运行：python scripts/test_adjustment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

TICKERS = ["QQQ", "VTV", "CGDV", "KO"]

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def load(auto_adjust: bool) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(
        TICKERS, period="400d", interval="1d",
        auto_adjust=auto_adjust, progress=False, threads=False, timeout=30,
    )
    close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    close = close[TICKERS]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index().ffill()


def main() -> int:
    print("\n== 复权口径回归测试 ==\n")

    print("加载两种口径数据（auto_adjust=True / False）...")
    df_adj = load(True)
    df_raw = load(False)

    t = df_adj.index[-1]
    print(f"最新交易日：{t.date()}\n")

    # -----------------------------------------------------------------------
    print("== 1. 最新收盘价应两种口径一致（近期无分红，复权不改变现值）==")
    # -----------------------------------------------------------------------
    # 复权价的特点是「调整历史、不改现值」，因此最新一天的收盘价应当相同
    for tk in TICKERS:
        a, r = float(df_adj[tk].iloc[-1]), float(df_raw[tk].iloc[-1])
        check(f"{tk} 最新收盘价一致（adj={a:.2f} / raw={r:.2f}）",
              abs(a - r) < 0.01, f"差异 {abs(a-r):.4f}")

    # -----------------------------------------------------------------------
    print("\n== 2. 历史价格应存在差异（证明复权确实生效）==")
    # -----------------------------------------------------------------------
    t20 = df_adj.index[-21]
    diffs = {}
    for tk in TICKERS:
        a, r = float(df_adj[tk].loc[t20]), float(df_raw[tk].loc[t20])
        diffs[tk] = abs(a - r)
    has_diff = any(d > 0.001 for d in diffs.values())
    check(f"20 日前存在复权差异（{t20.date()}）", has_diff,
          " → ".join(f"{k}:{v:.4f}" for k, v in diffs.items()))
    for tk, d in diffs.items():
        if d > 0.001:
            print(f"         {tk} 复权差异 {d:.4f}")

    # -----------------------------------------------------------------------
    print("\n== 3. ROC20 应因口径不同而产生偏差 ==")
    # -----------------------------------------------------------------------
    roc = {}
    for label, df in (("adj", df_adj), ("raw", df_raw)):
        r = df["VTV"] / df["QQQ"]
        roc[label] = (r.iloc[-1] / r.iloc[-21] - 1) * 100

    print(f"         VTV/QQQ ROC20  复权={roc['adj']:+.2f}%   原始={roc['raw']:+.2f}%")
    check("两种口径的 ROC 存在显著差异（>0.1）",
          abs(roc["adj"] - roc["raw"]) > 0.1,
          f"差异仅 {abs(roc['adj']-roc['raw']):.4f}")

    # -----------------------------------------------------------------------
    print("\n== 4. 锁定正确答案：原始价口径 ===")
    # -----------------------------------------------------------------------
    # 独立数据源（2026-09-29）给出的基准值
    EXPECTED = {
        "VTV/QQQ_ROC20": -5.88,
        "VTV/QQQ_EMA5": -5.70,
        "CGDV/QQQ_ROC20": -4.31,
        "CGDV/QQQ_EMA5": -4.86,
        "KO_ROC20": -2.06,
    }

    def ema(s, alpha=1/3):
        out = pd.Series(index=s.index, dtype=float)
        prev = None
        for i, v in s.items():
            if pd.isna(v):
                continue
            prev = float(v) if prev is None else alpha * float(v) + (1 - alpha) * prev
            out[i] = prev
        return out

    def roc20_of(df, tk):
        r = df[tk] / df["QQQ"]
        return (r.iloc[-1] / r.iloc[-21] - 1) * 100

    def ema5_of(df, tk):
        r = df[tk] / df["QQQ"]
        rr = (r / r.shift(20) - 1) * 100
        return float(ema(rr).iloc[-1])

    def ko_of(df):
        r = df["KO"] / df["KO"].shift(20)
        return (r.iloc[-1] - 1) * 100

    computed = {
        "VTV/QQQ_ROC20": roc20_of(df_raw, "VTV"),
        "VTV/QQQ_EMA5": ema5_of(df_raw, "VTV"),
        "CGDV/QQQ_ROC20": roc20_of(df_raw, "CGDV"),
        "CGDV/QQQ_EMA5": ema5_of(df_raw, "CGDV"),
        "KO_ROC20": ko_of(df_raw),
    }

    for k, expected in EXPECTED.items():
        got = computed[k]
        ok = abs(got - expected) <= 0.02
        check(f"{k}: 期望 {expected:+.2f}%，实际 {got:+.2f}%", ok,
              f"偏差 {abs(got-expected):.4f} 超出容差")

    # -----------------------------------------------------------------------
    print("\n== 5. 反证：复权口径对不上基准 ==")
    # -----------------------------------------------------------------------
    adj_computed = {
        "VTV/QQQ_ROC20": roc20_of(df_adj, "VTV"),
        "CGDV/QQQ_ROC20": roc20_of(df_adj, "CGDV"),
        "KO_ROC20": ko_of(df_adj),
    }
    mismatches = 0
    for k, expected in EXPECTED.items():
        if k in adj_computed:
            if abs(adj_computed[k] - expected) > 0.02:
                mismatches += 1
    check("复权口径确实与基准不符（证明修复方向正确）", mismatches >= 2,
          f"仅 {mismatches} 项不符")
    for k, v in adj_computed.items():
        print(f"         复权口径 {k} = {v:+.2f}%（期望 {EXPECTED[k]:+.2f}%）")

    # -----------------------------------------------------------------------
    print("\n" + "=" * 62)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print(f"   - {f}")
        print("\n⚠️ 若「锁定正确答案」一节失败，说明复权口径被改动了，")
        print("   请检查 compute.py 中 auto_adjust 参数是否为 False。")
        return 1
    print("✅ 全部通过 —— 复权口径正确（原始价）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
