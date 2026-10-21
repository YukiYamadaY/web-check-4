"""
汎用 新着チェッカー（ブラウザ版・商品リンク自動判定）

監視URL・通知先は GitHub Secrets から読み込む。
ログにはドメイン・商品名を出さない。

必要な Secrets:
  TARGET_URLS   … 監視する一覧ページのURL（複数ならカンマ区切り）
  WEBHOOK_URL   … 通知先のWebhook URL
  LINK_PATTERN  … （任意）商品リンクを手動で指定する正規表現。空なら自動判定
"""

import json
import os
import random
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# ================= 設定 =================
CHECK_INTERVAL_SEC = 5         # 待ち時間（秒）。読み込み込みで実際は約8〜10秒おき
MAX_RUN_SEC = 5.5 * 3600       # 1回の実行の最大時間
QUIET_START_HOUR = 24          # 24 = 停止しない（1日中稼働）
QUIET_END_HOUR = 0             # 0 = 停止しない（1日中稼働）
PERSIST_EVERY_SEC = 20 * 60    # 記録ファイルを保存する間隔
BROWSER_RESTART_EVERY = 300    # この回数ごとにブラウザ再起動
MAX_NOTIFY_PER_CHECK = 30

TARGET_URLS = [u.strip() for u in os.environ.get("TARGET_URLS", "").split(",") if u.strip()]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
LINK_PATTERN = os.environ.get("LINK_PATTERN", "").strip()
SEEN_FILE = Path(__file__).parent / "seen.json"
JST = timezone(timedelta(hours=9))
NAV_PATHS = {"", "/", "/cart", "/account", "/login", "/search", "/mypage", "/favorite", "/contact"}
# ========================================


def log(msg: str):
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def in_quiet_hours() -> bool:
    h = datetime.now(JST).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            log("[WARN] 記録ファイルの読み込み失敗")
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def persist_to_repo():
    try:
        subprocess.run(["git", "add", "seen.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "update [skip ci]"],
                       check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"],
                       check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
        log("記録を保存")
    except Exception:
        log("[WARN] 記録の保存に失敗")


def shape_of(path: str) -> str:
    """数字を含む部分を {id} に置き換えて、リンクの「形」を作る"""
    return "/".join("{id}" if re.search(r"\d", seg) else seg for seg in path.split("/"))


def collect_anchors(page) -> list[dict
