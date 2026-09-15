"""
Change Watch worker — open page, screenshot, write result.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def main() -> int:
    url = env("CHECK_URL")
    if not url:
        print("CHECK_URL is required", file=sys.stderr)
        return 2

    monitor_id = env("MONITOR_ID")
    job_id = env("JOB_ID")
    callback_url = env("CALLBACK_URL")
    worker_id = env("WORKER_ID", "01")
    started = datetime.now(timezone.utc).isoformat()

    screenshot_path = "screenshot.png"
    result_path = "result.json"
    error = None
    title = ""
    final_url = url
    content_hash = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)
            title = page.title() or ""
            final_url = page.url
            html = page.content() or ""
            content_hash = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
            page.screenshot(path=screenshot_path, full_page=True)
            browser.close()
    except Exception as e:
        error = str(e)
        print(f"Capture failed: {error}", file=sys.stderr)

    result = {
        "ok": error is None,
        "worker_id": worker_id,
        "monitor_id": monitor_id or None,
        "job_id": job_id or None,
        "url": url,
        "final_url": final_url,
        "title": title,
        "content_hash": content_hash or None,
        "screenshot": screenshot_path if error is None and os.path.exists(screenshot_path) else None,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "github_run_id": env("GITHUB_RUN_ID") or None,
        "github_repository": env("GITHUB_REPOSITORY") or None,
    }

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if callback_url:
        try:
            r = requests.post(callback_url, json=result, timeout=30)
            print(f"Callback status: {r.status_code}")
        except Exception as e:
            print(f"Callback failed: {e}", file=sys.stderr)

    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
