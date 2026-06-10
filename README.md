# 🛡 AIGuardian — AIエージェント・コントロールダッシュボード

社内AIエージェントの**重複・野良AI検知（ShadowAI_Detector）**と、
エージェント同士の**無限ループ遮断（LoopBreaker_Proxy）**を統合した軽量Webダッシュボード。

## クイックスタート

```bash
pip install -r requirements.txt
python app.py
```

→ ブラウザで `http://127.0.0.1:8787` が自動的に開きます。

画面上の **「⚡ 無限ループをシミュレート」** ボタンで、
同一パターンのラリー3回検知 → 強制遮断 → 管理者通知 の一連の動きを体験できます。

## テスト

```bash
python tests/test_aiguardian.py   # セキュリティロジック回帰テスト 17件
```

## 導入手順・実データ接続・チューニング

情シス向けの詳細は **[GOVERNANCE_SETUP.md](GOVERNANCE_SETUP.md)** を参照してください。
