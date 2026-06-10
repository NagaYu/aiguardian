"""ShadowAI_Detector — 野良AI・重複エージェント検知エンジン

社内に乱立したカスタムGPT / AIエージェントのプロンプトと参照ソースをスキャンし、
  1. プロンプトのベクトル化（TF-IDF: 単語 + CJKバイグラム）→ コサイン類似度で重複検知
  2. 危険な指示・埋め込みシークレット・古い規約/データソースのルールベース検査
を行い、リスクスコアとアラートを生成する。

外部MLライブラリ非依存（純Python実装）なので、閉域網の社内サーバでもそのまま動く。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

# ---------------------------------------------------------------------------
# 検知ルール定義
# ---------------------------------------------------------------------------

# プロンプト内の危険な指示・情報漏えいパターン
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"(ignore|disregard)\s+(all\s+)?(previous|prior)\s+instructions", "ジェイルブレイク指示（ignore previous instructions）"),
    (r"前の(指示|命令|制約)を(無視|忘れ)", "ジェイルブレイク指示（制約の無視）"),
    (r"(制限|制約|フィルタ|安全機構)を(解除|無効|バイパス|回避)", "安全機構のバイパス指示"),
    (r"sk-[A-Za-z0-9]{16,}", "APIキーのハードコーディング"),
    (r"(password|パスワード)\s*[:=：]\s*\S+", "パスワードの平文埋め込み"),
    (r"(認証|アクセス制限|ペイウォール)を(回避|突破|無視)", "認証回避の指示"),
    (r"(社外秘|機密情報|個人情報).{0,12}(外部|社外|第三者|そのまま).{0,8}(送信|共有|出力|転送)", "機密情報の外部送信指示"),
    (r"\bDAN\b.{0,20}(mode|モード)", "DANモード（脱獄プロンプト）"),
]

# 参照ソース・プロンプト内の「古い・廃止済み」根拠パターン
DEPRECATED_PATTERNS: list[tuple[str, str]] = [
    (r"(旧|改正前の?)(個人情報保護|労働基準|就業規則|社内規程|民法|規約)", "改正前の規程・法令を参照"),
    (r"(2019|2020|平成\d+)年[度版]*(の|版)?(規程|規則|規約|ガイドライン|マニュアル)", "古い年度の規程文書を参照"),
    (r"text-davinci-00\d|gpt-3(\.5)?-turbo-0[36]\d\d|code-davinci", "廃止済みAIモデルAPIを参照"),
    (r"(廃止|失効|無効化)(済み|された)(規程|API|システム|サーバ)", "廃止済みシステムへの依存"),
]

# 参照ソースがこの日数より古ければ「鮮度切れ」とみなす（既定: 365日）
STALE_SOURCE_DAYS = 365

# この類似度以上のプロンプトペアを「重複候補」とする
DUPLICATE_THRESHOLD = 0.60

# リスクスコア配点
SCORE_DANGEROUS = 40
SCORE_DEPRECATED = 25
SCORE_STALE = 15
SCORE_NO_OWNER = 25
SCORE_DUPLICATE = 10
SCORE_MAX = 100


# ---------------------------------------------------------------------------
# テキストのベクトル化（TF-IDF）
# ---------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


def tokenize(text: str) -> list[str]:
    """英数字は単語単位、日本語（CJK）は文字バイグラムでトークン化する。

    形態素解析器なしで日本語の類似比較を成立させるための実装。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    tokens = _ASCII_WORD.findall(text)
    cjk_runs = re.findall(r"[぀-ヿ㐀-鿿]{2,}", text)
    for run in cjk_runs:
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def tfidf_vectors(documents: list[str]) -> list[dict[str, float]]:
    """文書群をTF-IDFベクトル（疎ベクトル: dict）に変換する。"""
    token_lists = [tokenize(doc) for doc in documents]
    n_docs = max(len(documents), 1)
    df: Counter[str] = Counter()
    for tokens in token_lists:
        df.update(set(tokens))

    vectors: list[dict[str, float]] = []
    for tokens in token_lists:
        tf = Counter(tokens)
        total = max(sum(tf.values()), 1)
        vec = {
            term: (count / total) * (math.log((1 + n_docs) / (1 + df[term])) + 1.0)
            for term, count in tf.items()
        }
        vectors.append(vec)
    return vectors


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        a, b = b, a
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# スキャン結果モデル
# ---------------------------------------------------------------------------

@dataclass
class AgentRisk:
    agent_id: str
    name: str
    owner: str
    department: str
    score: int = 0
    level: str = "ok"  # ok / warning / critical
    findings: list[dict] = field(default_factory=list)

    def add(self, points: int, category: str, detail: str) -> None:
        # 同一所見の二重計上を防ぐ（プロンプトとソース名の両方でヒットした場合など）
        if any(f["category"] == category and f["detail"] == detail for f in self.findings):
            return
        self.score = min(SCORE_MAX, self.score + points)
        self.findings.append({"category": category, "detail": detail, "points": points})


def _risk_level(score: int) -> str:
    if score >= 60:
        return "critical"
    if score >= 30:
        return "warning"
    return "ok"


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------

class ShadowAIDetector:
    """エージェント台帳をスキャンし、重複・リスク・アラートを算出する。"""

    def __init__(
        self,
        duplicate_threshold: float = DUPLICATE_THRESHOLD,
        stale_days: int = STALE_SOURCE_DAYS,
        today: date | None = None,
    ) -> None:
        self.duplicate_threshold = duplicate_threshold
        self.stale_days = stale_days
        self.today = today or date.today()

    # -- 個別チェック -------------------------------------------------------

    def _scan_text_rules(self, risk: AgentRisk, text: str) -> None:
        for pattern, label in DANGEROUS_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                risk.add(SCORE_DANGEROUS, "dangerous", label)
        for pattern, label in DEPRECATED_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                risk.add(SCORE_DEPRECATED, "deprecated", label)

    def _scan_sources(self, risk: AgentRisk, sources: Iterable[dict]) -> None:
        for src in sources:
            name = src.get("name", "(不明なソース)")
            updated = src.get("last_updated")
            if updated:
                try:
                    updated_date = datetime.strptime(str(updated), "%Y-%m-%d").date()
                except ValueError:
                    continue
                age = (self.today - updated_date).days
                if age > self.stale_days:
                    risk.add(
                        SCORE_STALE,
                        "stale",
                        f"参照ソース「{name}」が {age} 日間未更新（最終更新 {updated}）",
                    )
            self._scan_text_rules(risk, name)

    # -- スキャン本体 -------------------------------------------------------

    def scan(self, agents: list[dict]) -> dict:
        """台帳をスキャンし、ダッシュボード表示用のレポートを返す。"""
        risks: list[AgentRisk] = []
        for agent in agents:
            risk = AgentRisk(
                agent_id=agent["id"],
                name=agent.get("name", agent["id"]),
                owner=agent.get("owner") or "",
                department=agent.get("department") or "",
            )
            if not risk.owner or not risk.department:
                risk.add(
                    SCORE_NO_OWNER,
                    "shadow",
                    "所有者または所属部署が未登録（野良AIの疑い）",
                )
            self._scan_text_rules(risk, agent.get("prompt", ""))
            self._scan_sources(risk, agent.get("sources", []))
            risks.append(risk)

        # --- 重複検知（プロンプトのベクトル類似度） ---
        vectors = tfidf_vectors([a.get("prompt", "") for a in agents])
        uf = _UnionFind(len(agents))
        duplicate_pairs: list[dict] = []
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                sim = cosine_similarity(vectors[i], vectors[j])
                if sim >= self.duplicate_threshold:
                    uf.union(i, j)
                    duplicate_pairs.append(
                        {
                            "a": agents[i]["id"],
                            "a_name": agents[i].get("name", ""),
                            "b": agents[j]["id"],
                            "b_name": agents[j].get("name", ""),
                            "similarity": round(sim, 3),
                        }
                    )

        clusters_map: dict[int, list[int]] = defaultdict(list)
        for idx in range(len(agents)):
            clusters_map[uf.find(idx)].append(idx)
        clusters = []
        for members in clusters_map.values():
            if len(members) < 2:
                continue
            for idx in members:
                risks[idx].add(SCORE_DUPLICATE, "duplicate", "他エージェントと機能が重複")
            clusters.append(
                {
                    "agents": [
                        {
                            "id": agents[idx]["id"],
                            "name": agents[idx].get("name", ""),
                            "owner": agents[idx].get("owner") or "(未登録)",
                            "department": agents[idx].get("department") or "(不明)",
                            "monthly_cost_jpy": agents[idx].get("monthly_cost_jpy", 0),
                        }
                        for idx in members
                    ],
                    "wasted_cost_jpy": sum(
                        sorted(
                            (agents[idx].get("monthly_cost_jpy", 0) for idx in members),
                            reverse=True,
                        )[1:]
                    ),
                }
            )
        clusters.sort(key=lambda c: c["wasted_cost_jpy"], reverse=True)

        # --- レベル確定 & アラート化 ---
        alerts: list[dict] = []
        for risk in risks:
            risk.level = _risk_level(risk.score)
            for finding in risk.findings:
                if finding["category"] in ("dangerous", "shadow", "deprecated"):
                    alerts.append(
                        {
                            "source": "ShadowAI_Detector",
                            "severity": "critical" if finding["category"] == "dangerous" else "warning",
                            "agent_id": risk.agent_id,
                            "agent_name": risk.name,
                            "message": finding["detail"],
                        }
                    )
        for cluster in clusters:
            names = " / ".join(a["name"] for a in cluster["agents"])
            alerts.append(
                {
                    "source": "ShadowAI_Detector",
                    "severity": "warning",
                    "agent_id": cluster["agents"][0]["id"],
                    "agent_name": names,
                    "message": f"機能重複クラスタを検知（推定ムダ費用 ¥{cluster['wasted_cost_jpy']:,}/月）",
                }
            )

        severity_order = {"critical": 0, "warning": 1}
        alerts.sort(key=lambda a: severity_order.get(a["severity"], 9))

        return {
            "agents": [
                {
                    "id": r.agent_id,
                    "name": r.name,
                    "owner": r.owner or "(未登録)",
                    "department": r.department or "(不明)",
                    "score": r.score,
                    "level": r.level,
                    "findings": r.findings,
                    "monthly_cost_jpy": next(
                        (a.get("monthly_cost_jpy", 0) for a in agents if a["id"] == r.agent_id), 0
                    ),
                    "model": next((a.get("model", "") for a in agents if a["id"] == r.agent_id), ""),
                }
                for r in risks
            ],
            "duplicate_pairs": duplicate_pairs,
            "clusters": clusters,
            "alerts": alerts,
            "summary": {
                "total": len(agents),
                "critical": sum(1 for r in risks if r.level == "critical"),
                "warning": sum(1 for r in risks if r.level == "warning"),
                "ok": sum(1 for r in risks if r.level == "ok"),
                "duplicate_clusters": len(clusters),
                "wasted_cost_jpy": sum(c["wasted_cost_jpy"] for c in clusters),
            },
        }
