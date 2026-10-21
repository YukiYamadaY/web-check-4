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


def collect_anchors(page) -> list[dict]:
      return page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => ({
            href: a.href,
            text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 200),
            img: (a.querySelector('img') || {}).currentSrc || (a.querySelector('img') || {}).src || ''
        }))""",
    )


def group_products(anchors: list[dict], page_url: str) -> dict[str, dict]:
    """同じ商品への複数リンク（画像・タイトル）を1件にまとめる"""
    host = urlparse(page_url).netloc
    own_path = urlparse(page_url).path.rstrip("/")
    merged: dict[str, dict] = {}
    for a in anchors:
        u = urlparse(a["href"])
        path = u.path.rstrip("/")
        if u.netloc != host or path == own_path or path in NAV_PATHS:
            continue
        it = merged.setdefault(path, {"path": path, "url": f"{u.scheme}://{u.netloc}{u.path}",
                                      "title": "", "img": ""})
        if len(a["text"]) > len(it["title"]):
            it["title"] = a["text"]
        if not it["img"] and a["img"]:
            it["img"] = a["img"]
    return merged


def pick_shape(merged: dict[str, dict]) -> tuple[str, int]:
    counts = defaultdict(int)
    for path in merged:
        s = shape_of(path)
        if "{id}" in s:
            counts[s] += 1
    if not counts:
        return "", 0
    best = max(counts, key=counts.get)
    return best, counts[best]


def extract_items(page, url: str, state: dict, debug: bool = False) -> list[dict]:
    merged = group_products(collect_anchors(page), url)
    if debug or not merged:
        shapes = defaultdict(int)
        for p in merged:
            shapes[shape_of(p)] += 1
        top = sorted(shapes.items(), key=lambda x: -x[1])[:6]
        log(f"[調査] タイトル: {page.title()[:40]} / リンク数: {len(merged)} / 形: {top}")
        log(f"[調査] 本文: {page.inner_text('body')[:150]!r}")

    if LINK_PATTERN:
        rx = re.compile(LINK_PATTERN)
        items = [it for p, it in merged.items() if rx.search(p)]
        if debug:
            log(f"手動指定のパターンで {len(items)}件")
    else:
        if not state.get(url):
            shape, n = pick_shape(merged)
            if n >= 3:
                state[url] = shape
            if debug or n >= 3:
                log(f"商品リンクの形: {shape or '（見つからず）'}（{n}件）")
        shape = state.get(url, "")
        items = [it for p, it in merged.items() if shape and shape_of(p) == shape]
    for it in items:
        it["id"] = it["path"]
        if not it["title"]:
            it["title"] = "(タイトル取得不可)"
    return items


def load_page(page, url: str, first: bool):
    if first:
        page.goto(url, wait_until="networkidle", timeout=60000)
    else:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    page.mouse.wheel(0, 2000)
    page.wait_for_timeout(6000)


def make_embed(item: dict) -> dict:
    embed = {
        "title": f"🆕 新着: {item['title'][:110]}",
        "url": item["url"],
        "color": 0xE67E22,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item["img"] and item["img"].startswith("http"):
        embed["image"] = {"url": item["img"]}
    return embed


def notify_batch(items: list[dict]):
    """10件ずつ1メッセージにまとめて送る"""
    if not WEBHOOK_URL:
        log("[WARN] WEBHOOK_URL未設定")
        return
    for i in range(0, len(items), 10):
        payload = {"embeds": [make_embed(it) for it in items[i:i + 10]]}
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
            if r.status_code >= 400:
                log(f"[ERROR] 通知失敗（ステータス {r.status_code}）")
        except Exception as e:
            log(f"[ERROR] 通知エラー（{type(e).__name__}）")
        time.sleep(1)


def new_browser(p):
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    pages = [
        browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"),
            viewport={"width": 1280, "height": 1000},
            locale="ja-JP",
        )
        for _ in TARGET_URLS
    ]
    return browser, pages


def main():
    if not TARGET_URLS:
        log("[ERROR] TARGET_URLS が未設定です")
        return
    if in_quiet_hours():
        log("停止時間帯なので終了")
        return

    seen = load_seen()
    first_run = len(seen) == 0
    start = last_persist = time.time()
    fail_streak = cycle = 0
    shape_state: dict[str, str] = {}

    with sync_playwright() as p:
        browser, pages = new_browser(p)
        first_load = [True] * len(TARGET_URLS)

        while True:
            if time.time() - start > MAX_RUN_SEC:
                log("最大実行時間に到達。次の実行に引き継ぎ")
                break
            if in_quiet_hours():
                log("停止時間帯に入ったので終了")
                break

            cycle += 1
            if cycle % BROWSER_RESTART_EVERY == 0:
                browser.close()
                browser, pages = new_browser(p)
                first_load = [True] * len(TARGET_URLS)

            all_items, ok = [], False
            for i, url in enumerate(TARGET_URLS):
                try:
                    load_page(pages[i], url, first_load[i])
                    first_load[i] = False
                    got = extract_items(pages[i], url, shape_state, debug=(cycle == 1))
                    ok = ok or bool(got)
                    all_items.extend(got)
                except Exception as e:
                    log(f"[WARN] 取得失敗（{type(e).__name__}）")
                    first_load[i] = True

            if not ok:
                fail_streak += 1
                wait = min(CHECK_INTERVAL_SEC * (2 ** fail_streak), 300)
                log(f"[WARN] 取得0件（{fail_streak}回連続）。{wait:.0f}秒待機")
                shape_state.clear()
                time.sleep(wait)
                continue
            fail_streak = 0

            new_items = [it for it in all_items if it["id"] not in seen]
            if first_run:
                log(f"初回: {len(all_items)}件を登録（通知なし）")
                first_run = False
            elif new_items:
                log(f"新着 {len(new_items)}件を検知・通知")
                notify_batch(new_items[:MAX_NOTIFY_PER_CHECK])
            elif cycle % 50 == 0:
                log(f"異常なし（{len(all_items)}件監視中・{cycle}回目）")

            seen.update(it["id"] for it in all_items)
            save_seen(seen)

            if time.time() - last_persist > PERSIST_EVERY_SEC:
                persist_to_repo()
                last_persist = time.time()

            time.sleep(max(1.0, CHECK_INTERVAL_SEC + random.uniform(-1.5, 1.5)))

        browser.close()

    save_seen(seen)
    persist_to_repo()


if __name__ == "__main__":
    main()
