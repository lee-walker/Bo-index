"""
Bo 配对动能指标 — 交易日判定

设计原则（重要）：
    判定「前一交易日是否为新交易日」时，**以数据源返回的行情日历为事实依据**，
    而不是硬编码节假日表。

    原因：
    1. 美股休市日除固定节日外，还有调休（如独立日逢周末提前到周五休市）、
       临时休市（国葬、极端天气、交易所故障），静态表无法覆盖。
    2. 静态表需要每年维护，一旦遗漏就会在休市日发出重复播报。
    3. 数据源本身就带有交易日历（yfinance 返回的日期序列即为真实交易日）。

    因此本模块的职责是：从已获取的行情数据中提取出「最新交易日」，
    并与上次已处理的交易日对比，判断是否值得继续执行。

为什么不直接比对「数据内容是否变化」：
    数据内容不变有两种可能——休市，或数据源故障。
    前者应静默跳过，后者应告警。若只看内容则会混淆二者。
    用交易日历判定可以明确区分。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

#: 判定结果
#:   NEW_TRADING_DAY — 出现了上次未处理过的新交易日，继续执行
#:   NOT_TRADING_DAY — 前一交易日与上次相同（休市），跳过
#:   FIRST_RUN       — 无历史基线（首次运行或 state 丢失），按新交易日处理
#:   STALE_DATA      — 数据源返回的最新交易日早于上次已处理日（异常回退）
SkipReason = Literal["NEW_TRADING_DAY", "NOT_TRADING_DAY", "FIRST_RUN", "STALE_DATA"]


def _to_date(value: str | date | None) -> date | None:
    """
    将 'YYYY-MM-DD' 字符串或 date 对象统一归一化为 date；无法解析时返回 None。

    交易日数据在工程内以两种形态流转：
        - 数据源 / JSON 文件 / state  → 字符串 'YYYY-MM-DD'
        - 内部计算与比较              → date 对象
    比较前必须先归一化，否则 str 与 date 混用会在 `<` / `>` 上抛 TypeError。
    """
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def latest_trading_day(trading_days: list[str] | list[date]) -> date | None:
    """
    从交易日序列中取最新一个交易日。

    Args:
        trading_days: 交易日序列，元素为 'YYYY-MM-DD' 字符串或 date 对象
    Returns:
        最新的交易日；序列为空时返回 None
    """
    if not trading_days:
        return None
    normalized = [d for d in (_to_date(x) for x in trading_days) if d is not None]
    return max(normalized) if normalized else None


def is_us_trading_day(d: date, trading_days: list[str] | list[date]) -> bool:
    """
    判断某日是否为美股交易日。

    注意：这是**基于已有交易日历的查询**，而非独立推算。
    若该日期超出交易日历覆盖范围，返回 False（保守：不确认就不执行）。

    首个版本的实现依赖数据源返回的日历。若需要判断「未来某日」，
    应扩展为接入 exchange_calendars 之类的交易日历库。
    """
    normalized = {x for x in (_to_date(i) for i in trading_days) if x is not None}
    return _to_date(d) in normalized


def evaluate_skip(
    *,
    latest_trade_date: date | str | None,
    last_processed_trade_date: str | date | None,
    today_utc: date | None = None,
) -> tuple[SkipReason, str]:
    """
    判定本次运行是否应跳过后续流程。

    Args:
        latest_trade_date:          数据源返回的最新交易日（'YYYY-MM-DD' 或 date）
        last_processed_trade_date:  state 中记录的上次已处理交易日（'YYYY-MM-DD' 或 date）
        today_utc:                  当前 UTC 日期（用于异常提示，可注入便于测试）

    Returns:
        (判定结果, 人类可读的说明)

    判定逻辑：
        1. 数据源无数据                          → STALE_DATA
        2. state 无基线（首次运行）               → FIRST_RUN（继续执行）
        3. 最新交易日 == 上次已处理               → NOT_TRADING_DAY（跳过）
        4. 最新交易日 < 上次已处理                → STALE_DATA（数据源异常回退）
        5. 最新交易日 > 上次已处理                → NEW_TRADING_DAY（继续执行）

    ⚠️ 两个日期参数都必须先归一化为 date 才能比较。
       调用方（compute.py）传入的是 strftime 得到的**字符串**，
       若直接用字符串比较，`<` / `>` 会因 str 与 date 类型不匹配而抛 TypeError。
    """
    if latest_trade_date is None:
        return "STALE_DATA", "数据源未返回任何交易日"

    if not last_processed_trade_date:
        return "FIRST_RUN", f"无历史基线，按新交易日处理（{latest_trade_date}）"

    latest = _to_date(latest_trade_date)
    last = _to_date(last_processed_trade_date)

    if latest is None or last is None:
        bad = latest_trade_date if latest is None else last_processed_trade_date
        return "FIRST_RUN", f"日期格式异常（{bad}），按新交易日处理"

    if latest == last:
        return (
            "NOT_TRADING_DAY",
            f"最新交易日 {latest} 与上次已处理日相同，"
            f"说明前一交易日为休市日，跳过本次执行",
        )

    if latest < last:
        return (
            "STALE_DATA",
            f"数据源返回的最新交易日 {latest} 早于上次已处理日 {last}，"
            f"疑似数据源异常",
        )

    return (
        "NEW_TRADING_DAY",
        f"检测到新交易日 {latest}（上次处理至 {last}），继续执行",
    )


# ---------------------------------------------------------------------------
# 辅助：计算「应处理的目标交易日」
# ---------------------------------------------------------------------------

def expected_trade_date(beijing_trigger: datetime) -> date:
    """
    由北京时间触发时刻推算应处理的美股交易日。

    美股夏令时收盘为北京时间 04:00，冬令时为 05:00。
    本任务在北京时间 07:50 触发（周二至周六），
    已晚于当日凌晨的收盘，因此对应的美股交易日是**北京时间触发日的前一天**。

    例：
        北京 2026-09-08 07:50（周二）→ 目标美股交易日 2026-09-07（周一）
        但若 09-07 为劳动节休市 → 实际最新交易日退回到 09-04（周五）

    注意：
        1. 本函数只做「预期推算」，实际判定仍以数据源返回的日历为准。
        2. 触发时刻须晚于当日美股收盘（冬令时最晚 05:00），否则「前一天」
           的假设不成立。07:50 满足该前提，且留有 2 小时以上的缓冲。
    """
    return (beijing_trigger - timedelta(days=1)).date()


def describe_beijing_schedule() -> str:
    """返回当前调度配置的说明，便于日志排查。"""
    return (
        "调度：北京时间 07:50，周二至周六\n"
        "对应 UTC cron：50 23 * * 1-5（UTC 周一至周五 23:50）\n"
        "目标：美股前一交易日的收盘数据"
    )
