"""AIGuardian セキュリティロジックの自律テスト

実行: python -m pytest tests/ -v  または  python tests/test_aiguardian.py
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiguardian.detector import ShadowAIDetector, cosine_similarity, tfidf_vectors
from aiguardian.loopbreaker import LoopBreakerProxy
from aiguardian.sample_data import LOOP_SCENARIO, NORMAL_SCENARIO, SAMPLE_AGENTS


class TestShadowAIDetector(unittest.TestCase):
    def setUp(self):
        self.detector = ShadowAIDetector(today=date(2026, 6, 10))
        self.report = self.detector.scan(SAMPLE_AGENTS)

    def _agent(self, agent_id):
        return next(a for a in self.report["agents"] if a["id"] == agent_id)

    def test_duplicate_detection_finds_expense_bots(self):
        """経理アシスタントGPT と けいひ精算お助けボット は重複として検知される。"""
        pairs = {(p["a"], p["b"]) for p in self.report["duplicate_pairs"]}
        self.assertIn(("agt-001", "agt-002"), pairs)

    def test_unrelated_agents_not_duplicated(self):
        """翻訳アシスタントは経理ボットと重複扱いされない。"""
        for p in self.report["duplicate_pairs"]:
            self.assertNotIn("agt-008", (p["a"], p["b"]))

    def test_dangerous_prompt_is_critical(self):
        """APIキー埋め込み + 認証回避 + 所有者不明 の野良AIは critical。"""
        rogue = self._agent("agt-006")
        self.assertEqual(rogue["level"], "critical")
        categories = {f["category"] for f in rogue["findings"]}
        self.assertIn("dangerous", categories)
        self.assertIn("shadow", categories)

    def test_stale_source_detected(self):
        """2020年版就業規則を参照する人事ボットは鮮度切れ・廃止規程を検知。"""
        hr = self._agent("agt-005")
        categories = {f["category"] for f in hr["findings"]}
        self.assertIn("stale", categories)
        self.assertIn("deprecated", categories)

    def test_deprecated_law_reference_detected(self):
        """改正前の民法を参照する契約書チェッカーは deprecated 検知。"""
        legal = self._agent("agt-007")
        categories = {f["category"] for f in legal["findings"]}
        self.assertIn("deprecated", categories)

    def test_healthy_agent_is_ok(self):
        """最新ソースの翻訳アシスタントは正常判定。"""
        self.assertEqual(self._agent("agt-008")["level"], "ok")

    def test_alerts_generated(self):
        self.assertGreater(len(self.report["alerts"]), 0)
        severities = {a["severity"] for a in self.report["alerts"]}
        self.assertIn("critical", severities)

    def test_cluster_wasted_cost(self):
        """重複クラスタのムダ費用 = 最高額エージェント以外の合計。"""
        cluster = next(
            c for c in self.report["clusters"]
            if {a["id"] for a in c["agents"]} >= {"agt-001", "agt-002"}
        )
        self.assertEqual(cluster["wasted_cost_jpy"], 18500)  # min(18500, 22400)

    def test_cosine_similarity_sanity(self):
        vecs = tfidf_vectors(["経費精算の質問に答える", "経費精算の質問に回答する", "契約書をレビューする"])
        self.assertGreater(cosine_similarity(vecs[0], vecs[1]), cosine_similarity(vecs[0], vecs[2]))


class TestLoopBreakerProxy(unittest.TestCase):
    def setUp(self):
        self.proxy = LoopBreakerProxy()

    def test_loop_blocked_at_third_repeat(self):
        """同一パターン3回目のラリーで遮断される。"""
        results = [
            self.proxy.handle_message(s, r, c) for s, r, c in LOOP_SCENARIO[:6]
        ]
        # 5通目（営業側の同一パターン3回目）で遮断
        statuses = [r["status"] for r in results]
        self.assertIn("blocked", statuses)
        blocked_index = statuses.index("blocked")
        self.assertLessEqual(blocked_index, 5)
        # 遮断後は rejected
        self.assertEqual(statuses[-1], "rejected")

    def test_normal_traffic_not_blocked(self):
        """内容が毎回異なる健全な会話は遮断されない。"""
        for sender, recipient, content in NORMAL_SCENARIO:
            result = self.proxy.handle_message(sender, recipient, content)
            self.assertEqual(result["status"], "forwarded")
        self.assertEqual(len(self.proxy.blocked), 0)

    def test_numbers_do_not_evade_detection(self):
        """日付や数値だけ変えた同一パターンは検知をすり抜けられない。"""
        for i in range(3):
            result = self.proxy.handle_message(
                "A", "B", f"受領しました。整理番号 {1000 + i} で承りました。担当者よりご連絡します。"
            )
        self.assertEqual(result["status"], "blocked")

    def test_blocked_pair_rejects_and_accumulates_savings(self):
        for s, r, c in LOOP_SCENARIO[:6]:
            self.proxy.handle_message(s, r, c)
        snap_before = self.proxy.snapshot()
        attempts_before = snap_before["blocked_pairs"][0]["blocked_attempts"]
        self.proxy.handle_message(*LOOP_SCENARIO[6])
        snap_after = self.proxy.snapshot()
        self.assertEqual(
            snap_after["blocked_pairs"][0]["blocked_attempts"], attempts_before + 1
        )
        self.assertGreaterEqual(
            snap_after["saved_cost_jpy"], snap_before["saved_cost_jpy"]
        )

    def test_admin_notification_sent(self):
        """遮断時に管理者通知が記録され、フックが呼ばれる。"""
        received = []
        self.proxy.notify_hook = received.append
        for s, r, c in LOOP_SCENARIO[:6]:
            self.proxy.handle_message(s, r, c)
        self.assertEqual(len(self.proxy.notifications), 1)
        self.assertEqual(len(received), 1)
        self.assertIn("強制遮断", received[0]["title"])

    def test_unblock_restores_communication(self):
        for s, r, c in LOOP_SCENARIO[:6]:
            self.proxy.handle_message(s, r, c)
        a, b = LOOP_SCENARIO[0][0], LOOP_SCENARIO[0][1]
        self.assertTrue(self.proxy.unblock(a, b))
        result = self.proxy.handle_message(a, b, "新しい話題のメッセージです。次回の定例会議について。")
        self.assertEqual(result["status"], "forwarded")

    def test_notify_hook_failure_does_not_break_blocking(self):
        """Webhook通知の失敗が遮断処理を阻害しない。"""
        def failing_hook(_):
            raise RuntimeError("webhook down")
        self.proxy.notify_hook = failing_hook
        for s, r, c in LOOP_SCENARIO[:6]:
            result = self.proxy.handle_message(s, r, c)
        self.assertEqual(len(self.proxy.blocked), 1)

    def test_different_pairs_isolated(self):
        """ペアAB のループ検知はペアCD に影響しない。"""
        for i in range(3):
            self.proxy.handle_message("A", "B", "全く同じ自動返信メッセージです。確認しました。")
        result = self.proxy.handle_message("C", "D", "全く同じ自動返信メッセージです。確認しました。")
        self.assertEqual(result["status"], "forwarded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
