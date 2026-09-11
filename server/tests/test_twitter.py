import json
from pathlib import Path

import pytest

from app.errors import NoMedia, NotFound, Private
from app.providers.twitter import TwitterProvider, _best_mp4, syndication_token
from app.urlnorm import canonicalize

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def provider(fixture=None, data=None, converted=None):
    payload = data if data is not None else (load(fixture) if fixture else None)

    async def fetch(twid):
        return payload

    async def conv(url, headers=None):
        converted.append((url, headers or {})) if converted is not None else None
        p = FIX / "fake_out.gif"
        p.write_bytes(b"GIF89a-test")
        return p

    return TwitterProvider(fetcher=fetch, converter=conv)


def target(twid):
    return canonicalize(f"https://x.com/i/web/status/{twid}")


@pytest.mark.parametrize(
    "twid,tok",
    [("657991469417025536", "1lf52y9f4it"), ("1577855540407197696", "3toz99oivuy"), ("20", "6dq1a2xwd93"), ("1946306971306467682", "4c9gcitu1vq" if False else None)],
)
def test_syndication_token_matches_ytdlp(twid, tok):
    from yt_dlp.jsinterp import js_number_to_string
    import math

    expected = js_number_to_string((int(twid) / 1e15) * math.pi, 36).translate(str.maketrans(dict.fromkeys("0.")))
    assert syndication_token(twid) == expected
    if tok:
        assert syndication_token(twid) == tok  # values observed live against cdn.syndication.twimg.com


def test_fallback_number_to_string_matches_ytdlp():
    import math
    from yt_dlp.jsinterp import js_number_to_string
    from app.providers.twitter import _js_number_to_string

    for twid in ["20", "657991469417025536", "1577855540407197696", "2098024802980110352", "1623739803874349067"]:
        val = (int(twid) / 1e15) * math.pi
        assert _js_number_to_string(val, 36) == js_number_to_string(val, 36)


async def test_photo_and_video():
    post = await provider("twitter_photo_video_1577855540407197696.json").resolve(target("1577855540407197696"), gif_keep_mp4=True)
    assert post.author == "oshtru" and post.post_id == "1577855540407197696"
    assert [s.kind for s in post.sources] == ["photo", "video"]
    photo, video = post.sources
    assert photo.filename == "twitter_oshtru_1577855540407197696_1.jpg"
    assert photo.upstream_url == "https://pbs.twimg.com/media/FeWrKrMaYAELAxK.jpg?format=jpg&name=orig"
    assert photo.mime == "image/jpeg"
    assert video.filename == "twitter_oshtru_1577855540407197696_2.mp4"
    assert video.upstream_url.endswith(".mp4?tag=12") or "video.twimg.com" in video.upstream_url
    assert (video.width, video.height) == (720, 900)


async def test_video_with_two_photos_keeps_post_order():
    post = await provider("twitter_video_2photos_1623739803874349067.json").resolve(target("1623739803874349067"), gif_keep_mp4=True)
    assert [s.kind for s in post.sources] == ["video", "photo", "photo"]
    assert [s.filename[-6:] for s in post.sources] == ["_1.mp4", "_2.jpg", "_3.jpg"]


async def test_highest_bitrate_mp4_chosen():
    data = load("twitter_video_user_2098083773837431020.json")
    m = data["mediaDetails"][0]
    best = _best_mp4(m["video_info"])
    assert best["bitrate"] == 2176000 and best["content_type"] == "video/mp4"
    post = await provider(data=data).resolve(target("2098083773837431020"), gif_keep_mp4=True)
    assert post.sources[0].upstream_url == best["url"]
    assert post.sources[0].kind == "video" and (post.sources[0].width, post.sources[0].height) == (1080, 546)


async def test_animated_gif_becomes_gif_plus_mp4():
    conv = []
    post = await provider("twitter_gif_user_2098024802980110352.json", converted=conv).resolve(target("2098024802980110352"), gif_keep_mp4=True)
    assert [s.kind for s in post.sources] == ["gif", "video"]
    gif, mp4 = post.sources
    assert gif.filename == "twitter_GoldLeopardess_2098024802980110352_1.gif"
    assert mp4.filename == "twitter_GoldLeopardess_2098024802980110352_1.mp4"
    assert gif.local_path and gif.local_path.endswith("fake_out.gif") and gif.upstream_url is None
    assert gif.bytes == len(b"GIF89a-test")
    assert mp4.upstream_url == "https://video.twimg.com/tweet_video/HR2uaT6bIAA4CLf.mp4"
    assert conv == [("https://video.twimg.com/tweet_video/HR2uaT6bIAA4CLf.mp4", {})]
    assert (gif.width, gif.height) == (540, 540)


async def test_animated_gif_without_mp4():
    post = await provider("twitter_gif_1946306971306467682.json").resolve(target("1946306971306467682"), gif_keep_mp4=False)
    assert [s.kind for s in post.sources] == ["gif"]


async def test_text_only_no_media():
    with pytest.raises(NoMedia):
        await provider("twitter_text_only_20.json").resolve(target("20"), gif_keep_mp4=True)


async def test_tombstone_not_found():
    with pytest.raises(NotFound):
        await provider("twitter_tombstone_657991469417025536.json").resolve(target("657991469417025536"), gif_keep_mp4=True)


async def test_empty_response_not_found():
    with pytest.raises(NotFound):
        await provider(data={}).resolve(target("1"), gif_keep_mp4=True)


async def test_protected_tombstone_is_private_without_cookies():
    data = {"__typename": "TweetTombstone", "tombstone": {"text": {"text": "This Post is from an account that no longer exists or is protected. Learn more"}}}
    with pytest.raises(Private):
        await provider(data=data).resolve(target("1"), gif_keep_mp4=True)


async def test_quoted_tweet_media_ignored():
    data = load("twitter_text_only_20.json")
    data["quoted_tweet"] = {"mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]}
    with pytest.raises(NoMedia):
        await provider(data=data).resolve(target("20"), gif_keep_mp4=True)


async def test_ytdlp_fallback_shape_gif_heuristic():
    p = provider(data={})
    info = {
        "_type": "playlist", "uploader_id": "someone", "description": "hello",
        "entries": [
            {"duration": 4, "formats": [{"url": "https://v/a.mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "tbr": 100, "width": 100, "height": 50}]},
            {"duration": 60, "formats": [{"url": "https://v/b.mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "tbr": 100, "filesize": 5}]},
        ],
    }
    post = p._from_ytdlp(info, "77", True)
    assert [s.kind for s in post.sources] == ["gif", "video", "video"]
    assert post.sources[0].filename == "twitter_someone_77_1.gif"
    assert post.sources[2].bytes == 5


async def test_resolve_endpoint_end_to_end(auth_client, monkeypatch):
    from app import providers

    monkeypatch.setitem(providers.PROVIDERS, "twitter", provider("twitter_video_2photos_1623739803874349067.json"))
    r = auth_client.post("/v1/resolve", json={"url": "look https://x.com/liberdalau/status/1623739803874349067?s=20"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "twitter" and body["author"] == "Johnnybull3ts"  # URL username is ignored; author comes from the tweet
    assert body["summary"] == "video + 2 images"
    assert [i["kind"] for i in body["items"]] == ["video", "photo", "photo"]
    assert all(i["dl"].startswith("/v1/dl/") for i in body["items"])
    assert body["zip"].startswith("/v1/zip/")
    assert "twimg.com" not in r.text  # raw CDN URLs never reach the page
