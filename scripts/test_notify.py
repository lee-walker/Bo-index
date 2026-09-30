"""
notify.py 分支逻辑验证（不真实发送推送）

通过 monkeypatch 拦截 push 调用，验证四种推送类型的选择逻辑与去重行为。

运行：python scripts/test_notify.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify  # noqa: E402
from state_io import State, save_state, load_state  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LATEST = DATA_DIR / "latest.json"
STATE = DATA_DIR / "state.json"
BACKUP = DATA_DIR / ".test_backup"

FAILED: list[str] = []
SENT: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_push(title, body, level="active"):
    SENT.append({"title": title, "body": body, "level": level})


def base_latest(**over) -> dict:
    d = {
        "schemaVersion": 2,
        "tradeDate": "2026-09-30",
        "isDegraded": False,
        "regime": "OFFENSIVE",
        "regimeText": "进攻阶段（成长端主导）",
        "crossoverEvent": "NONE",
        "koAlert": {"roc20": -1.5, "isTriggered": False, "threshold": 7.0},
        "quotes": {
            "QQQ": {"price": 737.93, "changePct": 0.19},
            "VTV": {"price": 217.89, "changePct": -0.22},
            "CGDV": {"price": 49.18, "changePct": 0.24},
            "KO": {"price": 86.84, "changePct": -0.39},
        },
        "indicators": {
            "vtvPair": {"ratio": 0.2953, "roc20": -5.88, "signal": -5.70},
            "cgdvPair": {"ratio": 0.0666, "roc20": -4.31, "signal": -4.86},
        },
    }
    d.update(over)
    return d


def run_case(latest: dict, state: State) -> dict:
    SENT.clear()
    LATEST.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
    save_state(STATE, state)
    notify.push = fake_push
    notify.main()
    return SENT[0] if SENT else {}


def main() -> int:
    if BACKUP.exists():
        shutil.rmtree(BACKUP)
    BACKUP.mkdir(parents=True)
    for f in (LATEST, STATE):
        if f.exists():
            shutil.copy2(f, BACKUP / f.name)

    try:
        print("\n== notify.py 分支验证 ==\n")

        # --- 1. 常规日 ---
        print("1. 常规日（无穿越 / 无 KO 警报）")
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0)
        st.pending_signal_vtv = -5.7
        st.pending_signal_cgdv = -4.86
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(), st)
        check("发送了一条", bool(got))
        check("标题为常规播报", "美股配对动能" in got.get("title", ""), got.get("title"))
        check("优先级 active", got.get("level") == "active", got.get("level"))
        check("正文含免责声明", "不构成任何投资建议" in got.get("body", ""))
        check("正文含状态", "进攻阶段" in got.get("body", ""))

        # --- 2. KO 边沿 ---
        print("\n2. KO 首次进入阈值（边沿）")
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0, ko_was_active=False)
        st.pending_signal_vtv = -5.7
        st.pending_signal_cgdv = -4.86
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(koAlert={"roc20": 7.4, "isTriggered": True,
                                            "threshold": 7.0}), st)
        check("标题为 KO 警报", "KO" in got.get("title", ""), got.get("title"))
        check("优先级 timeSensitive", got.get("level") == "timeSensitive",
              got.get("level"))
        check("正文说明后续转常规", "不再重复告警" in got.get("body", ""))

        # --- 3. KO 持续（不应重复告警）---
        print("\n3. KO 持续区间内（不应重复告警）")
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0, ko_was_active=True)
        st.pending_signal_vtv = -5.7
        st.pending_signal_cgdv = -4.86
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(koAlert={"roc20": 7.5, "isTriggered": True,
                                            "threshold": 7.0}), st)
        check("降为常规播报", "美股配对动能" in got.get("title", ""), got.get("title"))
        check("优先级降为 active", got.get("level") == "active", got.get("level"))
        check("正文标注仍在区间内", "仍在极端避险区间内" in got.get("body", ""))

        # --- 4. 穿越 ---
        print("\n4. 信号穿越零轴")
        st = State(last_signal_vtv=1.2, last_signal_cgdv=-2.0, ko_was_active=False)
        st.pending_signal_vtv = -0.4
        st.pending_signal_cgdv = -2.5
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(), st)
        check("标题含下穿零轴", "下穿零轴" in got.get("title", ""), got.get("title"))
        check("优先级 timeSensitive", got.get("level") == "timeSensitive",
              got.get("level"))
        check("不含买卖字样",
              not any(w in got.get("body", "") for w in ["买入", "卖出", "建仓", "止盈"]),
              got.get("body"))

        # --- 5. 降级 ---
        print("\n5. 数据降级")
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0)
        st.pending_signal_vtv = -5.0
        st.pending_signal_cgdv = -4.0
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(isDegraded=True,
                                   degradedReason="全部数据源不可用"), st)
        check("标题含数据延迟", "延迟" in got.get("title", ""), got.get("title"))
        check("优先级 passive", got.get("level") == "passive", got.get("level"))

        # --- 6. 幂等去重 ---
        print("\n6. 幂等去重（模拟 cron 重复触发）")
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0, ko_was_active=False)
        st.pending_signal_vtv = -5.7
        st.pending_signal_cgdv = -4.86
        st.pending_trade_date = "2026-09-30"
        # 第一次
        got = run_case(base_latest(), st)
        check("首次正常发送", bool(got))
        # 第二次：state 已被上一次运行写入 last_push_key
        st_after = load_state(STATE)
        check("push_key 已写入", st_after.last_push_key == "2026-09-30|DAILY",
              st_after.last_push_key)
        SENT.clear()
        LATEST.write_text(json.dumps(base_latest(), ensure_ascii=False), encoding="utf-8")
        # 模拟重复运行：pending 会被 compute.py 重新填入
        st_after.pending_signal_vtv = -5.7
        st_after.pending_signal_cgdv = -4.86
        st_after.pending_trade_date = "2026-09-30"
        save_state(STATE, st_after)
        notify.push = fake_push
        notify.main()
        check("重复运行被去重拦截", len(SENT) == 0, f"实际发送 {len(SENT)} 条")

        # --- 7. 文案合规扫描 ---
        print("\n7. 文案合规扫描")
        forbidden = ["建仓", "加仓", "减仓", "平仓", "清仓", "调仓",
                     "买入", "卖出", "抄底", "逃顶", "止盈", "止损",
                     "目标价", "建议买", "推荐买", "必涨", "稳赚"]
        st = State(last_signal_vtv=-5.0, last_signal_cgdv=-4.0, ko_was_active=False)
        st.pending_signal_vtv = -5.7
        st.pending_signal_cgdv = -4.86
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(), st)
        hits = [w for w in forbidden if w in got.get("body", "") or w in got.get("title", "")]
        check("常规播报无违禁词", not hits, f"命中 {hits}")

        st = State(last_signal_vtv=1.2, last_signal_cgdv=-2.0, ko_was_active=False)
        st.pending_signal_vtv = -0.4
        st.pending_signal_cgdv = -2.5
        st.pending_trade_date = "2026-09-30"
        got = run_case(base_latest(), st)
        hits = [w for w in forbidden if w in got.get("body", "") or w in got.get("title", "")]
        check("穿越播报无违禁词", not hits, f"命中 {hits}")

    finally:
        for f in (LATEST, STATE):
            src = BACKUP / f.name
            if src.exists():
                shutil.copy2(src, f)
        shutil.rmtree(BACKUP, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("✅ 全部测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
