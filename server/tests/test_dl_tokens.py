import time

import pytest

from app import dl
from app.config import settings
from app.models import MediaSource


def _src(**kw):
    base = dict(kind="video", filename="twitter_user_1_1.mp4", mime="video/mp4", upstream_url="https://cdn.example/v.mp4", headers={"Referer": "https://www.tiktok.com/"})
    base.update(kw)
    return MediaSource(**base)


def test_round_trip():
    src = _src()
    tok = dl.mint_dl_token(src)
    back = dl.read_dl_token(tok)
    assert back.upstream_url == src.upstream_url
    assert back.headers == src.headers
    assert back.filename == src.filename
    assert back.mime == "video/mp4"
    assert back.local_path is None
    assert "cdn.example" not in tok  # payload is compressed/encoded, not readable in the URL


def test_local_path_round_trip(tmp_path):
    p = tmp_path / "x.gif"
    src = _src(kind="gif", filename="a.gif", mime="image/gif", upstream_url=None, local_path=str(p))
    back = dl.read_dl_token(dl.mint_dl_token(src))
    assert back.local_path == str(p)
    assert back.upstream_url is None


def test_tampered_token_rejected():
    tok = dl.mint_dl_token(_src())
    with pytest.raises(dl.TokenError):
        dl.read_dl_token(tok[:-3] + "abc")
    with pytest.raises(dl.TokenError):
        dl.read_dl_token("garbage")


def test_expired_token(monkeypatch):
    tok = dl.mint_dl_token(_src())
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + settings().dl_token_ttl + 5)
    with pytest.raises(dl.TokenExpired):
        dl.read_dl_token(tok)


def test_zip_token_round_trip():
    srcs = [_src(filename="a.jpg", mime="image/jpeg"), _src(filename="b.jpg", mime="image/jpeg")]
    name, back = dl.read_zip_token(dl.mint_zip_token("twitter_user_1.zip", srcs))
    assert name == "twitter_user_1.zip"
    assert [s.filename for s in back] == ["a.jpg", "b.jpg"]


def test_content_disposition_sanitizes():
    assert dl.content_disposition('we"ird name.mp4') == 'attachment; filename="we_ird_name.mp4"'
    assert dl.content_disposition("twitter_user_1_1.mp4") == 'attachment; filename="twitter_user_1_1.mp4"'


def test_dl_endpoint_expired_and_bad(client, monkeypatch):
    tok = dl.mint_dl_token(_src())
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + settings().dl_token_ttl + 5)
    r = client.get(f"/v1/dl/{tok}")
    assert r.status_code == 410
    assert "expired" in r.text.lower()
    r = client.get("/v1/dl/not-a-token")
    assert r.status_code == 404


def test_dl_endpoint_serves_local_file(client, tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"GIF89a-fake")
    src = _src(kind="gif", filename="twitter_u_1_1.gif", mime="image/gif", upstream_url=None, local_path=str(p))
    r = client.get(f"/v1/dl/{dl.mint_dl_token(src)}")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="twitter_u_1_1.gif"'
    assert r.headers["content-type"].startswith("image/gif")
    assert r.content == b"GIF89a-fake"


@pytest.mark.asyncio
async def test_zip_stream_is_valid_zip(tmp_path):
    import io
    import zipfile

    a = tmp_path / "a.txt"; a.write_bytes(b"hello")
    b = tmp_path / "b.txt"; b.write_bytes(b"world!!")
    srcs = [
        MediaSource(kind="photo", filename="a.txt", mime="text/plain", local_path=str(a)),
        MediaSource(kind="photo", filename="b.txt", mime="text/plain", local_path=str(b)),
    ]
    buf = io.BytesIO()
    async for chunk in dl.zip_stream(srcs):
        buf.write(chunk)
    z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    assert z.testzip() is None
    assert z.namelist() == ["a.txt", "b.txt"]
    assert z.read("b.txt") == b"world!!"
