"""
Bo 配对动能指标 — 算法内核单元测试

重点覆盖三处最容易写错的逻辑：
    1. EMA 种子值（用 0 作种子会让前 20 个点失真）
    2. 零轴容差（浮点误差导致虚假穿越）
    3. 双通道合成（分歧必须给 NEUTRAL，不能强行选一边）

运行：python scripts/test_indicators.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from indicators import (  # noqa: E402
    EPS, ALPHA, rolling_roc, ema_signal, detect_crossovers,
    synthesize_regime, sign, compute_channel,
)
from state_io import State  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


# ---------------------------------------------------------------------------
print("\n== 1. 符号函数容差 ==")
# ---------------------------------------------------------------------------

check("正数符号为 +1", sign(1.5) == 1)
check("负数符号为 -1", sign(-2.3) == -1)
check("精确零符号为 0", sign(0.0) == 0)
# 核心：浮点误差产生的极小值必须视为零轴，否则会伪造穿越事件
check("1e-9 视为零轴", sign(1e-9) == 0, f"EPS={EPS}")
check("-1e-9 视为零轴", sign(-1e-9) == 0)
check("1e-5 不视为零轴", sign(1e-5) == 1)

# ---------------------------------------------------------------------------
print("\n== 2. ROC 计算 ==")
# ---------------------------------------------------------------------------

s = pd.Series([100.0] * 20 + [110.0])       # 前 20 天 100，第 21 天 110
roc = rolling_roc(s, 20)
check("前 20 天 ROC 为 NaN", roc.iloc[:20].isna().all())
check("第 21 天 ROC = +10%", approx(roc.iloc[20], 10.0), f"实际 {roc.iloc[20]}")

s2 = pd.Series([100.0] * 20 + [90.0])
check("下跌 10% 得 -10%", approx(rolling_roc(s2, 20).iloc[20], -10.0))

# ---------------------------------------------------------------------------
print("\n== 3. EMA 种子值（关键）==")
# ---------------------------------------------------------------------------

# 恒定 ROC：无论用什么种子，收敛后都应等于该常量
const = pd.Series([5.0] * 30)
sig = ema_signal(const)
check("恒定输入下 EMA 收敛到该常量", approx(sig.iloc[-1], 5.0, 1e-6),
      f"实际 {sig.iloc[-1]}")
# 种子正确：第一个值应直接等于首个 ROC，而不是被 0 稀释
check("种子 = 首个 ROC 值（非 0）", approx(sig.iloc[0], 5.0),
      f"实际 {sig.iloc[0]}，若为 2.5 则说明用了 0 作种子")

# 验证递推公式：S_t = (1/3)*ROC_t + (2/3)*S_{t-1}
mixed = pd.Series([3.0, 6.0, 9.0])
msig = ema_signal(mixed)
expected_1 = ALPHA * 6.0 + (1 - ALPHA) * 3.0
check(f"第 2 点递推正确（α={ALPHA:.4f}）", approx(msig.iloc[1], expected_1, 1e-9),
      f"期望 {expected_1}，实际 {msig.iloc[1]}")

# 前导 NaN 应被跳过，且不污染种子
with_nan = pd.Series([np.nan, np.nan, 4.0, 4.0])
wsig = ema_signal(with_nan)
check("前导 NaN 保持 NaN", wsig.iloc[:2].isna().all())
check("NaN 后的首值即种子", approx(wsig.iloc[2], 4.0), f"实际 {wsig.iloc[2]}")

# ---------------------------------------------------------------------------
print("\n== 4. 零轴穿越状态机 ==")
# ---------------------------------------------------------------------------

seq = pd.Series([1.0, 0.5, -0.3, -0.5, 0.2, 0.6])   # + + - - + +
idx = pd.date_range("2026-01-01", periods=len(seq), freq="D")
cr = detect_crossovers(pd.Series(seq.values, index=idx))

check("首次出现不判事件", cr.iloc[0] == "NONE", f"实际 {cr.iloc[0]}")
check("+ → - 判 CROSS_DOWN", cr.iloc[2] == "CROSS_DOWN", f"实际 {cr.iloc[2]}")
check("- → - 不判事件", cr.iloc[3] == "NONE", f"实际 {cr.iloc[3]}")
check("- → + 判 CROSS_UP", cr.iloc[4] == "CROSS_UP", f"实际 {cr.iloc[4]}")
check("+ → + 不判事件", cr.iloc[5] == "NONE", f"实际 {cr.iloc[5]}")

# 容差穿越：从 +1e-9 到 -3e-6，前者算零轴、后者算负 → 应判 CROSS_DOWN
tseq = pd.Series([1e-9, -3e-6])
tidx = pd.date_range("2026-01-01", periods=2, freq="D")
tcr = detect_crossovers(pd.Series(tseq.values, index=tidx))
check("容差边界穿越判定正确", tcr.iloc[1] == "CROSS_DOWN", f"实际 {tcr.iloc[1]}")

# 洗盘场景：从未真正跨过零轴，不应产生任何事件
chop = pd.Series([5e-7, -8e-7, 6e-7, -4e-7])
cidx = pd.date_range("2026-01-01", periods=len(chop), freq="D")
ccr = detect_crossovers(pd.Series(chop.values, index=cidx))
check("零轴附近洗盘不产生虚假穿越", (ccr == "NONE").all(),
      f"实际 {list(ccr)}")

# ---------------------------------------------------------------------------
print("\n== 5. 双通道合成 ==")
# ---------------------------------------------------------------------------

check("双负 → OFFENSIVE", synthesize_regime(-5.0, -3.0) == "OFFENSIVE")
check("双正 → DEFENSIVE", synthesize_regime(5.0, 3.0) == "DEFENSIVE")
check("VTV 负 CGDV 正 → NEUTRAL", synthesize_regime(-5.0, 3.0) == "NEUTRAL")
check("VTV 正 CGDV 负 → NEUTRAL", synthesize_regime(5.0, -3.0) == "NEUTRAL")

# 单通道贴零轴时应跟随另一通道，而非误判分歧
check("VTV 贴零 + CGDV 负 → OFFENSIVE", synthesize_regime(1e-9, -3.0) == "OFFENSIVE")
check("VTV 贴零 + CGDV 正 → DEFENSIVE", synthesize_regime(1e-9, 3.0) == "DEFENSIVE")
check("双贴零 → NEUTRAL", synthesize_regime(1e-9, -1e-9) == "NEUTRAL")

# ---------------------------------------------------------------------------
print("\n== 6. 通道整合 ==")
# ---------------------------------------------------------------------------

# 构造：防御端相对成长端持续走强 → 比值上升 → ROC 正 → 信号 + → DEFENSIVE
n = 60
growth = pd.Series(np.full(n, 100.0), index=pd.RangeIndex(n))
defensive = pd.Series(np.linspace(50.0, 80.0, n), index=pd.RangeIndex(n))
ch = compute_channel(defensive, defensive.index.to_series().map(
    lambda i: growth.iloc[i]).astype(float))
check("比值序列已生成", len(ch.ratio) == n)
check("ROC 后段为正", ch.roc.iloc[-1] > 0, f"实际 {ch.roc.iloc[-1]}")
check("信号后段为正", ch.signal.iloc[-1] > 0, f"实际 {ch.signal.iloc[-1]}")

try:
    compute_channel(
        pd.Series([1.0, 2.0], index=pd.RangeIndex(2)),
        pd.Series([1.0, 2.0], index=pd.RangeIndex(5)),
    )
    check("索引不一致时抛异常", False)
except ValueError:
    check("索引不一致时抛异常", True)

# ---------------------------------------------------------------------------
print("\n== 7. 状态时序（去重与穿越判定）==")
# ---------------------------------------------------------------------------

st = State()
check("初始无推送记录", not st.already_pushed("2026-09-29", "DAILY"))
st.mark_pushed("2026-09-29", "DAILY")
check("标记后识别为已推送", st.already_pushed("2026-09-29", "DAILY"))
check("不同日不误判", not st.already_pushed("2026-09-30", "DAILY"))
check("同日不同类型不误判", not st.already_pushed("2026-09-29", "KO_ALERT"))

# 穿越判定依赖 last_*（昨日）与 pending_*（今日）的分离
st2 = State()
st2.last_signal_vtv = 1.2
st2.pending_signal_vtv = -0.4
st2.last_signal_cgdv = -2.0
st2.pending_signal_cgdv = -2.5
crosses = st2.detect_crossovers()
check("VTV 由正转负判 CROSS_DOWN", crosses["vtv"] == "CROSS_DOWN",
      f"实际 {crosses['vtv']}")
check("CGDV 持续为负不判事件", crosses["cgdv"] == "NONE",
      f"实际 {crosses['cgdv']}")

# commit_pending 后基线推进，再次判定不应重复报同一事件
st2.commit_pending()
check("基线推进后事件不重复", st2.detect_crossovers()["vtv"] == "NONE",
      f"实际 {st2.detect_crossovers()['vtv']}")

# ---------------------------------------------------------------------------
print("\n== 8. KO 警报边沿检测 ==")
# ---------------------------------------------------------------------------

# 实测背景：KO_ROC20 会在阈值上方连续停留（历史样本最长连续 21 个交易日）。
# 若用电平触发，用户连续三周每天收到高优先级警报 → 必然关闭通知。
# 因此必须为边沿触发。

def ko_should_alert(triggered: bool, was_active: bool) -> bool:
    return triggered and not was_active


check("首次进入阈值 → 告警", ko_should_alert(True, False))
check("持续在阈值内 → 不告警", not ko_should_alert(True, True))
check("退出阈值 → 不告警", not ko_should_alert(False, True))
check("一直在阈值外 → 不告警", not ko_should_alert(False, False))

# 模拟连续 21 天在阈值内，应只告警 1 次
days = [True] * 21
was = False
alerts = 0
for t in days:
    if ko_should_alert(t, was):
        alerts += 1
    was = t
check("连续 21 天在阈值内只告警 1 次", alerts == 1, f"实际 {alerts} 次")

# 三段式：进入 → 持续 → 退出 → 再进入，应告警 2 次
seq2 = [True, True, False, True]
was, alerts = False, 0
for t in seq2:
    if ko_should_alert(t, was):
        alerts += 1
    was = t
check("进入-退出-再进入共告警 2 次", alerts == 2, f"实际 {alerts} 次")

# 验证 State 字段存在且可持久化
st3 = State()
check("State 含 ko_was_active 字段", hasattr(st3, "ko_was_active"))
st3.ko_was_active = True
restored = State.from_dict(st3.to_dict())
check("ko_was_active 可正常持久化", restored.ko_was_active is True)

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：")
    for f in FAILED:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部测试通过")
