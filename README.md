# Media Saver

Personal PWA + Python server: install it to an Android home screen, share a Twitter/X or TikTok post to **Media Saver** from the share sheet, and the post's media (video / images / gif) downloads through Chrome's download manager with correct filenames. One site, one FastAPI app behind Caddy, no native code, no database.

Spec: [`media-saver-SPEC-pwa.md`](media-saver-SPEC-pwa.md) (source of truth). Decisions and verification results: [`DECISIONS.md`](DECISIONS.md).

## Status

| # | Milestone | State |
|---|---|---|
| 1 | Server skeleton + auth + `/share-debug` | ✅ |
| 2 | Twitter provider | ✅ |
| 3 | Gif pipeline | ✅ |
| 4 | TikTok provider | ✅ (re-check from the droplet at deploy) |
| 5 | PWA shell (installable, end-to-end share) | ✅ desktop Chrome (Android part with milestone 6) |
| 6 | Deploy | ⏳ |
| 7 | Polish | ⏳ |

## Run locally

```bash
cd server
cp .env.example .env          # set ACCESS_KEY and SECRET (python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
docker compose up --build
```

- App directly: http://127.0.0.1:8000 → `/login`, `/v1/health`, `/share-debug`
- Through Caddy: https://localhost (self-signed; `SITE_ADDRESS=localhost` in `.env`). Trust the local cert or use the direct port for testing.

Without Docker:

```bash
cd server
pip install -r requirements-dev.txt
export ACCESS_KEY=dev SECRET=dev-secret PUBLIC_BASE_URL=http://127.0.0.1:8000 DATA_DIR=./data
uvicorn app.main:app --reload
pytest
```

## API quick reference

| Method | Path | Auth |
|---|---|---|
| GET/POST | `/login` | none — sets the `session` cookie (1 year) |
| GET | `/`, `/share`, `/share-debug` | cookie (redirects to `/login?next=…` otherwise) |
| GET | `/v1/health` | none |
| POST | `/v1/resolve` `{ "url": "<shared text>", "options": { "gif_keep_mp4": true } }` | cookie or `X-Api-Key` |
| GET | `/v1/dl/{token}`, `/v1/zip/{token}` | signed token (15 min) |

Errors: `{ "error": code, "message": text }` with `unsupported_url` 400, `not_found` 404, `private` 403, `no_media` 422, `extract_failed` 502, `rate_limited` 429, `timeout` 504.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/resolve -H "X-Api-Key: $ACCESS_KEY" \
     -H 'Content-Type: application/json' -d '{"url":"https://x.com/jack/status/20"}'
```

## Smoke test

`scripts/smoke.sh` needs a running instance and `jq`. It checks health, auth (401/400), then resolves one post of each type when you supply sample URLs, and verifies the first download's `Content-Disposition`:

```bash
BASE=http://127.0.0.1:8000 ACCESS_KEY=… \
TW_PHOTO=https://x.com/… TW_MULTI=… TW_VIDEO=… TW_GIF=… TT_VIDEO=… TT_PHOTO=… scripts/smoke.sh
```

## Browser end-to-end test

`scripts/e2e_share.py` drives a real Chromium (Playwright) through the whole share flow against a running instance: login redirect, downloads with filenames, error copy, zip setting, service worker cache scope, manifest, and Chrome's installability check.

```bash
pip install playwright && playwright install chromium     # or CHROME=/path/to/chrome
BASE=http://127.0.0.1:8000 ACCESS_KEY=… scripts/e2e_share.py
```

## Deploy (Digital Ocean droplet)

1. Point `media.<domain>` at the droplet (A/AAAA). Ports 80/443 open.
2. On the droplet: install Docker + compose plugin, `mkdir -p /opt/media-saver/server`, copy `server/.env.example` → `/opt/media-saver/server/.env` and fill in `ACCESS_KEY`, `SECRET`, `PUBLIC_BASE_URL=https://media.<domain>`, `SITE_ADDRESS=media.<domain>`.
3. From your machine: `scripts/deploy.sh root@droplet` — rsyncs the repo, stamps a fresh service-worker cache version, runs `docker compose up -d --build`.
4. `BASE=https://media.<domain> ACCESS_KEY=… scripts/smoke.sh`.

HTTPS is mandatory: PWA install and the share target only work on a secure origin. Caddy provisions the certificate automatically.

### Weekly yt-dlp update

yt-dlp extractors break often; rebuild weekly. On the droplet, `crontab -e`:

```
17 4 * * 1  cd /opt/media-saver/server && docker compose build --pull --no-cache app >/dev/null 2>&1 && docker compose up -d app
```

(`requirements.txt` pins yt-dlp unversioned, so a `--no-cache` build always pulls the latest release.)

## Android setup (the part people miss)

1. Open `https://media.<domain>` in Chrome, enter the access key.
2. Chrome menu ⋮ → **Add to Home screen** / **Install app**. **The "Media Saver" entry appears in the share sheet only after the PWA is installed.**
3. In Twitter/X or TikTok: Share → Media Saver. The page shows "Saving…", the downloads start, then "Saved ✓".
4. On the first multi-file share Chrome asks "Download multiple files?" — allow it (remembered per site).
5. Keep Chrome's "Ask where to save files" setting **off** (default), or it will prompt per file.

Files land in `Download/` and are picked up by gallery apps.

## Manual PWA checklist (run at milestone 5/6)

- [ ] Install flow from Chrome; "Installed ✓" shows on `/`
- [ ] Share from the Twitter app / TikTok app / Chrome
- [ ] Multi-image tweet → "allow multiple downloads" prompt path
- [ ] Gif tweet → `.gif` plays animated in the gallery, `.mp4` alongside
- [ ] Offline share → clean "No connection" error with Retry
- [ ] Expired cookie share → `/login?next=…` round-trip completes the share
- [ ] Uninstall / reinstall

## `/share-debug`

Auth-gated page that dumps the share-target query params, environment (display mode, opener, referrer), tests 3-file programmatic downloads with 0 / 400 / 1000 ms stagger, tests `window.close()`, and can simulate a share. It also POSTs what it observes to `/v1/debug/log` so the findings appear in `docker compose logs app` as `share-debug:` lines. To receive real shares on it, temporarily set `"action": "/share-debug"` in `server/static/manifest.webmanifest`, redeploy, and reinstall the PWA.

## Security

- Single `ACCESS_KEY`; browser paths use a signed `HttpOnly; SameSite=Lax; Secure` cookie, scripts use `X-Api-Key`. Never commit `server/.env`.
- Download links are signed tokens that expire after 15 minutes.
- Resolve is rate limited (30 / 5 min per IP).
- If you set `COOKIES_PATH` (Netscape `cookies.txt` exported from a logged-in browser), that file is a **live session for your accounts**: mount it read-only, keep it out of git (it is gitignored), and rotate it if the droplet is ever compromised.
- Both platforms' ToS prohibit automated downloading. Personal use only; the access key is the privacy.

## Layout

```
server/app        FastAPI app (main, config, auth, urlnorm, providers/, ytdlp_util, dl, convert, ratelimit)
server/static     the PWA (index/share/login/share-debug html, app.js, share.js, sw.js, manifest, icons/)
server/tests      pytest (no live network)
scripts/          deploy.sh, smoke.sh, gen_icons.py
```
