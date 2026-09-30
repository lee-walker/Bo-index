"""
Bo 配对动能指标 — 状态读写与幂等去重

GitHub Actions 每次运行都是全新容器，没有跨运行的内存。
因此「昨日信号值」「上次推送记录」必须落盘到 state.json 并随仓库提交。

本模块同时承担幂等保护：同一天的推送键若已存在，则拒绝重复推送
（cron 重复触发、手动 dispatch 与定时任务撞车都会导致重复运行）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path

STATE_VERSION = 2

#: 推送键的形态："{trade_date}|{push_type}"
PushKey = str


@dataclass
class State:
    # -- 上次已确认的基线（推送完成或判定无需推送后才推进）--
    last_push_key: str = ""
    last_signal_vtv: float | None = None
    last_signal_cgdv: float | None = None
    last_regime: str = ""
    last_trade_date: str = ""
    consecutive_failures: int = 0

    # -- 本次运行由 compute.py 写入的待确认值 --
    # 分两段存储的原因：穿越判定需要「昨日 vs 今日」对比。
    # compute.py 写完 pending 后，notify.py 才能用 (last_*, pending_*) 做判断，
    # 判完再决定是否推进。顺序颠倒会导致永远检测不到穿越。
    pending_signal_vtv: float | None = None
    pending_signal_cgdv: float | None = None
    pending_regime: str = ""
    pending_trade_date: str = ""
    pending_source: str = ""
    pending_degraded: bool = False

    # -- KO 警报的边沿检测状态 --
    # 实测发现 KO_ROC20 会在阈值上方连续停留（历史样本中最长连续 21 个交易日）。
    # 若采用电平触发，用户会连续三周每天收到高优先级警报，必然导致关闭通知。
    # 因此改为边沿触发：只在「未触发 → 触发」当天告警，持续期间降级为普通播报。
    ko_was_active: bool = False

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_version"] = STATE_VERSION
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "State":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    # -- 业务方法 ----------------------------------------------------------

    @staticmethod
    def make_push_key(trade_date: str, push_type: str) -> PushKey:
        return f"{trade_date}|{push_type}"

    def already_pushed(self, trade_date: str, push_type: str) -> bool:
        return self.last_push_key == self.make_push_key(trade_date, push_type)

    def mark_pushed(self, trade_date: str, push_type: str) -> None:
        self.last_push_key = self.make_push_key(trade_date, push_type)

    def commit_pending(self) -> None:
        """把 pending 值推进为已确认基线。推送与否都应调用，避免基线漂移。"""
        self.last_signal_vtv = self.pending_signal_vtv
        self.last_signal_cgdv = self.pending_signal_cgdv
        self.last_regime = self.pending_regime
        self.last_trade_date = self.pending_trade_date

    def detect_crossovers(self) -> dict:
        """
        用「已确认基线 vs 本次待确认值」判定穿越。

        Returns:
            {"vtv": "NONE"|"CROSS_DOWN"|"CROSS_UP", "cgdv": ...}
        """
        return {
            "vtv": self._cross(self.last_signal_vtv, self.pending_signal_vtv),
            "cgdv": self._cross(self.last_signal_cgdv, self.pending_signal_cgdv),
        }

    @staticmethod
    def _cross(prev: float | None, cur: float | None) -> str:
        from indicators import sign
        if prev is None or cur is None:
            return "NONE"
        s_prev, s_cur = sign(prev), sign(cur)
        if s_cur < 0 and s_prev >= 0:
            return "CROSS_DOWN"
        if s_cur > 0 and s_prev <= 0:
            return "CROSS_UP"
        return "NONE"


def load_state(path: Path) -> State:
    """读取状态文件。文件缺失或损坏时返回全新状态，不抛异常。"""
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("_version") != STATE_VERSION:
            # 版本不匹配：保守起见重置，宁可多推一条也不要静默丢数据
            return State()
        return State.from_dict(raw)
    except (json.JSONDecodeError, OSError, TypeError):
        return State()


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_json(path: Path, payload: dict) -> None:
    """统一的 JSON 落盘：UTF-8、缩进 2、末尾换行（利于 git diff 可读性）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
