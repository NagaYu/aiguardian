"""AIGuardian — AIエージェント・コントロールダッシュボード 起動エントリポイント

使い方:
    pip install -r requirements.txt
    python app.py
    → ブラウザで http://127.0.0.1:8787 が自動で開く

環境変数:
    AIGUARDIAN_PORT          リッスンポート（既定 8787）
    AIGUARDIAN_WEBHOOK_URL   遮断時に管理者通知をPOSTするWebhook URL（Slack等）
    AIGUARDIAN_NO_BROWSER    "1" でブラウザ自動起動を抑止
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from aiguardian.detector import ShadowAIDetector
from aiguardian.loopbreaker import LoopBreakerProxy
from aiguardian.sample_data import LOOP_SCENARIO, NORMAL_SCENARIO, SAMPLE_AGENTS

BASE_DIR = Path(__file__).resolve().parent
AGENTS_FILE = BASE_DIR / "data" / "agents.json"
PORT = int(os.environ.get("AIGUARDIAN_PORT", "8787"))
WEBHOOK_URL = os.environ.get("AIGUARDIAN_WEBHOOK_URL", "")

app = FastAPI(title="AIGuardian", version="1.0.0")

# ---------------------------------------------------------------------------
# 状態（シングルプロセス内のインメモリ管理）
# ---------------------------------------------------------------------------


def _webhook_notify(notice: dict) -> None:
    """遮断通知をWebhookへ転送（設定時のみ・ベストエフォート）。"""
    if not WEBHOOK_URL:
        return
    payload = json.dumps(
        {"text": f"[AIGuardian] {notice['title']}\n{notice['body']}"}
    ).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=5)


def _load_agents() -> list[dict]:
    """data/agents.json があればそれを、なければサンプル台帳を読み込む。"""
    if AGENTS_FILE.exists():
        with open(AGENTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return SAMPLE_AGENTS


detector = ShadowAIDetector()
proxy = LoopBreakerProxy()
proxy.notify_hook = _webhook_notify

_state_lock = threading.Lock()
_agents: list[dict] = _load_agents()
_scan_report: dict = detector.scan(_agents)
_simulation = {"running": False, "name": ""}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ProxyMessage(BaseModel):
    sender: str
    recipient: str
    content: str


class PairRequest(BaseModel):
    a: str
    b: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/state")
def get_state() -> JSONResponse:
    """ダッシュボードが1.5秒ごとにポーリングする統合状態。"""
    loop_state = proxy.snapshot()
    with _state_lock:
        report = _scan_report
        sim = dict(_simulation)
    alerts = list(loop_state["alerts"])[::-1] + report["alerts"]
    return JSONResponse(
        {
            "scan": report,
            "loop": loop_state,
            "alerts": alerts,
            "simulation": sim,
            "kpis": {
                "agents_total": report["summary"]["total"],
                "agents_critical": report["summary"]["critical"],
                "agents_warning": report["summary"]["warning"],
                "duplicate_clusters": report["summary"]["duplicate_clusters"],
                "wasted_cost_jpy": report["summary"]["wasted_cost_jpy"],
                "proxy_messages": loop_state["total_messages"],
                "proxy_cost_jpy": loop_state["total_cost_jpy"],
                "blocked_pairs": len(loop_state["blocked_pairs"]),
                "saved_cost_jpy": loop_state["saved_cost_jpy"],
                "active_alerts": len(alerts),
            },
        }
    )


@app.post("/api/scan")
def rescan() -> dict:
    """ShadowAI_Detector の再スキャンを実行する。"""
    global _scan_report, _agents
    with _state_lock:
        _agents = _load_agents()
        _scan_report = detector.scan(_agents)
        return {"status": "ok", "summary": _scan_report["summary"]}


@app.post("/api/proxy/message")
def proxy_message(msg: ProxyMessage) -> dict:
    """エージェント間通信の実プロキシエンドポイント。

    社内のエージェントは直接相手に送信せず、このエンドポイント経由で送る。
    遮断中ペアは status="rejected" が返り、APIコールは発生しない。
    """
    return proxy.handle_message(msg.sender, msg.recipient, msg.content)


@app.post("/api/proxy/unblock")
def unblock(req: PairRequest) -> dict:
    ok = proxy.unblock(req.a, req.b)
    return {"status": "ok" if ok else "not_found"}


def _run_scenario(name: str, scenario: list[tuple[str, str, str]], interval: float) -> None:
    """シナリオをバックグラウンドで1通ずつプロキシへ流す（ライブ表示用）。"""
    global _simulation
    try:
        for sender, recipient, content in scenario:
            proxy.handle_message(sender, recipient, content)
            time.sleep(interval)
    finally:
        with _state_lock:
            _simulation = {"running": False, "name": ""}


@app.post("/api/simulate/loop")
def simulate_loop() -> dict:
    """🔁 無限ループ・シミュレーション: 2体のエージェントが自動返信を打ち合う。"""
    return _start_simulation("無限ループ", LOOP_SCENARIO)


@app.post("/api/simulate/normal")
def simulate_normal() -> dict:
    """✉️ 正常トラフィック・シミュレーション: 内容の異なる健全な会話。"""
    return _start_simulation("正常トラフィック", NORMAL_SCENARIO)


def _start_simulation(name: str, scenario: list[tuple[str, str, str]]) -> dict:
    global _simulation
    with _state_lock:
        if _simulation["running"]:
            return {"status": "busy", "message": "別のシミュレーションが実行中です"}
        _simulation = {"running": True, "name": name}
    thread = threading.Thread(
        target=_run_scenario, args=(name, scenario, 0.6), daemon=True
    )
    thread.start()
    return {"status": "started", "name": name}


@app.post("/api/reset")
def reset() -> dict:
    """LoopBreaker の状態（遮断・イベント・コスト集計）を初期化する。"""
    proxy.reset()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 60)
    print("  🛡  AIGuardian — AIエージェント・コントロールダッシュボード")
    print(f"  ダッシュボード: {url}")
    print("=" * 60)
    if os.environ.get("AIGUARDIAN_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
