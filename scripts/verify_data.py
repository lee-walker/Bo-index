"""
CGDV 数据可得性验证脚本

这是本方案唯一的真实技术风险：CGDV（Capital Group 股息价值 ETF）成立时间较晚，
若历史不足 1 年，则图表早期段落缺失、20 日 ROC 也需要预热期。

本地一次性运行，不属于工作流的一部分。

运行：python scripts/verify_data.py
"""

from __future__ import annotations

import sys

import pandas as pd

TICKERS = ["QQQ", "VTV", "CGDV", "KO"]


def main() -> int:
    import yfinance as yf

    print("=" * 68)
    print("CGDV 数据可得性验证")
    print("=" * 68)

    raw = yf.download(
        TICKERS, period="5y", interval="1d",
        # 与生产口径保持一致：原始价，非复权价
        auto_adjust=False, progress=False, threads=False, timeout=20,
    )

    if raw is None or raw.empty:
        print("❌ yfinance 返回空数据")
        return 1

    close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
    close = close[TICKERS]

    print(f"\n{'标的':<6} {'首个交易日':<14} {'末个交易日':<14} "
          f"{'有效天数':>8} {'缺失率':>8}")
    print("-" * 68)

    total_days = len(close)
    summary = {}

    for t in TICKERS:
        s = close[t].dropna()
        if s.empty:
            print(f"{t:<6} {'—':<14} {'—':<14} {'0':>8} {'100%':>8}")
            summary[t] = 0
            continue

        first = s.index[0].strftime("%Y-%m-%d")
        last = s.index[-1].strftime("%Y-%m-%d")
        n = len(s)
        miss = (1 - n / total_days) * 100
        summary[t] = n
        print(f"{t:<6} {first:<14} {last:<14} {n:>8} {miss:>7.1f}%")

    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)

    # 图表需要 1 年 ≈ 250 交易日；ROC+EMA 预热需要约 25 日
    NEEDED_CHART = 250
    NEEDED_MIN = 30

    ok = True
    for t in TICKERS:
        n = summary.get(t, 0)
        if n < NEEDED_MIN:
            print(f"❌ {t}: 仅 {n} 天，不足以计算 20 日 ROC（需 ≥ {NEEDED_MIN}）")
            ok = False
        elif n < NEEDED_CHART:
            print(f"⚠️  {t}: {n} 天（< {NEEDED_CHART}）—— 指标可算，"
                  f"但图表无法展示完整 1 年，起始点需顺延")
        else:
            print(f"✅ {t}: {n} 天，满足 1 年图表需求")

    cgdv_n = summary.get("CGDV", 0)
    print()
    if cgdv_n >= NEEDED_CHART:
        print("→ CGDV 数据充足，方案可按原设计实现。")
    elif cgdv_n >= NEEDED_MIN:
        print("→ CGDV 数据可算指标但不足 1 年。处置建议：")
        print("   图表起始点改为 CGDV 首个可用日，并在看板标注「CGDV 通道历史较短」。")
    else:
        print("→ CGDV 数据严重不足，需更换标的。候选替代（同为股息价值风格）：")
        print("   SCHD / VYM / DGRO / SPYD")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
