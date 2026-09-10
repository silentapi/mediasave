# Media Saver — Technical Specification (PWA)

Personal tool: install a small web app to the Android home screen; share a Twitter/X or TikTok post to "Media Saver" from the share sheet and the media (video / image(s) / gif) is downloaded to the phone automatically. Everything — the PWA, the API, gif conversion, and download proxying — lives on **one site** on a Digital Ocean droplet. No app store, no APK, no native code.

This document is the source of truth for the coding agent. Where it says "decide", make a reasonable choice and note it in `DECISIONS.md`. Where it says "verify", do it before building on the assumption.

---

## 1. Product requirements

### 1.1 User flow
1. **One-time setup:** visit `https://media.<domain>`, enter the access key (sets a long-lived cookie), tap Chrome's **Install / Add to Home Screen**. After install, "Media Saver" appears in the Android share sheet.
2. User is in Twitter/X or TikTok, taps Share → **Media Saver**.
3. The PWA opens on a minimal "Saving…" page, immediately resolves the link and triggers the downloads, updates to "Saved ✓ · @author · 3 items", and attempts to close itself. If auto-close is blocked, the user swipes back — the downloads continue in Chrome's download manager regardless.
4. Chrome's own download notifications show progress/completion; files land in `Download/MediaSaver/…` and appear in the gallery (Downloads are media-scanned).
5. On failure the page shows a clear error ("Post not found", "Private post", …) with a **Retry** button.

### 1.2 Hard rules
- **No decisions on the share path.** The share page never asks anything; it saves with defaults. The only interactive page is Settings.
- Share → downloads started in **≤ ~2 s** on a normal connection (resolution time; the media itself streams in the background).
- Twitter items tagged `animated_gif` are saved as `.gif` (plus the source `.mp4` by default).
- Multi-image tweets and TikTok photo-mode posts save **every** image (see §4.5 for the multi-download strategy).
- All downloads are proxied by the server with `Content-Disposition: attachment; filename=…` so names are correct and Chrome's download manager owns the transfer.
- Single access key; the whole site (except `/login` and static shell assets) is behind it.
- Android Chrome is the target. Anything extra that happens to work elsewhere is a bonus.

### 1.3 Accepted tradeoffs (vs. a native app) — do not try to "fix" these
- A browser window flashes open for a moment; auto-close is best-effort.
- Files go to `Download/`, not `Pictures/`/`Movies/`. (Gallery apps index them anyway.)
- Chrome will ask once for the "download multiple files" permission; it's remembered per-site.

### 1.4 Non-goals (v1)
- No history/library UI (a simple `/recent` page is a v2 idea).
- No providers beyond Twitter/X and TikTok, but the provider interface must make adding one a single new module.
- No accounts, no multi-user, no iOS, no offline mode beyond the app shell.

---

## 2. Architecture

```
Android share sheet
   │  (installed PWA registered as Web Share Target)
   ▼
GET /share?…                        ┌──────────────────────────────────────────┐
   │ auth cookie                    │  ONE SITE  https://media.<domain>        │
   ▼                                │                                          │
share page JS ── POST /v1/resolve ─►│  FastAPI: resolve, auth, dl proxy, gif   │
   │                                │  yt-dlp + ffmpeg (Docker)                │
   │  for each item:                │  Caddy: TLS, serves /static              │
   └─ navigate <a> → GET /v1/dl/<token> ──► streams media from CDN/cache       │
                                    └──────────────────────────────────────────┘
Chrome download manager → Download/MediaSaver/ → gallery
```

**Principle: the server does everything; the page is a dumb trigger.** The share page only extracts the shared URL, calls `/v1/resolve`, and clicks download links. Every site-specific hack, header, cookie, and conversion lives server-side and redeploys in seconds.

Proxying (rather than handing the phone raw CDN URLs) is deliberate in the PWA version: anchor-tag downloads can't set custom headers (which TikTok's CDN needs), and proxying lets us set the filename. Bandwidth on a DO droplet is a non-issue at personal scale.

---

## 3. Server

### 3.1 Stack
- Python 3.12, FastAPI, uvicorn, `httpx` (async, streaming), `yt-dlp` (latest), system `ffmpeg`, `pydantic` v2, `itsdangerous` (signed tokens/cookies).
- One Docker container for the app; Caddy container for TLS + static. No database; filesystem cache at `/data`.

### 3.2 Repo layout
```
media-saver/
  server/
    app/
      main.py            # FastAPI app, routes, static mounting fallback
      config.py          # env settings: ACCESS_KEY, SECRET, PUBLIC_BASE_URL, COOKIES_PATH, GIF_*, ...
      models.py          # pydantic models (§3.4)
      auth.py            # login route, cookie + X-Api-Key dependency
      urlnorm.py         # canonicalize/expand URLs, detect provider
      providers/
        base.py          # Provider protocol: matches(url), resolve(url) -> ResolvedPost
        twitter.py
        tiktok.py
      ytdlp_util.py      # extract_info wrapper: threadpool, 45s timeout, cookies
      dl.py              # signed download tokens + streaming proxy + zip
      convert.py         # mp4 -> gif, cache, prune loop
      ratelimit.py       # in-memory token bucket
    static/              # the PWA (see §4)
    tests/
      test_urlnorm.py test_twitter.py test_tiktok.py test_dl_tokens.py
      fixtures/          # recorded provider JSON + tiny sample.mp4
    Dockerfile
    docker-compose.yml   # app + caddy
    Caddyfile
    requirements.txt
    .env.example
  scripts/
    deploy.sh            # rsync + docker compose up -d --build
    smoke.sh             # curl health + resolve for each post type
  README.md
  DECISIONS.md
```

### 3.3 Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/login` + POST `/login` | none | Enter access key → sets signed HttpOnly cookie (1 year) |
| GET | `/` | cookie | Settings/status page (PWA shell) |
| GET | `/share` | cookie* | Share-target landing page |
| GET | `/manifest.webmanifest`, `/sw.js`, `/static/*` | none | Installability assets |
| GET | `/v1/health` | none | `{ ok, ytdlp_version }` |
| POST | `/v1/resolve` | cookie or `X-Api-Key` | Resolve post URL → media items |
| GET | `/v1/dl/{token}` | token is self-authorizing | Stream one media file as attachment |
| GET | `/v1/zip/{token}` | token | Stream a zip of all items of a post |

*`/share` with no/invalid cookie must NOT dead-end: redirect to `/login?next=<full share URL>` so the flow completes after login. This matters — the first real share someone does may be pre-login.

Auth details: `POST /login` compares against `ACCESS_KEY` (`secrets.compare_digest`), sets `session` cookie = signed timestamp (itsdangerous, `SECRET`). Dependency accepts either the cookie (browser paths) or `X-Api-Key` header (curl/scripts). Rate limit: 30 resolves / 5 min; `429` + `Retry-After`.

### 3.4 `/v1/resolve` contract

Request: `{ "url": "<shared text or url>", "options": { "gif_keep_mp4": true } }` — the server tolerates full shared *text* here and extracts the first URL itself (single place for that logic; the page passes through whatever it got).

Response `200`:
```json
{
  "provider": "twitter",
  "post_id": "1234567890",
  "author": "username",
  "text": "first 120 chars for display",
  "summary": "gif + mp4",
  "items": [
    { "kind": "gif",   "filename": "twitter_username_1234567890_1.gif",
      "bytes": null, "width": 480, "height": 270,
      "dl": "/v1/dl/eyJ…" },
    { "kind": "video", "filename": "twitter_username_1234567890_1.mp4",
      "bytes": 1048576, "width": 1280, "height": 720,
      "dl": "/v1/dl/eyJ…" }
  ],
  "zip": "/v1/zip/eyJ…"
}
```
- `kind` ∈ `photo | video | gif`. Items ordered as in the post. `summary` is preformatted for the page ("video", "3 images", "gif + mp4").
- `dl` is a **signed token URL** (§3.8). The page never sees raw CDN URLs.
- `zip` present when `items.length > 1`.

Errors — `{ "error": code, "message": "human text" }` with codes: `unsupported_url` 400, `not_found` 404, `private` 403, `no_media` 422, `extract_failed` 502, `rate_limited` 429, `timeout` 504. The page maps codes to friendly text.

### 3.5 URL normalization (`urlnorm.py`)
1. Extract the first `http(s)://` URL from the input text (share sheets prepend post text; TikTok shares often look like `"Check out this video! https://vm.tiktok.com/xyz/"`).
2. Follow redirects for shorteners (`t.co`, `vm.tiktok.com`, `vt.tiktok.com`, `tiktok.com/t/`), max 5 hops, 10 s.
3. Canonicalize: `twitter.com|x.com|mobile.*` → provider `twitter`, status ID from `/status/<id>` (strip `/photo/1` etc.); `tiktok.com` → provider `tiktok`, ID from `/video/<id>` or `/photo/<id>`.
4. Return `(provider, post_id, canonical_url)` or raise `UnsupportedUrl`. Table-test ≥ 15 variants.

### 3.6 Twitter provider
**Primary — syndication endpoint, no auth:** `GET https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=<t>` where the token formula must be **copied from the current yt-dlp `twitter.py`** (verify §8.1). Browser-like User-Agent.
- `mediaDetails[].type`:
  - `photo` → url `media_url_https` + `?format=<ext>&name=orig`.
  - `video` → highest-bitrate `video/mp4` variant.
  - `animated_gif` → **gif**: take the mp4 variant, convert (§3.7); emit gif item + (if `gif_keep_mp4`, default true) a video item with the same base filename.
- Tombstone/missing → `not_found`; protected → yt-dlp fallback with `COOKIES_PATH` if set, else `private`.
- Fallback gif heuristic (yt-dlp doesn't tag gifs): no audio stream and duration ≤ 30 s → treat as gif; record in DECISIONS.md.
- Quote tweets: only the shared tweet's own media; if none → `no_media` (leave `include_quoted` as a commented option).

### 3.7 Gif conversion (`convert.py`)
- Cache key sha1(mp4 URL) → `/data/converted/<key>.gif`; reuse if fresh.
- Download mp4 to temp (50 MB cap), then two-pass palette:
  ```
  ffmpeg -y -i in.mp4 -vf "fps=${GIF_FPS},scale='min(iw,${GIF_MAX_WIDTH})':-1:flags=lanczos,palettegen=stats_mode=diff" palette.png
  ffmpeg -y -i in.mp4 -i palette.png -lavfi "fps=${GIF_FPS},scale='min(iw,${GIF_MAX_WIDTH})':-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" out.gif
  ```
  Defaults `GIF_FPS=15`, `GIF_MAX_WIDTH=480`. Subprocess with 60 s timeout.
- Conversion runs during `/v1/resolve` (the page shows "Converting gif…" state if resolve takes > 3 s — the resolve response can simply take longer; no polling needed in v1). Prune `/data` hourly: converted > 24 h old, temps > 1 h.

### 3.8 Download proxy (`dl.py`)
- Token = itsdangerous-signed, URL-safe blob of `{ upstream_url, headers, filename, mime, exp }`; `exp` = now + 15 min (covers TikTok's short-lived signed URLs — the share page uses tokens within seconds).
- `GET /v1/dl/{token}`: verify + check exp → open streaming `httpx` GET to `upstream_url` with `headers` → stream response through with:
  `Content-Type: <mime>`, `Content-Disposition: attachment; filename="<filename>"`, `Content-Length` when upstream provides it, `Accept-Ranges` passthrough (Chrome's download manager may issue Range requests on resume — forward `Range` upstream when present; if upstream refuses, respond 200 full-body).
- For converted gifs the "upstream" is the local file; serve with `FileResponse`.
- `GET /v1/zip/{token}`: token wraps the full item list; stream a zip (store, no compression — media is already compressed) using `zipstream`-style chunked generation. Filename `twitter_username_<id>.zip`.
- Expired token → `410` with a tiny HTML page: "Link expired — reshare the post."

### 3.9 TikTok provider
- yt-dlp `extract_info(download=False)` through the shared wrapper.
- Video: best watermark-free mp4 (verify current format naming). Attach the format's `http_headers` into the dl token.
- Photo-mode: one `photo` item per image, highest resolution (verify current result shape — playlist vs `images` list).
- Login/captcha-style failures → `private`. **Verify §8.2 first:** extractor works from the droplet IP; if not, document the cookies workflow and set `COOKIES_PATH`.
- Because the server both resolves and proxies from the same IP within seconds, the TikTok expiring-URL/IP-binding problem from the native design disappears.

### 3.10 Deployment
- `Dockerfile`: `python:3.12-slim` + ffmpeg, non-root, `/data` volume.
- Caddy: serves `/static` directly, reverse-proxies the rest to `app:8000`, automatic HTTPS on `media.<domain>`. HTTPS is mandatory — PWA install and share target only work on secure origins.
- `.env`: `ACCESS_KEY`, `SECRET`, `PUBLIC_BASE_URL`, `COOKIES_PATH?`, `GIF_FPS`, `GIF_MAX_WIDTH`, `GIF_KEEP_MP4_DEFAULT=true`.
- Weekly yt-dlp update via cron (`pip install -U yt-dlp` + restart) — documented in README.
- `scripts/smoke.sh`: health + resolve (with `X-Api-Key`) for: Twitter photo, multi-photo, video, gif; TikTok video, photo-mode; asserts item counts/kinds; downloads one `dl` URL to verify headers.

---

## 4. The PWA (`server/static/`)

Plain HTML/CSS/vanilla JS (or Preact via CDN if the agent prefers — no build step, keep it deployable as static files).

### 4.1 Files
```
static/
  index.html        # settings/status (the "app" you open from the icon)
  share.html        # share-target landing page (minimal, fast)
  login.html
  app.css  app.js  share.js
  manifest.webmanifest
  sw.js
  icons/  (192, 512, maskable)
```

### 4.2 `manifest.webmanifest`
```json
{
  "name": "Media Saver",
  "short_name": "Media Saver",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#111",
  "theme_color": "#111",
  "icons": [ { "src": "/static/icons/192.png", "sizes": "192x192", "type": "image/png" },
             { "src": "/static/icons/512.png", "sizes": "512x512", "type": "image/png" },
             { "src": "/static/icons/maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" } ],
  "share_target": {
    "action": "/share",
    "method": "GET",
    "params": { "title": "title", "text": "text", "url": "url" }
  }
}
```
- `method: GET` → shared data arrives as query params. Twitter/TikTok apps typically put the link in `text` (sometimes with surrounding words); `url` may be empty. `share.js` concatenates `url + " " + text` and sends the whole thing to `/v1/resolve` (the server extracts the URL).
- The share-sheet entry appears **only after the PWA is installed**. README must say this loudly.

### 4.3 `sw.js`
Minimal but real (Chrome requires a fetch handler for installability):
- `install`: pre-cache the shell (`/`, css/js, icons, manifest).
- `fetch`: cache-first for `/static/*` and the shell; **network-only for `/share`, `/login`, `/v1/*`** — never cache API or download responses.
- Version string constant; bump to bust cache on deploy (deploy script does a find-replace or uses a build timestamp).

### 4.4 `share.html` + `share.js` — the whole point
Target: **first paint < 300 ms** (inline the CSS, no framework, no blocking requests), downloads triggered within ~1–2 s.

```
state machine: RESOLVING → DOWNLOADING → DONE | ERROR

on load:
  input = (params.url ?? "") + " " + (params.text ?? "")
  show "Saving…" + spinner
  resp = POST /v1/resolve { url: input }            // cookie auth; 401 → location = /login?next=<current full URL>
  if error → ERROR state: friendly message by code + [Retry] button (re-runs resolve)
  else:
    for each item, staggered ~400 ms apart:
        a = <a href=item.dl download=item.filename>; a.click()
        mark item row ✓ as triggered
    show "Saved ✓ · @author · {summary}" + per-item list
    after 2.5 s: attempt window.close()
    if still open: show "Downloads running — you can go back now"
```
- The stagger avoids Chrome coalescing/blocking rapid programmatic clicks; the *first* multi-file share will surface Chrome's "Download multiple files?" permission — the page shows a one-line hint when `items.length > 1`: "If Chrome asks, allow multiple downloads (remembered)."
- A settings toggle "Zip multi-item posts" switches the loop to a single navigation to `resp.zip` (no permission prompt, but files need manual unzip — default **off**).
- Retry is a plain re-run; tokens are re-minted by the new resolve, so expiry is never a user problem.
- `window.close()` is best-effort by design (§1.3). Don't fight it beyond one attempt.

### 4.5 `index.html` (settings/status)
- Shows: login state, server health (`/v1/health` ping), notification of whether the app is installed (`beforeinstallprompt` captured → show an Install button; on `display-mode: standalone` show "Installed ✓").
- Settings stored in `localStorage`, sent as `options` on resolve: `gif_keep_mp4`, `zip_multi`.
- A "Test with a link" input that runs the exact share flow inline.
- Setup checklist rendered right on the page: 1) log in 2) install 3) share a post from Twitter/TikTok 4) allow multiple downloads if asked.

### 4.6 Edge cases
- Share with no URL in text → ERROR "No link found in what was shared."
- Non-Twitter/TikTok link → `unsupported_url` message.
- Cookie expired mid-share → `/login?next=…` round-trip preserves the share and completes it after login.
- Two shares in quick succession → each opens its own `/share` navigation; they're independent; server rate limit is the only guard needed.
- Chrome "Ask where to save each file" user setting will prompt per file — README notes to keep it off (default).
- Duplicate filenames → Chrome auto-suffixes `(1)`; acceptable. (Server keeps names deterministic so re-saves are visibly duplicates.)

---

## 5. Security notes
- The site downloads and re-serves arbitrary media; the access cookie is the only gate. `SameSite=Lax`, `HttpOnly`, `Secure`. All state-changing/costly routes behind auth + rate limit.
- dl tokens are signed and short-lived, so leaked page HTML can't be replayed later.
- Never log the access key, cookies file, or full tokens. Cookies file (if used) mounted read-only, gitignored; README warns it's a live session for Joe's accounts.
- Both platforms' ToS prohibit automated downloading; personal use, keep the site private (the access key IS the privacy).

---

## 6. Testing
- Server unit tests as laid out in §3.2 (fixtures, no live network in CI).
- `scripts/smoke.sh` against real posts (needs network) — run at milestones 2–4 and after deploy.
- PWA manual checklist in README: install flow; share from Twitter app / TikTok app / Chrome; multi-image tweet (permission prompt path); gif tweet (plays animated in gallery); offline share (clean error); expired cookie share (login round-trip works); uninstall/reinstall.

## 7. Milestones

| # | Milestone | Done when |
|---|---|---|
| 1 | Server skeleton + auth | `docker compose up` locally; health OK; `/login` sets cookie; resolve 401s without auth, 400s garbage. |
| 2 | Twitter provider | Smoke passes photo / multi-photo / video via syndication; unit tests green. |
| 3 | Gif pipeline | Gif tweet → resolve returns gif+mp4 items; `dl` serves both; gif animates; prune loop works. |
| 4 | TikTok provider | Video + photo-mode smoke passes **from the droplet**; dl proxy streams with the right headers. |
| 5 | PWA shell | Manifest + SW + icons; installable (Lighthouse PWA pass); `/share` flow works end-to-end from a desktop Chrome share simulation and from an Android phone. |
| 6 | Deploy | Live on `https://media.<domain>` behind Caddy; smoke green against prod; install + real share from Twitter and TikTok apps both save files. |
| 7 | Polish | Zip option, settings page complete, login-redirect share path verified, README + DECISIONS.md complete, weekly yt-dlp cron documented. |

Order is strict; don't start 5 before 2 is green (the page is trivial once resolve returns real data).

## 8. Verify at the very start (before provider code)
1. Current yt-dlp Twitter extractor: syndication `token` formula; endpoint still returns `mediaDetails[].type` incl. `animated_gif`.
2. yt-dlp TikTok extractor from a datacenter IP without cookies; current photo-mode shape; `http_headers` on formats.
3. Web Share Target on current Android Chrome: confirm GET params arrive as expected when sharing from the Twitter and TikTok apps (which field carries the link), and behavior of `window.close()` on a share-opened window. A 20-line test page deployed early answers all of this — build it in milestone 1 as `/share-debug` (dumps params; auth-gated).
4. Programmatic multi-`<a>.click()` downloads on Android Chrome: stagger needed? permission prompt behavior? (Same `/share-debug` page can test with two tiny files.)

Record every outcome in `DECISIONS.md`.
