import json
from pathlib import Path

import pytest

from app.errors import ExtractFailed, NoMedia, NotFound, Private
from app.providers.tiktok import DESKTOP_UA, MOBILE_UA, Page, TikTokProvider, choose_video, parse_page
from app.urlnorm import canonicalize

FIX = Path(__file__).parent / "fixtures"
VIDEO = json.loads((FIX / "tiktok_video_itemstruct_7047596209028074758.json").read_text())
PHOTO = json.loads((FIX / "tiktok_photo_itemstruct_7681541360641445133.json").read_text())
COOKIES = {"ttwid": "1%7Cabc", "tt_csrf_token": "csrf", "tt_chain_token": "TgKq+chain=="}


def make(pages, ytdlp=None):
    """pages: {ua: Page} or callable(url, ua) -> Page"""
    calls = []

    async def fetch(url, ua):
        calls.append((url, ua))
        if callable(pages):
            return pages(url, ua)
        return pages.get(ua) or Page(None, {}, {}, ua)

    async def yt(url, **kw):
        if ytdlp is None:
            raise AssertionError("yt-dlp should not be used")
        return ytdlp

    return TikTokProvider(fetcher=fetch, ytdlp=yt), calls


def video_target():
    return canonicalize("https://www.tiktok.com/@hankgreen1/video/7047596209028074758")


def photo_target():
    return canonicalize("https://www.tiktok.com/@bbscollection3.0/photo/7681541360641445133?_r=1")


def test_choose_video_prefers_h264_highest_bitrate():
    c = choose_video(VIDEO["video"])
    assert c["codec"] == "h264" and c["url"].startswith("https://v16-webapp-prime.us.tiktok.com/")
    assert c["bytes"] == 5076682 and (c["width"], c["height"]) == (576, 1024)


def test_choose_video_skips_bytevc2_and_falls_back_to_playaddr():
    v = {"bitrateInfo": [{"Bitrate": 9, "CodecType": "bytevc2", "PlayAddr": {"UrlList": ["https://x/1"]}}], "playAddr": "https://x/play", "width": 1, "height": 2, "size": 3}
    assert choose_video(v)["url"] == "https://x/play"
    v["bitrateInfo"].append({"Bitrate": 5, "CodecType": "h265_hvc1", "PlayAddr": {"UrlList": ["https://x/h265"]}})
    assert choose_video(v)["url"] == "https://x/h265"
    assert choose_video({}) is None


def test_parse_page_desktop_and_reflow_keys():
    html = '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">%s</script></html>'
    desk = html % json.dumps({"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": 0, "itemInfo": {"itemStruct": {"id": "1"}}}}})
    p = parse_page(desk, COOKIES, DESKTOP_UA)
    assert p.status == 0 and p.item == {"id": "1"} and p.cookies == COOKIES
    reflow = html % json.dumps({"__DEFAULT_SCOPE__": {"webapp.reflow.video.detail": {"statusCode": 10216, "itemInfo": {}}}})
    assert parse_page(reflow, {}, MOBILE_UA).status == 10216
    nodetail = html % json.dumps({"__DEFAULT_SCOPE__": {"webapp.app-context": {}}})
    assert parse_page(nodetail, {}, MOBILE_UA).status is None
    chal = parse_page('<html><div id="cs" class="abc"></div>Please wait...</html>', {}, DESKTOP_UA)
    assert chal.status is None and chal.challenge


async def test_video_post_uses_desktop_page_and_session_cookie():
    prov, calls = make({DESKTOP_UA: Page(0, VIDEO, COOKIES, DESKTOP_UA)})
    post = await prov.resolve(video_target(), gif_keep_mp4=True)
    assert calls == [("https://www.tiktok.com/@hankgreen1/video/7047596209028074758", DESKTOP_UA)]
    assert post.author == "hankgreen1" and post.post_id == "7047596209028074758"
    (src,) = post.sources
    assert src.kind == "video" and src.filename == "tiktok_hankgreen1_7047596209028074758.mp4"
    assert src.upstream_url.startswith("https://v16-webapp-prime.us.tiktok.com/") and src.bytes == 5076682  # DataSize unique to the 1895168 variant
    assert src.headers == {"Referer": "https://www.tiktok.com/", "User-Agent": DESKTOP_UA, "Cookie": "tt_chain_token=TgKq+chain=="}


async def test_photo_post_uses_mobile_page_and_lists_every_image():
    prov, calls = make({MOBILE_UA: Page(0, PHOTO, COOKIES, MOBILE_UA)})
    post = await prov.resolve(photo_target(), gif_keep_mp4=True)
    assert calls[0][1] == MOBILE_UA and len(calls) == 1
    assert post.author == "bbscollection3.0"
    assert [s.kind for s in post.sources] == ["photo", "photo"]
    assert [s.filename for s in post.sources] == ["tiktok_bbscollection3.0_7681541360641445133_1.jpeg", "tiktok_bbscollection3.0_7681541360641445133_2.jpeg"]
    assert all(s.mime == "image/jpeg" and s.upstream_url.startswith("https://p") and ".tiktokcdn-us.com/" in s.upstream_url for s in post.sources)
    assert (post.sources[0].width, post.sources[0].height) == (1170, 1170)
    assert "Cookie" not in post.sources[0].headers


async def test_video_url_that_is_actually_a_photo_post_retries_with_mobile_ua():
    prov, calls = make({MOBILE_UA: Page(0, PHOTO, {}, MOBILE_UA), DESKTOP_UA: Page(None, {}, {}, DESKTOP_UA)})
    post = await prov.resolve(video_target(), gif_keep_mp4=True)
    assert [ua for _, ua in calls] == [DESKTOP_UA, MOBILE_UA]
    assert len(post.sources) == 2


@pytest.mark.parametrize("status,exc", [(10216, Private), (10222, Private), (10204, Private), (10202, NotFound), (10203, NotFound)])
async def test_status_codes(status, exc):
    prov, _ = make({DESKTOP_UA: Page(status, {}, {}, DESKTOP_UA)})
    with pytest.raises(exc):
        await prov.resolve(video_target(), gif_keep_mp4=True)


async def test_audio_only_slideshow_is_no_media():
    item = {"id": "1", "author": {"uniqueId": "a"}, "video": {"duration": 0, "width": 0, "height": 0}, "music": {"playUrl": "https://m/x.mp3"}}
    prov, _ = make({DESKTOP_UA: Page(0, item, {}, DESKTOP_UA)})
    with pytest.raises(NoMedia):
        await prov.resolve(video_target(), gif_keep_mp4=True)


async def test_challenge_page_falls_back_to_ytdlp_with_cookies():
    info = {
        "uploader": "hankgreen1", "description": "hi", "http_headers": {"Referer": "https://www.tiktok.com/@hankgreen1/video/7047596209028074758", "User-Agent": "yt-dlp-ua"},
        "__cookies": {"tt_chain_token": "fromjar"},
        "formats": [
            {"format_id": "download", "url": "https://v/wm.mp4", "vcodec": "h264", "acodec": "aac", "format_note": "watermarked", "height": 1024},
            {"format_id": "bytevc1_720p_1", "url": "https://v/h265.mp4", "vcodec": "h265", "acodec": "aac", "height": 1280, "tbr": 900},
            {"format_id": "h264_540p_1", "url": "https://v/h264.mp4", "vcodec": "h264", "acodec": "aac", "height": 1024, "tbr": 1800, "filesize": 42},
            {"format_id": "audio", "url": "https://v/a.m4a", "vcodec": "none", "acodec": "aac"},
        ],
    }
    prov, calls = make(lambda url, ua: Page(None, {}, {}, ua, challenge=True), ytdlp=info)
    post = await prov.resolve(video_target(), gif_keep_mp4=True)
    assert len(calls) == 2  # both UAs tried before falling back
    (src,) = post.sources
    assert src.upstream_url == "https://v/h264.mp4" and src.bytes == 42
    assert src.headers["Cookie"] == "tt_chain_token=fromjar" and src.headers["User-Agent"] == "yt-dlp-ua"
    assert src.headers["Referer"].startswith("https://www.tiktok.com/")


async def test_photo_post_without_data_is_extract_failed():
    prov, _ = make(lambda url, ua: Page(None, {}, {}, ua), ytdlp={})
    with pytest.raises(ExtractFailed):
        await prov.resolve(photo_target(), gif_keep_mp4=True)


async def test_resolve_endpoint_tiktok_end_to_end(auth_client, monkeypatch):
    from app import providers

    prov, _ = make({MOBILE_UA: Page(0, PHOTO, COOKIES, MOBILE_UA)})
    monkeypatch.setitem(providers.PROVIDERS, "tiktok", prov)
    r = auth_client.post("/v1/resolve", json={"url": "Check out https://www.tiktok.com/@bbscollection3.0/photo/7681541360641445133?_r=1&_t=abc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "tiktok" and body["summary"] == "2 images" and body["zip"]
    assert "tiktokcdn" not in r.text
