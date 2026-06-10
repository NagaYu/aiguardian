"""LoopBreaker_Proxy — エージェント間無限ループ遮断プロキシ

エージェント同士のメール / チャット自動返信を本プロキシ経由にすることで、
「同一パターンのラリー」を常時監視する。

検知ロジック:
  - 会話ペア（A↔B）ごとに直近メッセージの履歴を保持
  - メッセージ本文を正規化（数字・空白・記号を除去）→ 文字バイグラム集合に変換
  - 同一送信者から Jaccard 類似度が閾値以上のメッセージが繰り返された回数を数える
  - 同一パターンが閾値回数（既定 3 回）に達した瞬間、そのペアの通信を強制遮断し
    管理者へ通知（アラート + Webhook）

遮断後のメッセージはプロキシで弾かれ、APIコールが発生しないため
「遮断によって回避できたコスト」も推計して可視化する。
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field

# 同一パターンがこの回数現れたら遮断（「3回以上の同一パターンのラリー」要件）
REPEAT_THRESHOLD = 3
# 1会話ペアあたり保持する直近メッセージ数
HISTORY_WINDOW = 12
# 「同一パターン」とみなす本文類似度（Jaccard）
SIMILARITY_THRESHOLD = 0.72
# コスト推計: 1メッセージあたりの推定APIコスト計算用
COST_PER_1K_TOKENS_JPY = 0.45  # 円 / 1Kトークン（入出力込みの概算）
TOKEN_OVERHEAD = 350  # システムプロンプト等の固定オーバーヘッド
# 遮断しなかった場合にループが続くと仮定するラリー数（回避コスト推計用）
PROJECTED_LOOP_MESSAGES = 5000


def _normalize(text: str) -> str:
    """日時・数値・空白などラリーごとに揺れる部分を落として本文を正規化する。"""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\d+", "", text)
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"[!-/:-@\[-`{-~。、！？「」『』（）()]", "", text)
    return text


def message_signature(text: str) -> frozenset[str]:
    """正規化した本文の文字バイグラム集合（メッセージの指紋）。"""
    norm = _normalize(text)
    if len(norm) < 2:
        return frozenset([norm]) if norm else frozenset()
    return frozenset(norm[i : i + 2] for i in range(len(norm) - 1))


def signature_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def estimate_cost_jpy(content: str) -> float:
    """メッセージ1通の推定APIコスト（円）。日本語は1文字≒1トークンで概算。"""
    tokens = len(content) + TOKEN_OVERHEAD
    return tokens / 1000 * COST_PER_1K_TOKENS_JPY


@dataclass
class _Record:
    sender: str
    signature: frozenset[str]
    timestamp: float


@dataclass
class BlockInfo:
    pair: tuple[str, str]
    reason: str
    blocked_at: float
    rally_count: int
    burned_cost_jpy: float
    saved_cost_jpy: float
    blocked_attempts: int = 0
    sample_content: str = ""


@dataclass
class LoopBreakerProxy:
    repeat_threshold: int = REPEAT_THRESHOLD
    similarity_threshold: float = SIMILARITY_THRESHOLD
    history_window: int = HISTORY_WINDOW

    _history: dict[tuple[str, str], deque] = field(default_factory=lambda: defaultdict(deque))
    _pair_cost: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    _pair_messages: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    blocked: dict[tuple[str, str], BlockInfo] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    total_cost_jpy: float = 0.0
    total_messages: int = 0
    notify_hook: object = None  # callable(alert: dict) — Webhook等の外部通知
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------

    @staticmethod
    def pair_key(a: str, b: str) -> tuple[str, str]:
        return tuple(sorted((a, b)))  # type: ignore[return-value]

    def _emit(self, kind: str, **payload) -> dict:
        event = {"type": kind, "timestamp": time.time(), **payload}
        self.events.append(event)
        if len(self.events) > 400:
            del self.events[: len(self.events) - 400]
        return event

    def _notify_admin(self, block: BlockInfo) -> None:
        notice = {
            "channel": "admin",
            "timestamp": block.blocked_at,
            "title": "🚨 無限ループを強制遮断しました",
            "body": (
                f"{block.pair[0]} ↔ {block.pair[1]} 間で同一パターンのラリーを "
                f"{block.rally_count} 回検知したため通信を遮断しました。"
                f"消費済みコスト ¥{block.burned_cost_jpy:,.1f} / "
                f"推定回避コスト ¥{block.saved_cost_jpy:,.0f}"
            ),
        }
        self.notifications.append(notice)
        if callable(self.notify_hook):
            try:
                self.notify_hook(notice)
            except Exception:
                pass  # 通知失敗で遮断処理自体を止めない

    # ------------------------------------------------------------------

    def handle_message(self, sender: str, recipient: str, content: str) -> dict:
        """プロキシのエントリポイント。転送可否を判定して結果を返す。"""
        with self._lock:
            pair = self.pair_key(sender, recipient)

            # 遮断済みペア: APIコールを発生させず即時拒否
            if pair in self.blocked:
                block = self.blocked[pair]
                block.blocked_attempts += 1
                block.saved_cost_jpy += estimate_cost_jpy(content)
                self._emit(
                    "rejected",
                    sender=sender,
                    recipient=recipient,
                    content=content[:120],
                    reason="このペアは遮断中です（管理者の解除待ち）",
                )
                return {"status": "rejected", "pair": list(pair), "reason": "blocked_pair"}

            cost = estimate_cost_jpy(content)
            self.total_cost_jpy += cost
            self.total_messages += 1
            self._pair_cost[pair] += cost
            self._pair_messages[pair] += 1

            sig = message_signature(content)
            history = self._history[pair]

            # 同一送信者からの類似メッセージ回数（今回分を含む）
            repeats = 1 + sum(
                1
                for rec in history
                if rec.sender == sender
                and signature_similarity(rec.signature, sig) >= self.similarity_threshold
            )

            history.append(_Record(sender=sender, signature=sig, timestamp=time.time()))
            while len(history) > self.history_window:
                history.popleft()

            if repeats >= self.repeat_threshold:
                avg_cost = self._pair_cost[pair] / max(self._pair_messages[pair], 1)
                block = BlockInfo(
                    pair=pair,
                    reason=(
                        f"同一パターンのラリーを {repeats} 回検知"
                        f"（類似度閾値 {self.similarity_threshold:.0%}）"
                    ),
                    blocked_at=time.time(),
                    rally_count=repeats,
                    burned_cost_jpy=self._pair_cost[pair],
                    saved_cost_jpy=avg_cost * PROJECTED_LOOP_MESSAGES,
                    sample_content=content[:160],
                )
                self.blocked[pair] = block
                alert = {
                    "source": "LoopBreaker_Proxy",
                    "severity": "critical",
                    "agent_id": " ↔ ".join(pair),
                    "agent_name": " ↔ ".join(pair),
                    "message": (
                        f"無限ループ検知 → 強制遮断（ラリー{repeats}回 / "
                        f"消費 ¥{block.burned_cost_jpy:,.1f} / 回避見込 ¥{block.saved_cost_jpy:,.0f}）"
                    ),
                }
                self.alerts.append(alert)
                self._emit(
                    "blocked",
                    sender=sender,
                    recipient=recipient,
                    content=content[:120],
                    rally_count=repeats,
                    burned_cost_jpy=round(block.burned_cost_jpy, 1),
                    saved_cost_jpy=round(block.saved_cost_jpy),
                )
                self._notify_admin(block)
                return {
                    "status": "blocked",
                    "pair": list(pair),
                    "rally_count": repeats,
                    "reason": block.reason,
                }

            self._emit(
                "forwarded",
                sender=sender,
                recipient=recipient,
                content=content[:120],
                repeats=repeats,
                cost_jpy=round(cost, 2),
            )
            return {"status": "forwarded", "pair": list(pair), "repeats": repeats}

    # ------------------------------------------------------------------

    def unblock(self, a: str, b: str) -> bool:
        """管理者によるペアの遮断解除。会話履歴もリセットする。"""
        with self._lock:
            pair = self.pair_key(a, b)
            if pair not in self.blocked:
                return False
            del self.blocked[pair]
            self._history.pop(pair, None)
            self._pair_cost.pop(pair, None)
            self._pair_messages.pop(pair, None)
            self._emit("unblocked", sender=a, recipient=b, content="管理者がペアの遮断を解除")
            return True

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._pair_cost.clear()
            self._pair_messages.clear()
            self.blocked.clear()
            self.events.clear()
            self.alerts.clear()
            self.notifications.clear()
            self.total_cost_jpy = 0.0
            self.total_messages = 0

    def snapshot(self) -> dict:
        """ダッシュボード表示用の現在状態。"""
        with self._lock:
            return {
                "total_messages": self.total_messages,
                "total_cost_jpy": round(self.total_cost_jpy, 1),
                "blocked_pairs": [
                    {
                        "pair": list(b.pair),
                        "reason": b.reason,
                        "blocked_at": b.blocked_at,
                        "rally_count": b.rally_count,
                        "burned_cost_jpy": round(b.burned_cost_jpy, 1),
                        "saved_cost_jpy": round(b.saved_cost_jpy),
                        "blocked_attempts": b.blocked_attempts,
                        "sample_content": b.sample_content,
                    }
                    for b in self.blocked.values()
                ],
                "saved_cost_jpy": round(sum(b.saved_cost_jpy for b in self.blocked.values())),
                "events": list(self.events[-60:]),
                "alerts": list(self.alerts),
                "notifications": list(self.notifications[-20:]),
            }
