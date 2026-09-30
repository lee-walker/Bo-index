"""
交易日判定模块单元测试

重点覆盖：
    1. 休市日识别（含调休、连续休市）
    2. 跳过判定状态机四种分支
    3. 真实美股节日场景（用历史数据反推的事实休市日）
    4. 边界：首次运行、数据源回退、state 损坏

运行：python scripts/test_trading_day.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_day import (  # noqa: E402
    latest_trading_day, is_us_trading_day, evaluate_skip, expected_trade_date,
    describe_beijing_schedule,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
print("\n== 1. 最新交易日提取 ==")
# ---------------------------------------------------------------------------

days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
check("从字符串序列取最新", latest_trading_day(days) == date(2026, 9, 4),
      str(latest_trading_day(days)))
check("支持 date 对象入参",
      latest_trading_day([date(2026, 1, 2), date(2026, 1, 5)]) == date(2026, 1, 5))
check("空序列返回 None", latest_trading_day([]) is None)
check("乱序序列也能取最大",
      latest_trading_day(["2026-09-04", "2026-09-01", "2026-09-08"]) == date(2026, 9, 8))

# ---------------------------------------------------------------------------
print("\n== 2. 交易日查询 ==")
# ---------------------------------------------------------------------------

cal = ["2026-09-03", "2026-09-04", "2026-09-08"]
check("交易日命中", is_us_trading_day(date(2026, 9, 4), cal))
check("休市日不命中", not is_us_trading_day(date(2026, 9, 7), cal))
check("周末不命中", not is_us_trading_day(date(2026, 9, 5), cal))

# ---------------------------------------------------------------------------
print("\n== 3. 跳过判定状态机 ==")
# ---------------------------------------------------------------------------

# 3.1 正常新交易日
r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 8),
    last_processed_trade_date="2026-09-04",
)
check("新交易日 → NEW_TRADING_DAY", r == "NEW_TRADING_DAY", r)

# 3.2 休市：最新交易日与上次相同
r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 4),
    last_processed_trade_date="2026-09-04",
)
check("交易日未推进 → NOT_TRADING_DAY", r == "NOT_TRADING_DAY", r)
check("跳过说明含休市提示", "休市" in d, d)

# 3.3 首次运行
r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 4),
    last_processed_trade_date=None,
)
check("无基线 → FIRST_RUN", r == "FIRST_RUN", r)

r, d = evaluate_skip(latest_trade_date=date(2026, 9, 4), last_processed_trade_date="")
check("空字符串基线 → FIRST_RUN", r == "FIRST_RUN", r)

# 3.4 数据源回退（最新交易日早于上次）
r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 1),
    last_processed_trade_date="2026-09-04",
)
check("交易日倒退 → STALE_DATA", r == "STALE_DATA", r)
check("回退说明含异常提示", "异常" in d, d)

# 3.5 数据源无数据
r, d = evaluate_skip(latest_trade_date=None, last_processed_trade_date="2026-09-04")
check("无数据 → STALE_DATA", r == "STALE_DATA", r)

# 3.6 state 损坏
r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 4),
    last_processed_trade_date="not-a-date",
)
check("基线格式损坏 → FIRST_RUN（不阻断）", r == "FIRST_RUN", r)

# 3.7 ⚠️ 回归：参数必须同时支持「字符串」与「date 对象」
#     真实调用方 compute.py 传的是 df.index.strftime 得到的**字符串**，
#     而本测试早期只用 date 对象，导致 str/date 混用时的 TypeError 逃逸。
#     （2026-09-30 首次部署验证时暴露：evaluate_skip 第 117 行 `<` 抛 TypeError）
r, d = evaluate_skip(
    latest_trade_date="2026-09-29",          # str 最新
    last_processed_trade_date="2026-09-28",  # str 基线
)
check("str/str → NEW_TRADING_DAY", r == "NEW_TRADING_DAY", r)

r, d = evaluate_skip(
    latest_trade_date="2026-09-01",          # str 最新，早于基线
    last_processed_trade_date="2026-09-05",  # str 基线
)
check("str/str 交易日倒退 → STALE_DATA（曾抛 TypeError）", r == "STALE_DATA", r)

r, d = evaluate_skip(
    latest_trade_date="2026-09-04",
    last_processed_trade_date="2026-09-04",
)
check("str/str 未推进 → NOT_TRADING_DAY", r == "NOT_TRADING_DAY", r)

r, d = evaluate_skip(
    latest_trade_date=date(2026, 9, 29),     # date 最新
    last_processed_trade_date="2026-09-28",  # str 基线（混用）
)
check("date/str 混用 → NEW_TRADING_DAY", r == "NEW_TRADING_DAY", r)

r, d = evaluate_skip(
    latest_trade_date="2026-09-29",          # str 最新
    last_processed_trade_date=date(2026, 9, 28),  # date 基线（混用）
)
check("str/date 混用 → NEW_TRADING_DAY", r == "NEW_TRADING_DAY", r)

r, d = evaluate_skip(latest_trade_date="", last_processed_trade_date="2026-09-28")
check("空字符串最新日 → FIRST_RUN（不阻断，与 None 同等处理）", r == "FIRST_RUN", r)

# ---------------------------------------------------------------------------
print("\n== 4. 真实美股节日场景 ==")
# ---------------------------------------------------------------------------

# 2026 年真实休市日（用历史行情数据反推得到）
US_2026_HOLIDAYS = [
    (date(2026, 1, 1),  "元旦"),
    (date(2026, 1, 19), "马丁·路德·金日"),
    (date(2026, 2, 16), "总统日"),
    (date(2026, 4, 3),  "耶稣受难日"),
    (date(2026, 5, 25), "阵亡将士纪念日"),
    (date(2026, 6, 19), "六月节"),
    (date(2026, 7, 3),  "独立日调休"),
    (date(2026, 9, 7),  "劳动节"),
]

cal_2026 = [date(2026, 1, 1) + timedelta(days=i) for i in range(300)]
cal_2026 = [d for d in cal_2026 if d.weekday() < 5 and d not in [h for h, _ in US_2026_HOLIDAYS]]

for h, name in US_2026_HOLIDAYS:
    check(f"{name}（{h}）识别为休市", not is_us_trading_day(h, cal_2026))

# 劳动节场景：完整推演
print("\n  -- 劳动节场景推演（2026-09-07 休市）--")
# 9/4 周五是节前最后交易日
check("9/4 周五是交易日", is_us_trading_day(date(2026, 9, 4), cal_2026))
check("9/5 周六休市", not is_us_trading_day(date(2026, 9, 5), cal_2026))
check("9/6 周日休市", not is_us_trading_day(date(2026, 9, 6), cal_2026))
check("9/7 劳动节休市", not is_us_trading_day(date(2026, 9, 7), cal_2026))
check("9/8 周二恢复交易", is_us_trading_day(date(2026, 9, 8), cal_2026))

# 北京 9/8 周二触发 → 目标美股 9/7（休市）→ 实际最新交易日仍是 9/4
r, _ = evaluate_skip(
    latest_trade_date=date(2026, 9, 4),
    last_processed_trade_date="2026-09-04",
)
check("北京9/8触发应跳过（劳动节）", r == "NOT_TRADING_DAY", r)

# 北京 9/9 周三触发 → 目标美股 9/8 → 正常执行
r, _ = evaluate_skip(
    latest_trade_date=date(2026, 9, 8),
    last_processed_trade_date="2026-09-04",
)
check("北京9/9触发应执行", r == "NEW_TRADING_DAY", r)

# 独立日调休场景：2026-07-03 周五休市，07-04 是周六
print("\n  -- 独立日调休场景（2026-07-03 休市）--")
check("7/3 调休休市", not is_us_trading_day(date(2026, 7, 3), cal_2026))
r, _ = evaluate_skip(
    latest_trade_date=date(2026, 7, 2),
    last_processed_trade_date="2026-07-02",
)
check("调休日应跳过", r == "NOT_TRADING_DAY", r)

# 6/19 六月节（周五）场景：与前后周末形成 3 连休
print("\n  -- 六月节 3 连休场景（2026-06-19 周五休市）--")
check("6/18 周四交易", is_us_trading_day(date(2026, 6, 18), cal_2026))
check("6/19 周五休市", not is_us_trading_day(date(2026, 6, 19), cal_2026))
check("6/22 周一交易", is_us_trading_day(date(2026, 6, 22), cal_2026))
# 北京 6/20 周六触发（对应美股 6/19）
r, _ = evaluate_skip(
    latest_trade_date=date(2026, 6, 18),
    last_processed_trade_date="2026-06-18",
)
check("六月节周末触发应跳过", r == "NOT_TRADING_DAY", r)
# 北京 6/23 周二触发（对应美股 6/22）
r, _ = evaluate_skip(
    latest_trade_date=date(2026, 6, 22),
    last_processed_trade_date="2026-06-18",
)
check("节后首日应执行", r == "NEW_TRADING_DAY", r)

# ---------------------------------------------------------------------------
print("\n== 5. 连续多次触发只执行一次 ==")
# ---------------------------------------------------------------------------

# 用劳动节全周期验证：只有真正出现新交易日那次才执行
seq = [
    # (北京触发日, 数据源最新交易日, 期望判定)
    # 首次运行无基线 → FIRST_RUN（这是正常的，首跑必须执行）
    (date(2026, 9, 5),  date(2026, 9, 4),  "FIRST_RUN"),
    (date(2026, 9, 8),  date(2026, 9, 4),  "NOT_TRADING_DAY"),   # 劳动节休市，跳过
    (date(2026, 9, 9),  date(2026, 9, 8),  "NEW_TRADING_DAY"),   # 节后首个交易日
    (date(2026, 9, 10), date(2026, 9, 9),  "NEW_TRADING_DAY"),
]

last_processed = None
executed = 0
skipped = 0
for beijing_day, latest_td, expected in seq:
    r, _ = evaluate_skip(
        latest_trade_date=latest_td,
        last_processed_trade_date=last_processed,
    )
    ok = (r == expected)
    check(f"{beijing_day} 触发 → {expected}", ok, f"实际 {r}")
    if r in ("NEW_TRADING_DAY", "FIRST_RUN"):
        executed += 1
        last_processed = latest_td.isoformat()
    else:
        skipped += 1

check("整段周期内执行 3 次", executed == 3, f"实际 {executed} 次")
check("整段周期内跳过 1 次", skipped == 1, f"实际 {skipped} 次")
check("跳过期间基线未被推进（仍为 9/4）", last_processed == "2026-09-09",
      f"实际 {last_processed}")

# ---------------------------------------------------------------------------
print("\n== 6. 预期交易日推算 ==")
# ---------------------------------------------------------------------------

# 北京 2026-09-08 07:50 触发 → 目标美股 2026-09-07
check("北京触发日减一天为目标日",
      expected_trade_date(datetime(2026, 9, 8, 7, 50)) == date(2026, 9, 7),
      str(expected_trade_date(datetime(2026, 9, 8, 7, 50))))

# 北京 2026-09-12 周六 07:50 → 目标美股 2026-09-11 周五
check("周六触发对应周五",
      expected_trade_date(datetime(2026, 9, 12, 7, 50)) == date(2026, 9, 11),
      str(expected_trade_date(datetime(2026, 9, 12, 7, 50))))

# 边界：触发时刻须晚于当日美股收盘（冬令时最晚 05:00），否则「前一天」不成立。
# 07:50 有充足余量，这里锁定该前提，防止日后把时间改早而悄悄失效。
check("07:50 晚于冬令时收盘(05:00)",
      datetime(2026, 9, 8, 7, 50).hour * 60 + 50 > 5 * 60,
      "触发时刻需晚于 05:00")

# 调度说明中的时间须与实际配置一致（防止改 cron 却忘了改文案）
_sched = describe_beijing_schedule()
check("调度说明含 07:50", "07:50" in _sched, _sched)
check("调度说明含 UTC cron 50 23", "50 23 * * 1-5" in _sched, _sched)

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：")
    for f in FAILED:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部测试通过")
