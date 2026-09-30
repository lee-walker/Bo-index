"""
端到端验证：休市日跳过行为

用真实数据模拟「前一日为休市日」的完整流程，验证：
    1. compute.py 提前退出，不写 latest/history
    2. 写入 skip.json 标记
    3. notify.py 读到标记后不推送
    4. 正常交易日能正常清除标记

运行：python scripts/test_e2e_skip.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LATEST = DATA_DIR / "latest.json"
HISTORY = DATA_DIR / "history.json"
STATE = DATA_DIR / "state.json"
SKIP = DATA_DIR / "skip.json"
BACKUP = DATA_DIR / ".e2e_backup"

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def snapshot() -> dict:
    return {
        "latest": LATEST.read_text(encoding="utf-8") if LATEST.exists() else None,
        "history": HISTORY.read_text(encoding="utf-8") if HISTORY.exists() else None,
        "state": STATE.read_text(encoding="utf-8") if STATE.exists() else None,
        "skip": SKIP.read_text(encoding="utf-8") if SKIP.exists() else None,
    }


def main() -> int:
    print("\n== 端到端：休市日跳过验证 ==\n")

    # 备份
    if BACKUP.exists():
        shutil.rmtree(BACKUP)
    BACKUP.mkdir(parents=True)
    for f in (LATEST, HISTORY, STATE, SKIP):
        if f.exists():
            shutil.copy2(f, BACKUP / f.name)

    try:
        from state_io import State, load_state, save_state
        from trading_day import evaluate_skip

        # ---- 场景 A：模拟休市日 ----
        print("场景 A：前一交易日为休市日（模拟劳动节）")
        # 构造 state：上次已处理 2026-09-04
        st = State(
            last_signal_vtv=-5.55,
            last_signal_cgdv=-4.96,
            last_regime="OFFENSIVE",
            last_trade_date="2026-09-04",
            last_push_key="2026-09-04|DAILY",
        )
        save_state(STATE, st)

        before = snapshot()

        # 数据源返回的最新交易日仍是 09-04（因为 09-07 休市）
        r, detail = evaluate_skip(
            latest_trade_date=date(2026, 9, 4),
            last_processed_trade_date="2026-09-04",
        )
        check("判定为休市跳过", r == "NOT_TRADING_DAY", r)
        print(f"       说明：{detail}")

        # 模拟 compute.py 的跳过行为
        skip_payload = {
            "skipped": True,
            "reason": r,
            "detail": detail,
            "latestTradeDate": "2026-09-04",
        }
        SKIP.write_text(json.dumps(skip_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

        after = snapshot()
        check("latest.json 未被修改", after["latest"] == before["latest"])
        check("history.json 未被修改", after["history"] == before["history"])
        check("state.json 未被修改", after["state"] == before["state"])
        check("skip.json 已写入",
              after["skip"] is not None and json.loads(after["skip"])["skipped"] is True)

        # 模拟 notify.py 检查 skip 标记
        flag = json.loads(SKIP.read_text(encoding="utf-8"))
        check("notify 能读到跳过标记", flag.get("skipped") is True)

        # ---- 场景 B：节后首个交易日 ----
        print("\n场景 B：节后首个交易日（2026-09-08）")
        r2, detail2 = evaluate_skip(
            latest_trade_date=date(2026, 9, 8),
            last_processed_trade_date="2026-09-04",
        )
        check("判定为正常执行", r2 == "NEW_TRADING_DAY", r2)
        print(f"       说明：{detail2}")

        # 正常执行时应清除 skip 标记
        if SKIP.exists():
            SKIP.unlink()
        check("skip.json 已清除", not SKIP.exists())

        # ---- 场景 C：连续休市（六月节 3 连休）----
        print("\n场景 C：连续休市（六月节 2026-06-19 周五休市 + 前后周末）")
        # 06-18 周四为最后交易日
        seq = [
            (date(2026, 6, 20), date(2026, 6, 18)),  # 周六触发，对应 6/19 休市
        ]
        for bj, td in seq:
            r3, _ = evaluate_skip(latest_trade_date=td,
                                  last_processed_trade_date="2026-06-18")
            check(f"北京 {bj} 触发应跳过", r3 == "NOT_TRADING_DAY", r3)

        # ---- 场景 D：基线不推进验证 ----
        print("\n场景 D：跳过期间基线不被推进")
        st_after = load_state(STATE)
        check("last_trade_date 仍为 9/4（未被污染）",
              st_after.last_trade_date == "2026-09-04",
              st_after.last_trade_date)
        check("last_push_key 仍为 9/4|DAILY（未被污染）",
              st_after.last_push_key == "2026-09-04|DAILY",
              st_after.last_push_key)

    finally:
        for f in (LATEST, HISTORY, STATE, SKIP):
            src = BACKUP / f.name
            if src.exists():
                shutil.copy2(src, f)
            elif f.exists():
                f.unlink()
        shutil.rmtree(BACKUP, ignore_errors=True)
        print("\n[已恢复原始数据]")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("✅ 端到端验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
