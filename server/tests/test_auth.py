from urllib.parse import quote

from app.auth import COOKIE_NAME


def test_health_open(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ytdlp_version"]


def test_login_page_and_wrong_key(client):
    assert client.get("/login").status_code == 200
    r = client.post("/login", data={"key": "nope", "next": "/"})
    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies


def test_login_sets_cookie_and_redirects_to_next(client):
    r = client.post("/login?next=%2Fshare%3Ftext%3Dhi", data={"key": "test-access-key", "next": "/share?text=hi"})
    assert r.status_code == 303
    assert r.headers["location"] == "/share?text=hi"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
    assert "Max-Age=31536000" in cookie
    # Cookie now grants access to pages and API.
    client.cookies.set(COOKIE_NAME, r.cookies[COOKIE_NAME])
    assert client.get("/").status_code == 200
    assert client.get("/v1/me").status_code == 200


def test_login_json(client):
    r = client.post("/login", json={"key": "test-access-key", "next": "/share?x=1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "next": "/share?x=1"}
    assert COOKIE_NAME in r.cookies


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"key": "test-access-key", "next": "https://evil.example/"})
    assert r.headers["location"] == "/"
    r = client.post("/login", data={"key": "test-access-key", "next": "//evil.example/"})
    assert r.headers["location"] == "/"


def test_share_without_cookie_redirects_to_login_with_next(client):
    share = "/share?url=&text=Check%20this%20https%3A%2F%2Fx.com%2Fjack%2Fstatus%2F20&title=t"
    r = client.get(share)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/login?next=")
    assert loc == "/login?next=" + quote(share, safe="")
    # and the login page then bounces back to exactly that share URL
    r2 = client.post(loc, data={"key": "test-access-key"})
    assert r2.status_code == 303 and r2.headers["location"] == share


def test_share_with_bad_cookie_redirects(client):
    client.cookies.set(COOKIE_NAME, "forged.value")
    r = client.get("/share?text=x")
    assert r.status_code == 302 and r.headers["location"].startswith("/login?next=")


def test_share_and_debug_with_cookie(auth_client):
    assert auth_client.get("/share?text=x").status_code == 200
    r = auth_client.get("/share-debug?text=x")
    assert r.status_code == 200 and "Share-target debug" in r.text
    r = auth_client.get("/v1/debug/file/2")
    assert r.headers["content-disposition"] == 'attachment; filename="mediasaver-test-2.txt"'


def test_index_redirects_without_cookie(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2F"


def test_resolve_requires_auth(client):
    r = client.post("/v1/resolve", json={"url": "https://x.com/jack/status/20"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_resolve_api_key_header(client):
    r = client.post("/v1/resolve", json={"url": "no link"}, headers={"X-Api-Key": "test-access-key"})
    assert r.status_code == 400
    assert r.json() == {"error": "unsupported_url", "message": "No link found in what was shared."}
    r = client.post("/v1/resolve", json={"url": "https://x.com/jack/status/20"}, headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401


def test_resolve_garbage_400(auth_client):
    r = auth_client.post("/v1/resolve", json={"url": "https://youtube.com/watch?v=1"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_url"
    r = auth_client.post("/v1/resolve", json={"nope": 1})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_url"
    r = auth_client.post("/v1/resolve", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_resolve_provider_stub_502(auth_client):
    r = auth_client.post("/v1/resolve", json={"url": "https://x.com/jack/status/20"})
    assert r.status_code == 502 and r.json()["error"] == "extract_failed"


def test_rate_limit(auth_client):
    codes = [auth_client.post("/v1/resolve", json={"url": "https://x.com/jack/status/20"}).status_code for _ in range(6)]
    assert codes[:5] == [502] * 5
    assert codes[5] == 429
    r = auth_client.post("/v1/resolve", json={"url": "https://x.com/jack/status/20"})
    assert r.json()["error"] == "rate_limited" and int(r.headers["retry-after"]) >= 1


def test_static_assets_open(client):
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/manifest.webmanifest").json()["share_target"]["action"] == "/share"
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/icons/512.png").status_code == 200
