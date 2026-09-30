"""
Bo 配对动能指标 — 取数 + 计算 + 输出

流程：
    1. 三级降级链取数：yfinance → Alpha Vantage → 沿用上次结果
    2. **交易日判定**：若前一交易日为休市日，跳过后续全部流程（不计算、不推送）
    3. 交易日历对齐（以 QQQ 为基准轴，前向填充）
    4. 全量指标计算
    5. 输出 docs/data/{latest,history}.json，更新 state.json

退出码约定：
    0 = 成功（含降级成功、含休市跳过）
    1 = 彻底失败（两级数据源都不可用且无历史兜底）
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from indicators import compute_all, REGIME_TEXT
from state_io import State, load_state, save_state, write_json
from trading_day import evaluate_skip, latest_trading_day

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
STATE_PATH = DATA_DIR / "state.json"

SCHEMA_VERSION = 2

TICKERS = ["QQQ", "VTV", "CGDV", "KO"]

#: 拉取的历史长度。250 交易日 ≈ 1 自然年；多留 60 天给 ROC/EMA 预热
LOOKBACK_DAYS = 400

#: Alpha Vantage 免费层限制 5 次/分钟 → 串行 + 13 秒间隔
AV_INTERVAL_SEC = 13


# ---------------------------------------------------------------------------
# 数据源 1：yfinance
# ---------------------------------------------------------------------------

def fetch_yfinance() -> pd.DataFrame | None:
    """
    yfinance 取数。8 秒超时，失败重试 2 次（间隔 2s / 6s）。

    ⚠️ 复权口径（重要，勿改）：
        必须使用 auto_adjust=False，即**原始收盘价（未复权）**。

        原因：本项目指标是「两只标的的比值」，比值本身已在一定程度上消除
        了市场整体波动，但仍受分红除息影响。若使用复权价，历史价格会被
        向前调整，导致 20 日前的比值与原始价口径不一致，ROC 产生系统性偏差。

        实测（2026-09-29 数据）：
            auto_adjust=True  → VTV/QQQ ROC20 = -5.54%  ❌
            auto_adjust=False → VTV/QQQ ROC20 = -5.88%  ✅ 与独立数据源一致

        差异量级约 0.34 个百分点，足以影响状态判定与推送内容。

        注：yfinance 0.2.x 中 auto_adjust 默认值为 True，必须显式传 False。

    返回：以日期为索引（升序）、列为 ticker 的收盘价 DataFrame。
    """
    import yfinance as yf

    for attempt, backoff in enumerate([0, 2, 6], start=1):
        if backoff:
            time.sleep(backoff)
        try:
            raw = yf.download(
                TICKERS,
                period=f"{LOOKBACK_DAYS}d",
                interval="1d",
                auto_adjust=False,     # 原始价，勿改，见 docstring
                progress=False,
                threads=False,
                timeout=8,
            )
            if raw is None or raw.empty:
                raise ValueError("yfinance 返回空数据")

            # 单 ticker 与多 ticker 的列结构不同，统一取 Close 层
            close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
            close = close[TICKERS].dropna(how="all")
            close.index = pd.to_datetime(close.index).tz_localize(None).normalize()

            return _sanity_check(close, "yfinance")
        except Exception as exc:  # noqa: BLE001 — 需吞掉一切异常走降级
            print(f"[yfinance] 第 {attempt} 次尝试失败：{exc}", file=sys.stderr)

    return None


# ---------------------------------------------------------------------------
# 数据源 2：Alpha Vantage
# ---------------------------------------------------------------------------

def fetch_alpha_vantage() -> pd.DataFrame | None:
    """
    Alpha Vantage 备用源。

    免费层 5 次/分钟 → 4 个 ticker 必须串行且每次间隔 13 秒。
    每个 ticker 单独请求，任一失败则整体放弃（避免半残缺数据）。

    ⚠️ 口径一致性（重要）：
        必须与主源 yfinance 保持相同的复权口径，即**原始收盘价（未复权）**。

        实现方式：使用 TIME_SERIES_DAILY 端点并取 `4. close` 字段。
        不要用 TIME_SERIES_DAILY_ADJUSTED 的 `5. adjusted close`——
        那是复权价，会让降级切换时的指标产生跳变，用户会看到
        「同一天指标突然变了」的异常（实测差异约 0.3~0.4 个百分点）。
    """
    api_key = os.environ.get("AV_API_KEY", "").strip()
    if not api_key:
        print("[alpha-vantage] 未配置 AV_API_KEY，跳过", file=sys.stderr)
        return None

    import requests

    series: dict[str, pd.Series] = {}
    for i, ticker in enumerate(TICKERS):
        if i:
            time.sleep(AV_INTERVAL_SEC)
        try:
            resp = requests.get(
                "https://www.alphavantage.co/query",
                params={
                    # 用非复权端点，保持与 yfinance auto_adjust=False 一致
                    "function": "TIME_SERIES_DAILY",
                    "symbol": ticker,
                    "outputsize": "full",
                    "apikey": api_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()

            key = next((k for k in payload if "Time Series" in k), None)
            if not key:
                raise ValueError(f"响应无时间序列（可能触发限额）：{list(payload)[:2]}")

            s = pd.Series(
                {d: float(v["4. close"]) for d, v in payload[key].items()}
            )
            s.index = pd.to_datetime(s.index)
            series[ticker] = s.sort_index()
        except Exception as exc:  # noqa: BLE001
            print(f"[alpha-vantage] {ticker} 取数失败：{exc}", file=sys.stderr)
            return None

    df = pd.DataFrame(series)
    df = df.last(f"{LOOKBACK_DAYS}D")
    return _sanity_check(df, "alpha-vantage")


# ---------------------------------------------------------------------------
# 校验与对齐
# ---------------------------------------------------------------------------

def _sanity_check(df: pd.DataFrame, source: str) -> pd.DataFrame | None:
    """基本健康检查：列齐、行数够、无全零。不通过则视为该源失败。"""
    if df is None or df.empty:
        print(f"[{source}] 数据为空", file=sys.stderr)
        return None
    if (df <= 0).any().any():
        print(f"[{source}] 存在非正价格，判定为脏数据", file=sys.stderr)
        return None
    # 至少要有 ROC 窗口 + EMA 预热 + 一段图表区间
    if len(df) < 60:
        print(f"[{source}] 行数不足（{len(df)} < 60）", file=sys.stderr)
        return None
    return df


def align_calendar(df: pd.DataFrame) -> pd.DataFrame:
    """
    交易日历对齐。

    以 QQQ 为基准轴（成长端是比值分母，也是图表主图），
    其余标的按前向填充对齐。

    为什么不用 inner join：不同 ETF 的停牌日、不同交易所的节假日会造成日期空洞，
    inner join 会把这些天整体删掉，导致 ROC 窗口错位。
    """
    df = df.sort_index()
    qqq_index = df["QQQ"].dropna().index
    aligned = df.reindex(qqq_index).ffill()
    return aligned.dropna(subset=["QQQ"])


def resolve(df: pd.DataFrame) -> pd.DataFrame | None:
    """三级降级链。返回 (df, source, is_degraded)。"""
    got = fetch_yfinance()
    if got is not None:
        return align_calendar(got), "yfinance", False

    print("[fallback] yfinance 不可用，切换 Alpha Vantage", file=sys.stderr)
    got = fetch_alpha_vantage()
    if got is not None:
        return align_calendar(got), "alpha-vantage", False

    print("[fallback] 备用源亦不可用", file=sys.stderr)
    return None, "", True


# ---------------------------------------------------------------------------
# 输出组装
# ---------------------------------------------------------------------------

def build_latest(result, source: str, degraded: bool, prev_state: State) -> dict:
    """组装 latest.json。"""
    df = result.df
    last = df.index[-1]
    prev = df.index[-2]

    def quote(ticker: str) -> dict:
        price = round(float(df[ticker].iloc[-1]), 2)
        change = round((float(df[ticker].iloc[-1]) / float(df[ticker].iloc[-2]) - 1) * 100, 2)
        return {"price": price, "changePct": change}

    vtv_sig = float(result.vtv.signal.iloc[-1])
    cgdv_sig = float(result.cgdv.signal.iloc[-1])
    regime = result.regimes.iloc[-1] or "NEUTRAL"
    crossover = result.vtv.crossovers.iloc[-1]

    trend = []
    for i in range(max(0, len(df) - 5), len(df)):
        d = df.index[i]
        trend.append({
            "date": d.strftime("%Y-%m-%d"),
            "vtvSignal": _round_or_none(result.vtv.signal.iloc[i]),
            "cgdvSignal": _round_or_none(result.cgdv.signal.iloc[i]),
            "state": result.regimes.iloc[i] or "NEUTRAL",
        })
    trend.reverse()

    return {
        "schemaVersion": SCHEMA_VERSION,
        "tradeDate": last.strftime("%Y-%m-%d"),
        "prevTradeDate": prev.strftime("%Y-%m-%d"),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataSource": source,
        "isDegraded": degraded,
        "regime": regime,
        "regimeText": REGIME_TEXT.get(regime, "未知状态"),
        "crossoverEvent": crossover,
        "koAlert": {
            "roc20": _round_or_none(result.ko_roc.iloc[-1]),
            "isTriggered": bool(result.ko_alert.iloc[-1]),
            "threshold": 7.0,
        },
        "quotes": {t: quote(t) for t in TICKERS},
        "indicators": {
            "vtvPair": {
                "ratio": _round_or_none(result.vtv.ratio.iloc[-1], 4),
                "roc20": _round_or_none(result.vtv.roc.iloc[-1]),
                "signal": round(vtv_sig, 2),
            },
            "cgdvPair": {
                "ratio": _round_or_none(result.cgdv.ratio.iloc[-1], 4),
                "roc20": _round_or_none(result.cgdv.roc.iloc[-1]),
                "signal": round(cgdv_sig, 2),
            },
        },
        "recentTrend": trend,
    }


def build_history(result) -> dict:
    """组装 history.json（列式存储，省体积）。"""
    df = result.df

    mask = result.vtv.signal.notna() & result.cgdv.signal.notna()
    idx = df.index[mask]

    events = []
    for i, d in enumerate(idx):
        pos = df.index.get_loc(d)
        if result.vtv.crossovers.iloc[pos] != "NONE":
            events.append({
                "date": d.strftime("%Y-%m-%d"),
                "type": result.vtv.crossovers.iloc[pos],
            })
        if bool(result.ko_alert.iloc[pos]):
            events.append({"date": d.strftime("%Y-%m-%d"), "type": "KO_ALERT"})

    return {
        "schemaVersion": SCHEMA_VERSION,
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "qqqClose": [round(float(v), 2) for v in df["QQQ"].loc[idx]],
        "vtvSignal": [_round_or_none(result.vtv.signal.loc[d]) for d in idx],
        "cgdvSignal": [_round_or_none(result.cgdv.signal.loc[d]) for d in idx],
        "regimes": [r or "NEUTRAL" for r in result.regimes.loc[idx]],
        "events": events,
    }


def _round_or_none(v, ndigits: int = 2):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 跳过标记（供工作流与 notify.py 读取）
# ---------------------------------------------------------------------------

SKIP_FLAG_PATH = DATA_DIR / "skip.json"


def write_skip_flag(reason: str, detail: str, trade_date: date | None) -> None:
    """
    写入跳过标记。

    为什么用文件而不是环境变量：
        compute.py 与 notify.py 是工作流中的两个独立 step，
        环境变量无法跨 step 传递（除非用 $GITHUB_ENV，但那需要脚本感知 CI 环境）。
        落盘标记文件更通用，本地运行也能正常工作。
    """
    write_json(SKIP_FLAG_PATH, {
        "skipped": True,
        "reason": reason,
        "detail": detail,
        "latestTradeDate": trade_date.isoformat() if trade_date else None,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def clear_skip_flag() -> None:
    """清除跳过标记（每次成功执行时调用，避免残留影响下次判定）。"""
    if SKIP_FLAG_PATH.exists():
        SKIP_FLAG_PATH.unlink()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    prev_state = load_state(STATE_PATH)

    df, source, degraded = resolve(None)

    if df is None:
        # 三级降级全败：沿用上次结果，标记 DEGRADED
        if LATEST_PATH.exists():
            import json
            stale = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
            stale["isDegraded"] = True
            stale["dataSource"] = "stale-last-known"
            stale["degradedReason"] = "全部数据源不可用，展示为上次成功结果"
            write_json(LATEST_PATH, stale)

            prev_state.consecutive_failures += 1
            save_state(STATE_PATH, prev_state)
            print(f"[warn] 已降级为历史数据，连续失败 {prev_state.consecutive_failures} 次")
            return 0

        print("[fatal] 无数据源可用且无历史兜底", file=sys.stderr)
        return 1

    # ---- 交易日判定（在计算之前）----
    # 数据源返回的日期序列即为真实交易日历，据此判断是否出现了新交易日。
    # 若前一交易日为休市日，最新交易日会与上次已处理日相同 → 跳过后续全部流程。
    td_latest = latest_trading_day(list(df.index.strftime("%Y-%m-%d")))
    reason, detail = evaluate_skip(
        latest_trade_date=td_latest,
        last_processed_trade_date=prev_state.last_trade_date or None,
    )
    print(f"[trading-day] {reason}: {detail}")

    if reason == "NOT_TRADING_DAY":
        # 休市：静默跳过。不写 latest/history，不改动信号基线，不发推送。
        # 只需记录跳过原因，便于事后排查。
        write_skip_flag(reason, detail, td_latest)
        print(f"[skip] 前一交易日为休市日，本次不执行计算与推送")
        return 0

    if reason == "STALE_DATA":
        # 数据源回退：按降级处理，保留上次结果并告警
        write_skip_flag(reason, detail, td_latest)
        if LATEST_PATH.exists():
            import json
            stale = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
            stale["isDegraded"] = True
            stale["degradedReason"] = detail
            write_json(LATEST_PATH, stale)
        prev_state.consecutive_failures += 1
        save_state(STATE_PATH, prev_state)
        print(f"[warn] {detail}", file=sys.stderr)
        return 0

    # NEW_TRADING_DAY 或 FIRST_RUN：正常执行
    clear_skip_flag()

    result = compute_all(df)

    # 记录信号变化，供 notify.py 判断穿越
    latest = build_latest(result, source, degraded, prev_state)
    history = build_history(result)

    write_json(LATEST_PATH, latest)
    write_json(HISTORY_PATH, history)

    # ⚠️ 状态更新时序（易错点）：
    #   穿越判定需要「昨日信号 vs 今日信号」的对比，而本脚本已经把今天的值写进 latest。
    #   若此处就把 last_signal_* 覆盖为今天的值，notify.py 再读时昨日值已丢失，
    #   永远判定不出穿越。
    #   因此这里先把「今日值」暂存到 *_pending 字段，真正的 last_signal_* 由
    #   notify.py 在推送完成后统一推进（无论是否推送都会推进，保证不丢基线）。
    prev_state.pending_signal_vtv = latest["indicators"]["vtvPair"]["signal"]
    prev_state.pending_signal_cgdv = latest["indicators"]["cgdvPair"]["signal"]
    prev_state.pending_regime = latest["regime"]
    prev_state.pending_trade_date = latest["tradeDate"]
    prev_state.pending_source = source
    prev_state.pending_degraded = degraded
    prev_state.consecutive_failures = 0
    save_state(STATE_PATH, prev_state)

    print(f"[ok] tradeDate={latest['tradeDate']} source={source} "
          f"regime={latest['regime']} crossover={latest['crossoverEvent']} "
          f"koAlert={latest['koAlert']['isTriggered']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
