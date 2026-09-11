#!/usr/bin/env python3
"""Desktop-Chrome simulation of the share flow (spec milestone 5).

Drives a real Chromium via Playwright against a running instance:
  login-redirect path, share → downloads, error path, zip setting, SW + manifest, installability.
Usage: BASE=http://127.0.0.1:8000 ACCESS_KEY=... [CHROME=/path/to/chrome] scripts/e2e_share.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ["ACCESS_KEY"]
CHROME = os.environ.get("CHROME")
GIF_TWEET = os.environ.get("TW_GIF", "https://x.com/GoldLeopardess/status/2098024802980110352")
MULTI = os.environ.get("TW_MULTI", "https://x.com/liberdalau/status/1623739803874349067")
TT_PHOTO = os.environ.get("TT_PHOTO", "https://www.tiktok.com/t/ZP83kyp72/")

fails = 0


def check(cond, msg):
    global fails
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails += 1


def share_and_collect(page, text, expect_n, timeout=60000):
    downloads = []
    page.on("download", lambda d: downloads.append(d))
    page.goto(f"{BASE}/share?text={quote(text)}")
    page.wait_for_selector("#status.ok, #status.err", timeout=timeout)
    deadline = time.time() + 10
    while len(downloads) < expect_n and time.time() < deadline:
        page.wait_for_timeout(200)
    return page.locator("#status").inner_text(), page.locator("#detail").inner_text(), downloads


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
    ctx = browser.new_context(accept_downloads=True, viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True)
    page = ctx.new_page()

    print("1. pre-login share redirects through /login and completes")
    share_url = f"/share?text={quote('look ' + GIF_TWEET)}"
    page.goto(BASE + share_url)
    check(page.url.startswith(BASE + "/login?next="), f"landed on {page.url[:60]}…")
    downloads = []
    page.on("download", lambda d: downloads.append(d))
    page.fill("#key", KEY)
    page.click("button[type=submit]")
    page.wait_for_url(BASE + share_url, timeout=15000)
    check(page.url == BASE + share_url, "returned to the exact share URL after login")
    page.wait_for_selector("#status.ok", timeout=90000)
    for _ in range(50):
        if len(downloads) >= 2:
            break
        page.wait_for_timeout(200)
    names = [d.suggested_filename for d in downloads]
    check(page.locator("#status").inner_text().startswith("Saved"), "status shows Saved ✓")
    check("@GoldLeopardess" in page.locator("#detail").inner_text() and "gif + mp4" in page.locator("#detail").inner_text(), f"detail: {page.locator('#detail').inner_text()}")
    check(names == ["twitter_GoldLeopardess_2098024802980110352_1.gif", "twitter_GoldLeopardess_2098024802980110352_1.mp4"], f"downloads: {names}")
    if downloads:
        path = downloads[0].path()
        check(open(path, "rb").read(6) in (b"GIF89a", b"GIF87a"), "first download is a real gif")
    page.wait_for_timeout(3200)
    check("go back" in page.locator("#hint").inner_text(), "after close attempt: 'you can go back' hint shown")

    print("2. multi-item tweet: 3 staggered downloads + multi-download hint")
    page = ctx.new_page()
    status, detail, dls = share_and_collect(page, MULTI, 3)
    check(status.startswith("Saved") and "video + 2 images" in detail, f"{status} · {detail}")
    check([d.suggested_filename[-6:] for d in dls] == ["_1.mp4", "_2.jpg", "_3.jpg"], f"downloads: {[d.suggested_filename for d in dls]}")
    check("multiple downloads" in page.locator("#hint").inner_text(), "multi-download hint visible")
    check(page.locator("#items li").count() == 3 and page.locator("#items li .ok").count() == 3, "3 item rows all ticked")

    print("3. TikTok photo-mode via short link with share-sheet text")
    page = ctx.new_page()
    status, detail, dls = share_and_collect(page, "Check out this post! " + TT_PHOTO, 2)
    check(status.startswith("Saved") and "2 images" in detail, f"{status} · {detail}")
    check(all(d.suggested_filename.endswith(".jpeg") for d in dls) and len(dls) == 2, f"downloads: {[d.suggested_filename for d in dls]}")

    print("4. error paths")
    page = ctx.new_page()
    status, _, _ = share_and_collect(page, "https://youtube.com/watch?v=abc", 0)
    check("Twitter/X or TikTok" in status, f"unsupported: {status}")
    check(page.locator("#retry").is_visible(), "Retry button visible")
    status, _, _ = share_and_collect(page, "no link here", 0)
    check("No link found" in status or "Twitter/X or TikTok" in status, f"no url: {status}")
    status, _, _ = share_and_collect(page, "https://x.com/jack/status/20", 0)
    check("no media" in status.lower(), f"text-only tweet: {status}")
    status, _, _ = share_and_collect(page, "https://x.com/jack/status/1", 0)
    check("not found" in status.lower(), f"missing tweet: {status}")

    print("5. zip_multi setting → single .zip download")
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.evaluate("localStorage.setItem('ms.settings', JSON.stringify({zip_multi: true}))")
    status, detail, dls = share_and_collect(page, MULTI, 1)
    check(status.startswith("Saved") and len(dls) == 1 and dls[0].suggested_filename == "twitter_Johnnybull3ts_1623739803874349067.zip", f"downloads: {[d.suggested_filename for d in dls]}")
    page.evaluate("localStorage.removeItem('ms.settings')")

    print("6. settings page, service worker, manifest")
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("document.getElementById('st-health').textContent.trim() !== '…'", timeout=10000)
    check(page.locator("#st-health").inner_text() == "✓", "health tick on settings page")
    reg = page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
    check(reg, "service worker active")
    keys = page.evaluate("caches.keys()")
    check(any(k.startswith("ms-") for k in keys), f"shell cache present: {keys}")
    cached = page.evaluate("caches.keys().then(ks => caches.open(ks.find(k => k.startsWith('ms-'))).then(c => c.keys()).then(rs => rs.map(r => new URL(r.url).pathname)))")
    check("/static/app.css" in cached and "/manifest.webmanifest" in cached, f"shell cached: {sorted(cached)}")
    check(not any(u.startswith("/v1") or u.startswith("/share") for u in cached), "no /v1 or /share responses cached")
    man = page.evaluate(f"fetch('{BASE}/manifest.webmanifest').then(r => r.json())")
    check(man["share_target"]["action"] == "/share" and man["share_target"]["method"] == "GET" and man["display"] == "standalone", "manifest share_target GET /share, standalone")
    check(len(man["icons"]) == 3 and any(i.get("purpose") == "maskable" for i in man["icons"]), "3 icons incl. maskable")
    for i in man["icons"]:
        r = page.request.get(BASE + i["src"])
        check(r.ok and r.headers.get("content-type", "").startswith("image/png"), f"icon {i['src']} served as png")

    browser.close()

    print("7. installability (Chrome's own check via CDP; needs a non-incognito profile)")
    import tempfile

    with tempfile.TemporaryDirectory() as prof:
        pctx = p.chromium.launch_persistent_context(prof, executable_path=CHROME, viewport={"width": 412, "height": 915}) if CHROME else p.chromium.launch_persistent_context(prof)
        page = pctx.new_page()
        page.goto(BASE + "/login")
        page.fill("#key", KEY)
        page.click("button[type=submit]")
        page.wait_for_url(BASE + "/", timeout=15000)
        page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
        cdp = pctx.new_cdp_session(page)
        res = cdp.send("Page.getInstallabilityErrors")
        errs = [e["errorId"] for e in res.get("installabilityErrors", [])]
        check(not errs, f"installability errors: {errs or 'none'}")
        app_man = cdp.send("Page.getAppManifest")
        crit = [e for e in app_man.get("errors", []) if e.get("critical")]
        check(app_man.get("url", "").endswith("/manifest.webmanifest") and not crit, f"Page.getAppManifest: url={app_man.get('url')} errors={app_man.get('errors') or 'none'}")
        pctx.close()

print("E2E FAILED" if fails else "E2E OK")
sys.exit(1 if fails else 0)
