"""Admin debug relationship scoring endpoints (open, no auth)."""

from fastapi.testclient import TestClient


def test_debug_relationship_get_and_write(monkeypatch):
    from mochi.admin import admin_server
    from mochi.relationship_model import RQI_WEIGHTS

    monkeypatch.setattr("mochi.config.OWNER_USER_ID", 1)

    client = TestClient(admin_server.app)
    got = client.get("/api/debug/relationship")
    assert got.status_code == 200
    payload = got.json()
    assert set(payload["weights"]) == set(RQI_WEIGHTS)
    assert "labels" in payload

    scores = {key: 7.0 for key in RQI_WEIGHTS}
    preview = client.post(
        "/api/debug/relationship/preview",
        json={"dimensions": scores},
    )
    assert preview.status_code == 200
    assert preview.json()["tier"] == "Healthy"
    assert preview.json()["rqi"] == 7.0

    written = client.post(
        "/api/debug/relationship",
        json={
            "dimensions": scores,
            "note": "debug unit",
            "refresh_voice": True,
        },
    )
    assert written.status_code == 200
    body = written.json()
    assert body["ok"] is True
    assert body["tier"] == "Healthy"
    assert body["voice_refreshed"] is True
    assert body["assessment_id"] > 0
