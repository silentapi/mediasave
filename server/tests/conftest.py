import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ACCESS_KEY", "test-access-key")
os.environ.setdefault("SECRET", "test-secret-please-do-not-use")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("RESOLVE_LIMIT", "5")
os.environ.setdefault("RESOLVE_WINDOW_SECONDS", "300")


@pytest.fixture(scope="session", autouse=True)
def _data_dir(tmp_path_factory):
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    from app.config import reset_settings

    reset_settings()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import main

    main._limiter = None
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


@pytest.fixture()
def auth_client(client):
    from app.auth import COOKIE_NAME, make_session_cookie
    from app.config import settings

    client.cookies.set(COOKIE_NAME, make_session_cookie(settings()))
    return client
