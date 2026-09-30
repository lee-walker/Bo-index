"""
Bo 配对动能指标 — 算法内核（纯函数，无 IO，便于单元测试）

算法规格严格对齐 需求方案.txt 第 2 节。

用法：
    from indicators import compute_all
    result = compute_all(df)   # df: 已对齐的 DataFrame，索引为时间升序
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 零轴判定容差。浮点误差会导致洗盘期出现虚假穿越，必须用容差而非 == 0。
EPS: float = 1e-6

#: ROC 回看窗口（交易日）
ROC_WINDOW: int = 20

#: EMA 平滑周期
EMA_PERIOD: int = 5

#: EMA 平滑系数 alpha = 2 / (N + 1)
ALPHA: float = 2.0 / (EMA_PERIOD + 1)   # = 1/3

#: KO 极端避险阈值（%）
KO_THRESHOLD: float = 7.0

Regime = Literal["OFFENSIVE", "DEFENSIVE", "NEUTRAL"]
Crossover = Literal["NONE", "CROSS_DOWN", "CROSS_UP"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sign(v: float) -> int:
    """带容差的符号函数。|v| < EPS 视为零轴（返回 0）。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0
    if abs(v) < EPS:
        return 0
    return 1 if v > 0 else -1


def rolling_roc(series: pd.Series, window: int = ROC_WINDOW) -> pd.Series:
    """
    滚动变动率（%）：ROC_t = (S_t / S_{t-window} - 1) * 100

    注意：必须使用 shift(window) 而非 pct_change(window)，两者在缺失值处理上不同。
    """
    prev = series.shift(window)
    return (series / prev - 1.0) * 100.0


def ema_signal(roc: pd.Series, alpha: float = ALPHA) -> pd.Series:
    """
    EMA 平滑：Signal_t = alpha * ROC_t + (1 - alpha) * Signal_{t-1}

    种子值策略（重要）：
        首个非 NaN 的 ROC 直接作为 Signal 初值，而非用 0 作种子。
        用 0 作种子会让前 ~20 个点严重失真（EMA 需要约 5 个半衰期才收敛）。

    实现选择：手写循环而非 pandas.ewm。
        原因：pandas.ewm 的 adjust 默认 True，前期是加权平均而非标准 EMA 递推，
        与本规格不符；adjust=False 也不处理「前导 NaN + 自定义种子」的场景。
        数据量很小（约 250 点），手写循环无性能问题，且语义完全可控。
    """
    out = pd.Series(np.nan, index=roc.index, dtype="float64")
    prev: float | None = None

    for idx, val in roc.items():
        if pd.isna(val):
            continue
        if prev is None:
            prev = float(val)              # 种子
        else:
            prev = alpha * float(val) + (1.0 - alpha) * prev
        out.loc[idx] = prev

    return out


# ---------------------------------------------------------------------------
# 单通道：比值 → ROC → EMA → 穿越事件
# ---------------------------------------------------------------------------

@dataclass
class ChannelResult:
    """单条配对通道的计算结果。"""
    ratio: pd.Series
    roc: pd.Series
    signal: pd.Series
    crossovers: pd.Series      # 逐日穿越事件：NONE / CROSS_DOWN / CROSS_UP


def compute_channel(defensive: pd.Series, growth: pd.Series) -> ChannelResult:
    """
    计算单条配对通道。

    Args:
        defensive: 防御端收盘价序列（VTV 或 CGDV）
        growth:    成长端收盘价序列（QQQ），两者索引必须一致
    """
    if not defensive.index.equals(growth.index):
        raise ValueError("防御端与成长端索引不一致，请先做交易日历对齐")

    ratio = defensive / growth
    roc = rolling_roc(ratio)
    signal = ema_signal(roc)
    crossovers = detect_crossovers(signal)

    return ChannelResult(ratio=ratio, roc=roc, signal=signal, crossovers=crossovers)


def detect_crossovers(signal: pd.Series) -> pd.Series:
    """
    零轴穿越状态机。

    逐日判定（基于容差化符号）：
        sign_t < 0 且 sign_{t-1} >= 0  → CROSS_DOWN（下穿零轴：成长端动能回归）
        sign_t > 0 且 sign_{t-1} <= 0  → CROSS_UP  （上穿零轴：资金流向防御端）
        其余                            → NONE

    注意 sign_{t-1} 用 >= / <=（含 0），因为「从零轴上贴着的状态穿越」也应算事件。
    """
    out = pd.Series("NONE", index=signal.index, dtype="object")
    signs = signal.apply(sign)

    prev_sign: int | None = None
    for idx, s in signs.items():
        if pd.isna(signal.loc[idx]):
            prev_sign = None
            continue
        if prev_sign is not None:
            if s < 0 and prev_sign >= 0:
                out.loc[idx] = "CROSS_DOWN"
            elif s > 0 and prev_sign <= 0:
                out.loc[idx] = "CROSS_UP"
        prev_sign = int(s)

    return out


# ---------------------------------------------------------------------------
# 双通道合成
# ---------------------------------------------------------------------------

def synthesize_regime(sig_vtv: float, sig_cgdv: float) -> Regime:
    """
    双通道合成规则：

        两通道均 < 0  → OFFENSIVE（成长端主导）
        两通道均 > 0  → DEFENSIVE（防御端主导）
        一正一负      → NEUTRAL（信号分歧，不强行给方向）

    单通道处于零轴（符号 0）时，以另一通道为准；两通道都为 0 时返回 NEUTRAL。
    """
    s1, s2 = sign(sig_vtv), sign(sig_cgdv)

    if s1 == 0 and s2 == 0:
        return "NEUTRAL"
    if s1 == 0:
        s1 = s2
    if s2 == 0:
        s2 = s1

    if s1 < 0 and s2 < 0:
        return "OFFENSIVE"
    if s1 > 0 and s2 > 0:
        return "DEFENSIVE"
    return "NEUTRAL"


def synthesize_series(sig_vtv: pd.Series, sig_cgdv: pd.Series) -> pd.Series:
    """逐日合成状态序列，用于历史的 regime 分块着色。"""
    regimes = [
        synthesize_regime(a, b)
        if not (pd.isna(a) or pd.isna(b))
        else None
        for a, b in zip(sig_vtv, sig_cgdv)
    ]
    return pd.Series(regimes, index=sig_vtv.index, dtype="object")


# ---------------------------------------------------------------------------
# KO 极端避险监控
# ---------------------------------------------------------------------------

def compute_ko(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """返回 (ko_roc20 序列, 是否触发警报 序列)。"""
    roc = rolling_roc(df["KO"], ROC_WINDOW)
    alert = roc >= KO_THRESHOLD
    return roc, alert


# ---------------------------------------------------------------------------
# 顶层聚合
# ---------------------------------------------------------------------------

REGIME_TEXT: dict[str, str] = {
    "OFFENSIVE": "进攻阶段（成长端主导）",
    "DEFENSIVE": "防守阶段（防御端主导）",
    "NEUTRAL": "信号分歧（两通道不一致）",
}


@dataclass
class ComputeResult:
    df: pd.DataFrame
    vtv: ChannelResult
    cgdv: ChannelResult
    regimes: pd.Series
    ko_roc: pd.Series
    ko_alert: pd.Series


def compute_all(df: pd.DataFrame) -> ComputeResult:
    """
    对已对齐的行情表做全量计算。

    Args:
        df: 索引为交易日（升序），列至少含 QQQ / VTV / CGDV / KO
    """
    required = {"QQQ", "VTV", "CGDV", "KO"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"行情表缺少必需列：{sorted(missing)}")

    df = df.sort_index()
    df = df.ffill()                 # 前向填充：不同 ETF 停牌日不产生空洞

    vtv = compute_channel(df["VTV"], df["QQQ"])
    cgdv = compute_channel(df["CGDV"], df["QQQ"])
    regimes = synthesize_series(vtv.signal, cgdv.signal)
    ko_roc, ko_alert = compute_ko(df)

    return ComputeResult(
        df=df, vtv=vtv, cgdv=cgdv, regimes=regimes, ko_roc=ko_roc, ko_alert=ko_alert
    )


def state_for_text(regime: str) -> str:
    return REGIME_TEXT.get(regime, "未知状态")
