import httpx
import pytest

from app.errors import UnsupportedUrl
from app.urlnorm import canonicalize, expand, extract_first_url, is_shortener, normalize

TW = "twitter"
TT = "tiktok"

TABLE = [
    ("https://twitter.com/jack/status/20", TW, "20"),
    ("https://x.com/jack/status/20", TW, "20"),
    ("https://mobile.twitter.com/jack/status/20", TW, "20"),
    ("https://www.x.com/jack/status/20?s=20&t=abc", TW, "20"),
    ("https://x.com/jack/status/20/photo/1", TW, "20"),
    ("https://x.com/jack/status/20/video/1", TW, "20"),
    ("https://twitter.com/i/web/status/1234567890123456789", TW, "1234567890123456789"),
    ("https://x.com/i/status/1234567890123456789", TW, "1234567890123456789"),
    ("http://twitter.com/Some_User/statuses/98765432101", TW, "98765432101"),
    ("https://fxtwitter.com/jack/status/20", TW, "20"),
    ("https://www.tiktok.com/@scout2015/video/6718335390845095173", TT, "6718335390845095173"),
    ("https://www.tiktok.com/@scout2015/video/6718335390845095173?is_from_webapp=1&sender_device=pc", TT, "6718335390845095173"),
    ("https://www.tiktok.com/@user.name_1/photo/7300000000000000000", TT, "7300000000000000000"),
    ("https://m.tiktok.com/v/6718335390845095173.html", TT, "6718335390845095173"),
    ("https://tiktok.com/@a/video/6718335390845095173/", TT, "6718335390845095173"),
    ("https://www.tiktok.com/embed/v2/6718335390845095173", TT, "6718335390845095173"),
    ("https://x.com/i/web/status/20?utm=1#frag", TW, "20"),
]


@pytest.mark.parametrize("url,provider,pid", TABLE)
def test_canonicalize_table(url, provider, pid):
    n = canonicalize(url)
    assert (n.provider, n.post_id) == (provider, pid)
    assert n.canonical_url.startswith("https://")


def test_canonical_urls():
    assert canonicalize("https://x.com/jack/status/20/photo/1").canonical_url == "https://x.com/i/web/status/20"
    assert canonicalize("https://www.tiktok.com/@scout2015/video/6718335390845095173?x=1").canonical_url == "https://www.tiktok.com/@scout2015/video/6718335390845095173"
    assert canonicalize("https://www.tiktok.com/@u/photo/7300000000000000000").canonical_url.endswith("/photo/7300000000000000000")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x/status/1",
        "https://x.com/jack",
        "https://x.com/home",
        "https://twitter.com/jack/likes",
        "https://www.tiktok.com/@scout2015",
        "https://www.tiktok.com/tag/fyp",
        "https://youtube.com/watch?v=abc",
        "not a url at all",
    ],
)
def test_unsupported(url):
    with pytest.raises(UnsupportedUrl):
        canonicalize(url)


@pytest.mark.parametrize(
    "text,url",
    [
        ("Check out this video! https://vm.tiktok.com/ZMabc123/", "https://vm.tiktok.com/ZMabc123/"),
        ("look https://x.com/jack/status/20?s=20 wow", "https://x.com/jack/status/20?s=20"),
        ("https://x.com/jack/status/20.", "https://x.com/jack/status/20"),
        ("(https://x.com/jack/status/20)", "https://x.com/jack/status/20"),
        ("nothing here", None),
        ("", None),
        ("text\nhttps://t.co/abc\n", "https://t.co/abc"),
        ("HTTPS://X.COM/jack/status/20", "HTTPS://X.COM/jack/status/20"),
    ],
)
def test_extract_first_url(text, url):
    assert extract_first_url(text) == url


def test_is_shortener():
    assert is_shortener("https://t.co/abc")
    assert is_shortener("https://vm.tiktok.com/ZMabc/")
    assert is_shortener("https://vt.tiktok.com/ZMabc/")
    assert is_shortener("https://www.tiktok.com/t/ZTabc/")
    assert not is_shortener("https://x.com/jack/status/20")
    assert not is_shortener("https://www.tiktok.com/@a/video/1")


def _mock_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key in routes:
            status, loc = routes[key]
            return httpx.Response(status, headers={"location": loc} if loc else {})
        return httpx.Response(200)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.asyncio
async def test_normalize_expands_shortener():
    routes = {
        "https://vm.tiktok.com/ZMabc/": (301, "https://www.tiktok.com/@scout2015/video/6718335390845095173?_r=1&u_code=x"),
    }
    async with _mock_client(routes) as c:
        n = await normalize("Check out this video! https://vm.tiktok.com/ZMabc/", c)
    assert n.provider == "tiktok"
    assert n.post_id == "6718335390845095173"
    assert "_r=" not in n.canonical_url


@pytest.mark.asyncio
async def test_normalize_tco_relative_redirect_chain():
    routes = {
        "https://t.co/abc": (301, "https://twitter.com/jack/status/20?s=1"),
    }
    async with _mock_client(routes) as c:
        n = await normalize("https://t.co/abc", c)
    assert (n.provider, n.post_id) == ("twitter", "20")


@pytest.mark.asyncio
async def test_expand_non_shortener_is_passthrough():
    assert await expand("https://x.com/jack/status/20") == "https://x.com/jack/status/20"


@pytest.mark.asyncio
async def test_normalize_no_url():
    with pytest.raises(UnsupportedUrl):
        await normalize("just words")
