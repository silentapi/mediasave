#!/usr/bin/env bash
# Smoke test against a running instance: health + resolve for each post type + one download's headers.
# Usage: BASE=http://127.0.0.1:8000 ACCESS_KEY=... scripts/smoke.sh
# Sample posts can be overridden with TW_PHOTO, TW_MULTI, TW_VIDEO, TW_GIF, TT_VIDEO, TT_PHOTO.
set -uo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
KEY="${ACCESS_KEY:-}"
if [ -z "$KEY" ] && [ -f "$(dirname "$0")/../server/.env" ]; then
  KEY="$(grep -E '^ACCESS_KEY=' "$(dirname "$0")/../server/.env" | head -1 | cut -d= -f2-)"
fi
[ -n "$KEY" ] || { echo "ACCESS_KEY not set" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

fail=0
pass() { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; fail=1; }

echo "health:"
H="$(curl -sS "$BASE/v1/health")" && echo "$H" | jq -e '.ok==true' >/dev/null && pass "ok ($(echo "$H" | jq -r .ytdlp_version))" || bad "health failed: $H"

echo "auth:"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/resolve" -H 'Content-Type: application/json' -d '{"url":"https://x.com/a/status/1"}')"
[ "$code" = "401" ] && pass "resolve without key → 401" || bad "resolve without key → $code (want 401)"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/resolve" -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' -d '{"url":"hello no link here"}')"
[ "$code" = "400" ] && pass "garbage → 400" || bad "garbage → $code (want 400)"

# resolve <label> <url> <expected item count or ''> <expected kinds csv or ''>
resolve() {
  local label="$1" url="$2" want_n="$3" want_kinds="$4"
  [ -n "$url" ] || { echo "  - $label: skipped (no sample URL)"; return; }
  local out
  out="$(curl -sS -X POST "$BASE/v1/resolve" -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' -d "{\"url\":$(jq -Rn --arg u "$url" '$u')}")"
  if ! echo "$out" | jq -e '.items' >/dev/null 2>&1; then bad "$label: $(echo "$out" | head -c 200)"; return; fi
  local n kinds
  n="$(echo "$out" | jq '.items|length')"; kinds="$(echo "$out" | jq -r '[.items[].kind]|join(",")')"
  if [ -n "$want_n" ] && [ "$n" != "$want_n" ]; then bad "$label: $n items (want $want_n) kinds=$kinds"; return; fi
  if [ -n "$want_kinds" ] && [ "$kinds" != "$want_kinds" ]; then bad "$label: kinds=$kinds (want $want_kinds)"; return; fi
  pass "$label: $(echo "$out" | jq -r '.summary') by @$(echo "$out" | jq -r .author) ($(echo "$out" | jq -r '[.items[].filename]|join(", ")'))"
  FIRST_DL="${FIRST_DL:-$(echo "$out" | jq -r '.items[0].dl')}"
  FIRST_NAME="${FIRST_NAME:-$(echo "$out" | jq -r '.items[0].filename')}"
  # remember one gif and one remote item so the header check covers both serving paths
  local g; g="$(echo "$out" | jq -r '[.items[]|select(.kind=="gif")][0].dl // empty')"; [ -n "$g" ] && GIF_DL="${GIF_DL:-$g}"
}

# Expected kinds can be overridden per case with <VAR>_KINDS (comma-separated), e.g. TW_PHOTO_KINDS=photo,video
echo "twitter:"
resolve "photo"       "${TW_PHOTO:-}" "" "${TW_PHOTO_KINDS:-photo}"
resolve "multi-photo" "${TW_MULTI:-}" "" "${TW_MULTI_KINDS:-}"
resolve "video"       "${TW_VIDEO:-}" "" "${TW_VIDEO_KINDS:-video}"
resolve "gif"         "${TW_GIF:-}"   "" "${TW_GIF_KINDS:-gif,video}"
echo "tiktok:"
resolve "video"       "${TT_VIDEO:-}" "" "${TT_VIDEO_KINDS:-video}"
resolve "photo-mode"  "${TT_PHOTO:-}" "" "${TT_PHOTO_KINDS:-}"

if [ -n "${FIRST_DL:-}" ]; then
  echo "download headers:"
  hdr="$(curl -sS -D - -o /dev/null -r 0-1023 "$BASE$FIRST_DL" 2>&1)"
  echo "$hdr" | grep -qi "content-disposition: attachment; filename=\"$FIRST_NAME\"" && pass "Content-Disposition attachment; filename=\"$FIRST_NAME\"" || bad "bad Content-Disposition: $(echo "$hdr" | grep -i content-disposition)"
  echo "$hdr" | grep -qiE '^HTTP/[0-9.]+ (200|206)' && pass "status $(echo "$hdr" | head -1 | tr -d '\r')" || bad "status: $(echo "$hdr" | head -1)"
fi
if [ -n "${GIF_DL:-}" ]; then
  echo "gif download:"
  tmp="$(mktemp)"; curl -sS -o "$tmp" "$BASE$GIF_DL"
  head -c 6 "$tmp" | grep -q 'GIF8' && pass "gif magic OK ($(wc -c < "$tmp") bytes)" || bad "not a gif"
  rm -f "$tmp"
fi

[ $fail -eq 0 ] && echo "SMOKE OK" || { echo "SMOKE FAILED"; exit 1; }
