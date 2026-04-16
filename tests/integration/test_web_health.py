from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def test_healthz_returns_200(tmp_path):
    app = create_app(app_context=None)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
