"""
Change Watch worker — open page, run saved actions, screenshot, callback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def parse_actions() -> list:
    raw = env("ACTIONS_JSON")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_click_point(target: str):
    m = re.match(r"^@\s*([0-9.]+)\s*,\s*([0-9.]+)\s*$", str(target or "").strip())
    if not m:
        return None
    try:
        x = min(1.0, max(0.0, float(m.group(1))))
        y = min(1.0, max(0.0, float(m.group(2))))
        return {"x": x, "y": y}
    except Exception:
        return None


def prepare_actions(actions: list) -> list:
    out = []
    has_dismiss = any(
        str(a.get("type") or "").lower() == "script"
        and re.search(r"hide_popups", str(a.get("target") or a.get("value") or ""), re.I)
        for a in actions
        if isinstance(a, dict)
    )
    if not has_dismiss:
        out.append({"type": "script", "target": "hide_popups", "value": ""})
    out.append({"type": "wait", "target": "0.8", "value": ""})
    for a in actions:
        if not isinstance(a, dict):
            continue
        out.append(a)
        if str(a.get("type") or "").lower() == "click":
            out.append({"type": "wait", "target": "2", "value": ""})
    return out


def hide_popups(page) -> None:
    page.evaluate(
        """() => {
      const sels = [
        '[id*="cookie" i]', '[class*="cookie" i]', '[id*="consent" i]',
        '[class*="consent" i]', '[aria-label*="cookie" i]', '.modal-backdrop',
        '[class*="newsletter" i]', '[id*="onetrust" i]'
      ];
      for (const sel of sels) {
        try {
          document.querySelectorAll(sel).forEach((el) => {
            const t = (el.innerText || '').toLowerCase();
            if (sel.includes('cookie') || sel.includes('consent') || /accept|agree|got it|allow/.test(t)) {
              el.style.setProperty('display', 'none', 'important');
            }
          });
        } catch (e) {}
      }
      document.querySelectorAll('button, a').forEach((el) => {
        const t = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
        if (/^(accept|agree|got it|allow all|i agree|ok)$/i.test(t) || /accept all|agree to/.test(t)) {
          try { el.click(); } catch (e) {}
        }
      });
    }"""
    )


def click_by_text(page, text: str) -> bool:
    needle = (text or "").strip()
    if not needle:
        return False
    # Prefer exact-ish button/role text, then partial.
    for sel in (
        f'button:has-text("{needle}")',
        f'a:has-text("{needle}")',
        f'[role="button"]:has-text("{needle}")',
        f'text={needle}',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(timeout=5000)
            return True
        except Exception:
            continue
    try:
        page.get_by_text(needle, exact=False).first.click(timeout=5000)
        return True
    except Exception:
        return False


def click_day(page, day_hint: str, label_hint: str = "") -> bool:
    day = re.sub(r"^0+", "", str(day_hint or "").strip()) or str(day_hint or "").strip()
    rich = re.sub(r"\s+", " ", str(label_hint or "").strip())
    if not re.match(r"^\d{1,2}$", day) and not rich:
        return False
    # Wait briefly for a calendar-like grid
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            if page.locator('[role="gridcell"], td[data-date], button').count() > 5:
                break
        except Exception:
            pass
        page.wait_for_timeout(200)
    candidates = []
    if rich:
        candidates.append(rich)
    if day:
        candidates.append(day)
    for needle in candidates:
        if click_by_text(page, needle):
            return True
    return False


def click_point(page, pt: dict) -> bool:
    try:
        dims = page.evaluate(
            """() => ({
          scrollW: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0, 1),
          scrollH: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0, 1),
          vw: window.innerWidth || 1,
          vh: window.innerHeight || 1,
        })"""
        )
        abs_x = pt["x"] * dims["scrollW"]
        abs_y = pt["y"] * dims["scrollH"]
        page.evaluate(
            """([x, y]) => {
          window.scrollTo(Math.max(0, x - window.innerWidth / 2), Math.max(0, y - window.innerHeight / 2));
        }""",
            [abs_x, abs_y],
        )
        page.wait_for_timeout(250)
        vp = page.evaluate(
            """([x, y]) => ({ x: x - window.scrollX, y: y - window.scrollY })""",
            [abs_x, abs_y],
        )
        cx = min(max(1, vp["x"]), max(1, dims["vw"] - 2))
        cy = min(max(1, vp["y"]), max(1, dims["vh"] - 2))
        page.mouse.click(cx, cy, delay=40)
        return True
    except Exception:
        return False


def run_actions(page, actions: list) -> list:
    log = []
    prepared = prepare_actions(actions)
    hide_popups(page)
    for raw in prepared:
        typ = str(raw.get("type") or "").lower()
        target = str(raw.get("target") or raw.get("selector") or raw.get("name") or "").strip()
        value = str(raw.get("value") or "")
        kind = str(raw.get("kind") or "").lower()
        try:
            if typ == "cookie":
                log.append({"type": typ, "ok": True, "skipped": True})
                continue
            if typ == "wait":
                n = float(target or value or 1)
                ms = int(min(30000, max(200, n * (1000 if n <= 120 else 1))))
                page.wait_for_timeout(ms)
                log.append({"type": typ, "ok": True, "ms": ms})
            elif typ == "script" and re.search(r"hide_popups", target or value, re.I):
                hide_popups(page)
                log.append({"type": typ, "ok": True})
            elif typ == "block" and target:
                page.evaluate(
                    """(sel) => {
                  try {
                    document.querySelectorAll(sel).forEach((el) => {
                      el.style.setProperty('display', 'none', 'important');
                      el.setAttribute('aria-hidden', 'true');
                    });
                  } catch (e) {}
                }""",
                    target,
                )
                log.append({"type": typ, "ok": True})
            elif typ == "click" and target:
                day_from = None
                m = re.search(r"\b(\d{1,2})\b", target) or re.search(r"\b(\d{1,2})\b", value)
                if m:
                    day_from = m.group(1)
                day_hint = (
                    target
                    if re.match(r"^\d{1,2}$", target)
                    else value
                    if re.match(r"^\d{1,2}$", value)
                    else day_from
                    if kind == "day"
                    else day_from
                    if re.search(r"20\d{2}", target)
                    else ""
                )
                rich = target if (not re.match(r"^\d{1,2}$", target) and not parse_click_point(target)) else ""
                pt = parse_click_point(target) or parse_click_point(value)
                ok = False
                if kind == "day" or (day_hint and re.match(r"^\d{1,2}$", str(day_hint).lstrip("0") or day_hint)):
                    ok = click_day(page, day_hint, rich)
                if not ok and not parse_click_point(target):
                    ok = click_by_text(page, target)
                if not ok and pt:
                    ok = click_point(page, pt)
                log.append({"type": typ, "target": target, "ok": ok})
            else:
                log.append({"type": typ, "ok": False, "skipped": True})
        except Exception as e:
            log.append({"type": typ, "ok": False, "error": str(e)})
    return log


def browser_ws_endpoint() -> str:
    """Bright Data Browser API websocket. Never log the password."""
    explicit = env("BRIGHTDATA_BROWSER_WS") or env("SBR_WS_CDP")
    if explicit:
        return explicit
    password = env("BRIGHTDATA_BROWSER_PASSWORD")
    if not password:
        return ""
    user = env(
        "BRIGHTDATA_BROWSER_USER",
        "brd-customer-hl_64096011-zone-scraping_browser2",
    )
    # Optional geo: BRIGHTDATA_BROWSER_COUNTRY=us → append -country-us (helps some hard sites).
    country = env("BRIGHTDATA_BROWSER_COUNTRY", "us").lower()
    if country and re.match(r"^[a-z]{2}$", country) and f"-country-{country}" not in user:
        user = f"{user}-country-{country}"
    host = env("BRIGHTDATA_BROWSER_HOST", "brd.superproxy.io:9222")
    from urllib.parse import quote

    return f"wss://{quote(user, safe='-_.')}:{quote(password, safe='')}@{host}"


def open_page(playwright):
    ws = browser_ws_endpoint()
    if ws:
        print("Using remote Browser API")
        browser = playwright.chromium.connect_over_cdp(ws)
        # Always a fresh page (Bright Data recommendation).
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            viewport={"width": 1366, "height": 768}
        )
        page = context.new_page()
        try:
            page.set_default_navigation_timeout(180000)
            page.set_viewport_size({"width": 1366, "height": 768})
        except Exception:
            pass
        return browser, page
    print("Using local Chromium")
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    page.set_default_navigation_timeout(120000)
    return browser, page


def looks_blocked(html: str, title: str) -> bool:
    blob = f"{title or ''}\n{html or ''}".lower()
    needles = [
        "just a moment",
        "cf-browser-verification",
        "attention required",
        "verification required",
        "slide right to secure",
        "unusual activity",
        "automated (bot) activity",
        "acceso está restringido",
        "acceso esta restringido",
        "restringido temporalmente",
        "access is temporarily restricted",
        "temporarily restricted",
        "please verify you are a human",
        "security check",
        "ray id",
    ]
    if any(n in blob for n in needles):
        return True
    if "an error occurred" in blob and "right back" in blob:
        return True
    if "access denied" in blob and ("viator" in blob or "tripadvisor" in blob or "getyourguide" in blob):
        return True
    return False


def wait_for_captcha_solve(page, detect_timeout_ms: int = 120000) -> str:
    """Ask Bright Data Browser API to finish Cloudflare/CAPTCHA if present."""
    try:
        client = page.context.new_cdp_session(page)
        try:
            client.send("Captcha.setAutoSolve", {"autoSolve": True})
        except Exception:
            pass
        # Prefer explicit solve, then waitForSolve (API varies by zone).
        status = ""
        try:
            result = client.send("Captcha.solve", {"detectTimeout": int(detect_timeout_ms)})
            if isinstance(result, dict):
                status = str(result.get("status") or "")
        except Exception:
            result = client.send(
                "Captcha.waitForSolve",
                {"detectTimeout": int(detect_timeout_ms)},
            )
            if isinstance(result, dict):
                status = str(result.get("status") or "")
        print(f"Captcha solve status: {status or 'unknown'}")
        return status or "unknown"
    except Exception as e:
        print(f"Captcha solve skipped: {e}")
        return "skipped"


def wait_until_unblocked(page, timeout_ms: int = 120000) -> bool:
    """Poll until bot interstitial is gone (or timeout)."""
    deadline = time.time() + max(5, timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            title = page.title() or ""
            html = page.content() or ""
            if not looks_blocked(html, title):
                # Require some real content length so empty shells don't pass.
                if len(html) > 4000 or "viator" not in (page.url or "").lower():
                    return True
                if len(html) > 1500 and "tour" in html.lower():
                    return True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return False


def navigate_ready(page, url: str) -> bool:
    """Goto + captcha solve + unblock poll. Returns False if still blocked."""
    page.goto(url, wait_until="domcontentloaded", timeout=180000)
    page.wait_for_timeout(2000)
    wait_for_captcha_solve(page, detect_timeout_ms=120000)
    if wait_until_unblocked(page, timeout_ms=90000):
        return True
    try:
        page.reload(wait_until="domcontentloaded", timeout=180000)
    except Exception:
        page.goto(url, wait_until="domcontentloaded", timeout=180000)
    page.wait_for_timeout(1500)
    wait_for_captcha_solve(page, detect_timeout_ms=90000)
    return wait_until_unblocked(page, timeout_ms=60000)


def main() -> int:
    url = env("CHECK_URL")
    if not url:
        print("CHECK_URL is required", file=sys.stderr)
        return 2

    monitor_id = env("MONITOR_ID")
    job_id = env("JOB_ID")
    callback_url = env("CALLBACK_URL")
    worker_id = env("WORKER_ID", "01")
    actions = parse_actions()
    started = datetime.now(timezone.utc).isoformat()

    screenshot_path = "screenshot.png"
    result_path = "result.json"
    error = None
    title = ""
    final_url = url
    content_hash = ""
    html = ""
    screenshot_b64 = None
    actions_log = []

    try:
        with sync_playwright() as p:
            browser, page = open_page(p)
            ready = navigate_ready(page, url)
            if not ready:
                title = page.title() or ""
                final_url = page.url
                html = page.content() or ""
                error = "blocked_or_error_page"
                print(f"Blocked/error page detected for {url}", file=sys.stderr)
                try:
                    page.screenshot(path=screenshot_path, full_page=True)
                except Exception:
                    pass
            else:
                page.wait_for_timeout(1200)
                if actions:
                    actions_log = run_actions(page, actions)
                    page.wait_for_timeout(1000)
                title = page.title() or ""
                final_url = page.url
                html = page.content() or ""
                content_hash = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
                page.screenshot(path=screenshot_path, full_page=True)
                if looks_blocked(html, title):
                    error = "blocked_or_error_page"
                    print(f"Blocked/error page detected for {url}", file=sys.stderr)
            browser.close()
        if os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as sf:
                # Keep screenshot even on block so operators can see the challenge page.
                screenshot_b64 = base64.b64encode(sf.read()).decode("ascii")
    except Exception as e:
        error = str(e)
        print(f"Capture failed: {error}", file=sys.stderr)

    body_out = html[:400000] if html else ""

    result = {
        "ok": error is None,
        "worker_id": worker_id,
        "monitor_id": monitor_id or None,
        "job_id": job_id or None,
        "url": url,
        "final_url": final_url,
        "title": title,
        "body": body_out,
        "content_hash": content_hash or None,
        "actions_count": len(actions),
        "actions_log": actions_log,
        "screenshot": screenshot_path if os.path.exists(screenshot_path) else None,
        "screenshot_base64": screenshot_b64,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "github_run_id": env("GITHUB_RUN_ID") or None,
        "github_repository": env("GITHUB_REPOSITORY") or None,
    }

    with open(result_path, "w", encoding="utf-8") as f:
        slim = dict(result)
        if slim.get("screenshot_base64"):
            slim["screenshot_base64"] = f"<omitted {len(screenshot_b64 or '')} chars>"
        json.dump(slim, f, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "screenshot_base64"},
            ensure_ascii=False,
            indent=2,
        )
    )

    if callback_url:
        try:
            r = requests.post(callback_url, json=result, timeout=90)
            print(f"Callback status: {r.status_code}")
            print((r.text or "")[:500])
        except Exception as e:
            print(f"Callback failed: {e}", file=sys.stderr)

    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
