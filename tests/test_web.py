import pytest
from fastapi.testclient import TestClient


def test_root_redirects_to_the_web_ui(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_web_ui_page_is_served(client: TestClient) -> None:
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Docs Extraction" in response.text
    assert 'src="app.js"' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/ui/app.js", "javascript"), ("/ui/styles.css", "text/css")],
)
def test_web_ui_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_web_ui_is_public(client: TestClient) -> None:
    """The page itself needs no key; it asks for one and sends it on API calls."""
    assert client.get("/ui/", headers={"X-API-Key": ""}).status_code == 200


def test_web_ui_does_not_shadow_the_api(client: TestClient) -> None:
    assert client.get("/jobs").status_code == 200
    assert client.get("/ui/missing.js").status_code == 404
