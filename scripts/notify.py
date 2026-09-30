"""
Bo 配对动能指标 — Bark 推送

时序约定（关键）：
    compute.py 已完成，state.json 中：
        last_*    = 上一次已确认的基线（昨日）
        pending_* = 本次算出的值（今日）

    本脚本用 (last_*, pending_*) 判定穿越，决定推送类型，
    推送后调用 commit_pending() 推进基线。

幂等保证：
    推送键 = "{trade_date}|{push_type}"，与 state.last_push_key 比对，
    相同则跳过。防止 cron 重复触发 / 手动 dispatch 撞车导致重复打扰。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

from indicators import REGIME_TEXT
from state_io import State, load_state, save_state

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
LATEST_PATH = DATA_DIR / "latest.json"
STATE_PATH = DATA_DIR / "state.json"
SKIP_FLAG_PATH = DATA_DIR / "skip.json"

#: 看板地址（Bark 通知点击后跳转）。首次部署后替换为你的 Pages 地址。
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "https://<your-github-username>.github.io/bo-momentum/"
)

DISCLAIMER = "本内容为公开市场指标的客观记录，不构成任何投资建议"

#: Bark 通知分组，同组通知在 iOS 通知中心自动折叠
BARK_GROUP = "BoMomentum"


# ---------------------------------------------------------------------------
# Bark 调用
# ---------------------------------------------------------------------------

def push(title: str, body: str, level: str = "active") -> None:
    """
    发送 Bark 通知。

    BARK_URL 形如 https://api.day.app/{device_key}
    官方公共服务器会记录内容到日志，因此本函数只应推送公开市场指标，
    禁止传入任何凭据或私有数据。
    """
    bark_url = os.environ.get("BARK_URL", "").strip()
    if not bark_url:
        raise RuntimeError("未配置 BARK_URL 环境变量")

    payload = {
        "title": title,
        "body": body,
        "level": level,          # passive | active | timeSensitive | critical
        "group": BARK_GROUP,
        "isArchive": 1,          # 服务端保留，便于回看历史播报
        "url": DASHBOARD_URL,
    }

    resp = requests.post(bark_url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # Bark 返回 {"code": 200, "message": "success", ...}
    if data.get("code") not in (200, None):
        raise RuntimeError(f"Bark 返回异常：{data}")

    print(f"[bark] 推送成功：{title}")


# ---------------------------------------------------------------------------
# 文案组装
# ---------------------------------------------------------------------------

def fmt_signal(v) -> str:
    return "—" if v is None else f"{v:+.2f}"


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def build_regular(latest: dict, ko_note: bool = False) -> tuple[str, str]:
    """常规日播报。ko_note=True 时表示 KO 警报仍在持续区间内。"""
    ind = latest["indicators"]
    q = latest["quotes"]
    ko = latest["koAlert"]

    title = f"美股配对动能 · {latest['tradeDate'][5:]}"
    lines = [
        f"状态：{latest['regimeText']}",
        f"VTV 通道：ROC {fmt_pct(ind['vtvPair']['roc20'])} / 信号 {fmt_signal(ind['vtvPair']['signal'])}",
        f"CGDV 通道：ROC {fmt_pct(ind['cgdvPair']['roc20'])} / 信号 {fmt_signal(ind['cgdvPair']['signal'])}",
        f"QQQ {q['QQQ']['price']}（{fmt_pct(q['QQQ']['changePct'])}）　"
        f"KO {q['KO']['price']}（{fmt_pct(q['KO']['changePct'])}）",
        f"KO 月涨幅 {fmt_pct(ko['roc20'])}（阈值 {ko['threshold']:+.2f}%）",
    ]
    if ko_note:
        lines.append("（KO 仍在极端避险区间内，本次为常规播报）")
    lines += ["", DISCLAIMER]
    return title, "\n".join(lines)


def build_crossover(latest: dict, which: str, direction: str) -> tuple[str, str]:
    """穿越日播报。which: 'vtv'|'cgdv'；direction: 'CROSS_DOWN'|'CROSS_UP'。"""
    label = "下穿零轴" if direction == "CROSS_DOWN" else "上穿零轴"
    channel = "VTV" if which == "vtv" else "CGDV"

    title = f"⚠️ 信号变化 · {channel} 通道{label}"
    ind = latest["indicators"]
    q = latest["quotes"]

    body = "\n".join([
        f"{channel} 通道信号穿越零轴（{label}）",
        f"当前信号：{fmt_signal(ind[f'{which}Pair']['signal'])}",
        f"当前状态：{latest['regimeText']}",
        f"QQQ {q['QQQ']['price']}（{fmt_pct(q['QQQ']['changePct'])}）　"
        f"KO {q['KO']['price']}（{fmt_pct(q['KO']['changePct'])}）",
        "",
        DISCLAIMER,
    ])
    return title, body


def build_ko_alert(latest: dict, is_new: bool = True) -> tuple[str, str]:
    """KO 极端避险警报（仅在边沿触发时调用）。"""
    ko = latest["koAlert"]
    q = latest["quotes"]

    title = "⚠️ KO 极端避险指标进入阈值区间"
    body = "\n".join([
        f"KO 近 20 交易日涨幅 {fmt_pct(ko['roc20'])}，"
        f"已进入 {ko['threshold']:+.2f}% 阈值区间",
        f"当前状态：{latest['regimeText']}",
        f"KO {q['KO']['price']}（{fmt_pct(q['KO']['changePct'])}）",
        f"QQQ {q['QQQ']['price']}（{fmt_pct(q['QQQ']['changePct'])}）",
        "",
        "后续若持续处于该区间内，将转为每日常规播报，不再重复告警。",
        "",
        DISCLAIMER,
    ])
    return title, body


def build_degraded(latest: dict) -> tuple[str, str]:
    """数据降级日播报。"""
    title = "美股配对动能 · 数据延迟"
    body = "\n".join([
        "⚠️ 数据源异常，本次展示为上次成功结果",
        f"数据日期：{latest.get('tradeDate', '—')}",
        f"原因：{latest.get('degradedReason', '未知')}",
        "",
        DISCLAIMER,
    ])
    return title, body


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    # ---- 休市跳过（双重保险）----
    # compute.py 在休市日会写入 skip.json 并提前退出。
    # 正常流程下工作流会跳过本步骤，但为防止有人手动单独运行 notify.py，
    # 或被跳过的工作流仍执行到此，这里再检查一次。
    if SKIP_FLAG_PATH.exists():
        try:
            flag = json.loads(SKIP_FLAG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            flag = {}
        if flag.get("skipped"):
            print(f"[skip] 前一交易日为休市日，不推送（{flag.get('detail', '')}）")
            return 0

    if not LATEST_PATH.exists():
        print("[fatal] latest.json 不存在，compute.py 可能未成功运行", file=sys.stderr)
        return 1

    latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    state = load_state(STATE_PATH)

    ko_triggered = bool(latest["koAlert"]["isTriggered"])
    ko_edge = ko_triggered and not state.ko_was_active      # 未触发 → 触发
    ko_persist = ko_triggered and state.ko_was_active       # 持续期间

    # 优先级：降级 > KO 边沿 > 穿越 > 常规
    # 注意 KO 用「边沿」而非「电平」：实测 KO_ROC20 曾连续 21 个交易日处于阈值上方，
    # 电平触发会连续三周每天发高优先级警报，用户必然关闭通知。
    if latest.get("isDegraded"):
        push_type = "DEGRADED"
        title, body = build_degraded(latest)
        level = "passive"
    elif ko_edge:
        push_type = "KO_ALERT_EDGE"
        title, body = build_ko_alert(latest, is_new=True)
        level = "timeSensitive"
    else:
        crosses = state.detect_crossovers()
        hit = next(
            ((ch, d) for ch, d in crosses.items() if d != "NONE"),
            None,
        )
        if hit:
            which, direction = hit
            push_type = direction
            title, body = build_crossover(latest, which, direction)
            level = "timeSensitive"
        elif ko_persist:
            # 警报持续中：仍每日播报，但降为普通优先级，并在正文标注仍在区间内
            push_type = "DAILY_KO_PERSIST"
            title, body = build_regular(latest, ko_note=True)
            level = "active"
        else:
            push_type = "DAILY"
            title, body = build_regular(latest)
            level = "active"

    trade_date = latest.get("tradeDate", "")

    # -- 幂等去重 --
    if state.already_pushed(trade_date, push_type):
        print(f"[skip] 已推送过（{trade_date}|{push_type}），跳过")
        state.ko_was_active = ko_triggered
        state.commit_pending()
        save_state(STATE_PATH, state)
        return 0

    try:
        push(title, body, level=level)
    except Exception as exc:  # noqa: BLE001
        # 推送失败不推进 push_key，下次运行会重试；但 KO 边沿状态也要跟着推进，
        # 否则次日会被当成「新的边沿」重复告警。
        print(f"[error] Bark 推送失败：{exc}", file=sys.stderr)
        state.ko_was_active = ko_triggered
        state.commit_pending()
        save_state(STATE_PATH, state)
        return 1

    state.mark_pushed(trade_date, push_type)
    state.ko_was_active = ko_triggered
    state.commit_pending()
    save_state(STATE_PATH, state)

    print(f"[ok] pushType={push_type} level={level} koActive={ko_triggered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
