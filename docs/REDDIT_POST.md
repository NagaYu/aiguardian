# Reddit 投稿ドラフト

実物のスクリーンショット（`docs/dashboard-overview.png` または `docs/dashboard-loopbreaker.png`）を
必ず1枚添付してください。Redditは画像なしだとほぼスルーされます。

投稿先の目安: **r/SideProject**, **r/Python**, **r/selfhosted**, **r/LocalLLaMA**（自作ツール紹介に寛容）。
避けたほうが無難: r/cybersecurity / r/devops（実運用実績を厳しく問われやすい）。

英語圏向けを主、日本語版は r/japanlife などの補助用です。

---

## 案A（英語 / r/SideProject・r/Python 向け）

**Title:**
I built a tiny dashboard that detects "shadow AI agents" and kills runaway agent-to-agent loops — no embeddings, 2 deps, runs offline

**Body:**

At work we ended up with a pile of internal custom-GPTs and automation agents that nobody fully tracks. Two failure modes kept worrying me:

1. **Duplicated / stale agents** — three different teams each built their own "expense bot", some still pointing at outdated policies.
2. **Agent-to-agent infinite loops** — two auto-reply bots ping-ponging "received your message" forever and quietly burning API budget.

So I built **AIGuardian**, a small FastAPI dashboard with two engines:

- **ShadowAI_Detector** — vectorizes agent prompts (TF-IDF, with CJK bigrams so it works on Japanese too) and clusters near-duplicates by cosine similarity. Plus rule-based flags for hardcoded API keys, jailbreak instructions, outdated policy references, and "no registered owner" agents. It also estimates how much you'd save by merging duplicate clusters.
- **LoopBreaker_Proxy** — agents send through `POST /api/proxy/message`. It normalizes message bodies (so just changing a date/ID doesn't evade detection) and **cuts the connection the moment the same pattern repeats 3 times**, then notifies an admin (and a webhook).

There's a **"Simulate infinite loop"** button so you can watch the detect → block → alert flow live.

**Being honest about scope:** the detection is TF-IDF + heuristics, *not* LLM/embedding semantic analysis. That was a deliberate trade-off — I wanted zero external deps, fully offline, and instant startup (it's literally `pip install -r requirements.txt && python app.py`). It's a proof-of-concept running on demo data, not a battle-tested product. There's no auth on the dashboard yet (put it behind a reverse proxy).

Repo (MIT): https://github.com/NagaYu/aiguardian

Would love feedback — especially: is the 3-strikes-same-pattern heuristic too naive for real loop detection? What would you want before trusting something like this in front of real agents?

---

## 案B（英語 / r/LocalLLaMA・r/selfhosted 向け・短め）

**Title:**
AIGuardian: self-hosted dashboard to catch duplicate "shadow" AI agents + auto-kill runaway agent loops (FastAPI, offline, MIT)

**Body:**

Small weekend-ish project. Two things it does:

- **Finds duplicate / risky internal agents** — TF-IDF + cosine similarity over agent prompts to cluster near-identical ones, plus rule flags (hardcoded keys, jailbreak prompts, stale policy refs, ownerless agents).
- **Breaks infinite agent loops** — proxy that watches agent-to-agent messages and blocks the pair after the same pattern repeats 3x, with admin/webhook alerts. There's a button to simulate a loop and watch it get cut.

No embeddings, no external API — just `fastapi` + `uvicorn`, runs fully offline. `pip install -r requirements.txt && python app.py` and a dashboard opens.

It's a proof-of-concept on demo data (detection is heuristic, not semantic; no auth yet). Sharing mainly for feedback on the approach.

MIT, repo: https://github.com/NagaYu/aiguardian

---

## 案C（日本語 / 個人開発系コミュニティ・X/Qiita 転用可）

**タイトル:**
社内に乱立した「野良AIエージェント」の重複検知と、エージェント同士の無限ループ遮断をやる軽量ダッシュボードを作った（FastAPI / 依存2つ / オフライン動作）

**本文:**

社内のカスタムGPTや自動化エージェントが増えすぎて、

- 同じ機能のボットが部署ごとに乱立（古い規程を参照したままのものも）
- 自動返信ボット同士が「受領しました」を打ち合って無限ループ → API課金が静かに増える

…という不安があったので、対策ダッシュボードを作りました。

**2つの機能:**
- **ShadowAI_Detector** — プロンプトをTF-IDF（日本語は文字バイグラム）でベクトル化してコサイン類似度で重複クラスタを検出。加えてAPIキー直書き・脱獄指示・古い規程参照・所有者不明の野良AIをルールで検知し、重複統合でいくら浮くかも概算
- **LoopBreaker_Proxy** — エージェント間通信をプロキシ経由にして常時監視。本文を正規化（日付や番号だけ変えても回避不可）し、同一パターンのラリーが3回続いた瞬間に遮断＋管理者通知。画面の「無限ループをシミュレート」ボタンで検知→遮断の流れを体験できます

**割り切り（正直なところ）:** 検知はTF-IDF + ルールベースで、LLM/embeddingによる意味理解はしていません。依存ゼロ・オフライン・即起動を優先した結果です。デモデータで動くプロトタイプで、認証もまだ付けていません（公開時はリバプロ配下推奨）。

`pip install -r requirements.txt && python app.py` だけで起動します。
MIT・リポジトリ: https://github.com/NagaYu/aiguardian

「3回同一パターンで遮断」のヒューリスティックが実運用で妥当か、フィードバックもらえると嬉しいです。

---

## 投稿のコツ（自分用メモ）

- **画像必須。** ヒーロー画像は `docs/dashboard-overview.png`、機能が伝わるのは `docs/dashboard-loopbreaker.png`
- タイトルに「企業向けソリューション」と書かない。「a small project / proof-of-concept」のトーンが一番伸びる
- **できないことを先に書く**と逆に信頼される（embeddingではない・認証なし・PoC）
- 投稿後30〜60分はコメントに張り付いて即レス。最初の反応速度がスコアを左右する
- 質問形で締める（「この遮断ロジックは甘い？」）とコメントが付きやすい
- r/Python は自己宣伝に厳しめ。サブのルールと曜日（週末の午前帯/ETが伸びやすい）を確認してから
