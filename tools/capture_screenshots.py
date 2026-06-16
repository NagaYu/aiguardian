"""README用スクリーンショット生成スクリプト（開発補助・配布対象外）

ローカルで AIGuardian を起動し、無限ループ・シミュレーションを実行した状態の
ダッシュボードを docs/ 以下に2枚撮影する。

使い方:
    pip install playwright && python -m playwright install chromium
    python tools/capture_screenshots.py
"""

import subprocess
import sys
import time
from pathlib import Path

import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def _post(path: str) -> None:
    req = urllib.request.Request(BASE + path, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def main() -> None:
    env = {"AIGUARDIAN_PORT": str(PORT), "AIGUARDIAN_NO_BROWSER": "1"}
    import os

    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=ROOT,
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # サーバ起動待ち
        for _ in range(30):
            try:
                urllib.request.urlopen(BASE + "/api/state", timeout=1).read()
                break
            except Exception:
                time.sleep(0.3)

        _post("/api/simulate/loop")
        time.sleep(7)  # シナリオ完走（遮断発生）を待つ

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=2)
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(2500)  # ポーリング1巡を待つ

            # 1枚目: ページ最上部（KPI + アラート）
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
            page.screenshot(path=str(DOCS / "dashboard-overview.png"))

            # 2枚目: LoopBreaker セクション（フィード + 遮断カード）
            page.evaluate("document.querySelector('#blocked-pairs').scrollIntoView({block:'center'})")
            page.wait_for_timeout(400)
            page.screenshot(path=str(DOCS / "dashboard-loopbreaker.png"))

            browser.close()
        print("saved:", DOCS / "dashboard-overview.png", "and", DOCS / "dashboard-loopbreaker.png")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
