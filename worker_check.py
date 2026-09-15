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
    # Fresh IP per attempt (DataDome often hard-blocks a sticky peer).
    session = env("BRIGHTDATA_BROWSER_SESSION") or f"cw{int(time.time())}{os.getpid()}"
    session = re.sub(r"[^a-zA-Z0-9]", "", session)[:24] or "cw"
    if "-session-" not in user:
        user = f"{user}-session-{session}"
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
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        try:
            page.set_default_navigation_timeout(180000)
            page.set_viewport_size({"width": 1920, "height": 1080})
        except Exception:
            pass
        # Enable auto-solve BEFORE navigation (Bright Data default, but be explicit).
        try:
            client = page.context.new_cdp_session(page)
            client.send("Captcha.setAutoSolve", {"autoSolve": True})
        except Exception as e:
            print(f"Captcha.setAutoSolve before nav skipped: {e}")
        return browser, page
    print("Using local Chromium")
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.set_default_navigation_timeout(120000)
    return browser, page


def looks_blocked(html: str, title: str) -> bool:
    """True only for real interstitials — not normal pages that load DataDome JS."""
    title_l = (title or "").lower().strip()
    body = (html or "").lower()
    if re.match(
        r"^(just a moment|attention required|access denied|verification required|security check)\b",
        title_l,
    ):
        return True
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
        "interstitial/?initialcid",
        "geo.captcha-delivery.com/captcha/",
        "captcha-delivery.com/captcha/",
        "#cmsg{",
        "px-captcha",
        "press & hold",
    ]
    if any(n in body for n in needles):
        return True
    if "an error occurred" in body and "right back" in body:
        return True
    # Short challenge shells only (full Viator HTML is huge and still mentions datadome).
    if len(body) > 0 and len(body) < 12000 and (
        "captcha-delivery" in body
        or "#cmsg" in body
        or ("datadome" in body and "captcha" in body)
    ):
        return True
    return False


def page_looks_real(html: str, url: str) -> bool:
    """True when HTML looks like a real product page, not a challenge shell."""
    h = (html or "").lower()
    if looks_blocked(h, ""):
        return False
    if len(h) < 8000:
        return False
    signals = 0
    for token in (
        "tour",
        "price",
        "review",
        "book now",
        "check availability",
        "from $",
        "from us$",
        "product",
        "itinerary",
        "viator",
        "xcaret",
        "cancun",
    ):
        if token in h:
            signals += 1
    return signals >= 2


def wait_for_captcha_solve(page, detect_timeout_ms: int = 90000) -> str:
    """Wait for Bright Data Browser API captcha solver after navigation."""
    try:
        client = page.context.new_cdp_session(page)
        try:
            client.send("Captcha.setAutoSolve", {"autoSolve": True})
        except Exception:
            pass
        timeout = int(min(120000, max(30000, detect_timeout_ms)))
        status = ""
        # Prefer waitForSolve (waits for in-flight auto-solve); fall back to solve.
        try:
            result = client.send("Captcha.waitForSolve", {"detectTimeout": timeout})
            if isinstance(result, dict):
                status = str(result.get("status") or "")
        except Exception:
            status = ""
        if not status or status in ("not_detected", "unknown", "invalid"):
            try:
                result = client.send("Captcha.solve", {"detectTimeout": timeout})
                if isinstance(result, dict):
                    status = str(result.get("status") or status)
                    if result.get("error"):
                        print(f"Captcha error detail: {result.get('error')}")
                    if result.get("type"):
                        print(f"Captcha type: {result.get('type')}")
            except Exception as e:
                print(f"Captcha.solve error: {e}")
        print(f"Captcha solve status: {status or 'unknown'}")
        return status or "unknown"
    except Exception as e:
        print(f"Captcha solve skipped: {e}")
        return "skipped"


def wait_until_unblocked(page, timeout_ms: int = 60000) -> bool:
    """Poll until bot interstitial is gone and real page content appears."""
    deadline = time.time() + max(5, timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            title = page.title() or ""
            html = page.content() or ""
            if page_looks_real(html, page.url or ""):
                return True
            if not looks_blocked(html, title) and len(html) > 20000:
                return True
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return False


def navigate_ready(page, url: str) -> bool:
    """Goto + captcha wait + unblock poll. Retry late unblock after solve_failed."""
    page.goto(url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(2500)
    status = wait_for_captcha_solve(page, detect_timeout_ms=90000)
    html = ""
    title = ""
    try:
        title = page.title() or ""
        html = page.content() or ""
    except Exception:
        pass

    if page_looks_real(html, page.url or ""):
        return True

    # solve_failed is common on DataDome — keep polling; auto-solve sometimes finishes late.
    if status == "solve_failed" or looks_blocked(html, title):
        print(
            "Captcha solve_failed / blocked — polling for late unblock before new session",
            file=sys.stderr,
        )
        if wait_until_unblocked(page, timeout_ms=75000):
            return True
        return False

    if wait_until_unblocked(page, timeout_ms=60000):
        return True
    try:
        page.reload(wait_until="domcontentloaded", timeout=90000)
    except Exception:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2000)
    status2 = wait_for_captcha_solve(page, detect_timeout_ms=60000)
    if page_looks_real(page.content() or "", page.url or ""):
        return True
    if status2 == "solve_failed":
        return wait_until_unblocked(page, timeout_ms=45000)
    return wait_until_unblocked(page, timeout_ms=45000)


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
            ready = False
            last_html = ""
            last_title = ""
            # Up to 4 fresh Browser API sessions (new IP/session each time).
            countries = ["us", "gb", "de", "nl"]
            for attempt in range(1, 5):
                country = countries[(attempt - 1) % len(countries)]
                os.environ["BRIGHTDATA_BROWSER_COUNTRY"] = country
                os.environ["BRIGHTDATA_BROWSER_SESSION"] = f"cw{attempt}{int(time.time())}"
                print(f"Capture attempt {attempt}/4 country={country}")
                browser, page = open_page(p)
                try:
                    ready = navigate_ready(page, url)
                    last_title = page.title() or ""
                    last_html = page.content() or ""
                    final_url = page.url
                    if ready and page_looks_real(last_html, final_url):
                        page.wait_for_timeout(1200)
                        if actions:
                            actions_log = run_actions(page, actions)
                            page.wait_for_timeout(1000)
                            last_title = page.title() or ""
                            last_html = page.content() or ""
                            final_url = page.url
                        title = last_title
                        html = last_html
                        content_hash = hashlib.sha256(
                            html.encode("utf-8", errors="ignore")
                        ).hexdigest()
                        page.screenshot(path=screenshot_path, full_page=True)
                        if looks_blocked(html, title) or not page_looks_real(html, final_url):
                            ready = False
                            error = "blocked_or_error_page"
                            print(f"Blocked page after capture (attempt {attempt})", file=sys.stderr)
                        else:
                            error = None
                            break
                    else:
                        ready = False
                        error = "blocked_or_error_page"
                        title = last_title
                        html = last_html
                        print(f"Blocked/error page detected (attempt {attempt})", file=sys.stderr)
                        try:
                            page.screenshot(path=screenshot_path, full_page=True)
                        except Exception:
                            pass
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if ready:
                    break
                print(f"Retrying with new session/country after attempt {attempt}")

        if os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as sf:
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
