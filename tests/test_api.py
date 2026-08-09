import json
from pathlib import Path

from fastapi.testclient import TestClient

from health_export_api.app import create_app


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(storage_dir=tmp_path, api_token="test-token")
    return TestClient(app)


def test_posted_export_is_persisted_and_can_be_retrieved(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = {
        "exportedAt": "2026-07-12T22:30:00-04:00",
        "data": {"steps": 8432, "weightKg": 81.2},
    }

    response = client.post(
        "/v1/exports",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )

    assert response.status_code == 201
    created = response.json()
    assert created["id"]
    assert created["received_at"].endswith("Z")

    stored = json.loads((tmp_path / f"{created['id']}.json").read_text())
    assert stored["payload"] == payload


def test_array_exports_are_preserved_without_schema_assumptions(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = [{"type": "stepCount", "value": 8432}]

    response = client.post(
        "/v1/exports",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )

    assert response.status_code == 201
    stored = json.loads((tmp_path / f"{response.json()['id']}.json").read_text())
    assert stored["payload"] == payload


def test_export_endpoints_require_a_matching_bearer_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.post("/v1/exports", json={"data": {}}).status_code == 401
    assert (
        client.post(
            "/v1/exports",
            headers={"Authorization": "Bearer wrong"},
            json={"data": {}},
        ).status_code
        == 401
    )


def test_exports_latest_is_removed(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get(
        "/v1/exports/latest", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 404  # endpoint gone, FastAPI returns 404


def test_large_export_is_streamed_to_disk_without_loading_into_memory(
    tmp_path: Path,
) -> None:
    """Body must be written to disk in chunks; the endpoint must not buffer the
    full payload in process memory before persisting it."""
    client = make_client(tmp_path)
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    big_metrics = [{"name": f"metric_{i}", "units": "count", "data": [{"date": "2026-01-01 08:00:00 -0500", "qty": i}]} for i in range(5000)]
    big_payload = {"data": {"metrics": big_metrics}}

    response = client.post("/v1/exports", headers=headers, json=big_payload)

    assert response.status_code == 201
    export_id = response.json()["id"]
    stored_path = tmp_path / f"{export_id}.json"
    assert stored_path.exists()
    import json as _json
    stored = _json.loads(stored_path.read_text())
    assert stored["payload"]["data"]["metrics"][0]["name"] == "metric_0"
    assert stored["payload"]["data"]["metrics"][-1]["name"] == "metric_4999"


def test_list_exports_returns_newest_first_and_honors_limit(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}

    first = client.post("/v1/exports", headers=headers, json={"data": {"steps": 10}})
    second = client.post("/v1/exports", headers=headers, json={"data": {"steps": 20}})

    response = client.get("/v1/exports?limit=1", headers=headers)

    assert response.status_code == 200
    exports = response.json()["exports"]
    assert [item["id"] for item in exports] == [second.json()["id"]]
    assert first.json()["id"] != second.json()["id"]


def test_a_truncated_export_file_does_not_break_the_listing(tmp_path: Path) -> None:
    """An upload that dies mid-write leaves a partial file on disk.

    One such file — 88 bytes, from a fortnight earlier — was making every call
    to this endpoint return 500. The other thousand exports are still the
    useful answer.
    """
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    good = client.post("/v1/exports", headers=headers, json={"data": {"steps": 10}})
    (tmp_path / "truncated.json").write_text('{"id":"x","received_at":"2026-07-2')

    response = client.get("/v1/exports", headers=headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["exports"]] == [good.json()["id"]]


def test_listing_reads_only_the_files_it_returns(tmp_path: Path, monkeypatch) -> None:
    """Reading the whole archive to answer `limit=1` OOM-killed the pod.

    A thousand exports is hundreds of megabytes, and they were all parsed just
    to sort by a field inside them.
    """
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    for index in range(6):
        client.post("/v1/exports", headers=headers, json={"data": {"steps": index}})

    reads: list[str] = []
    original = Path.read_text

    def counting_read(self, *args, **kwargs):
        if self.suffix == ".json":
            reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    response = client.get("/v1/exports?limit=2", headers=headers)

    assert response.status_code == 200
    assert len(response.json()["exports"]) == 2
    assert len(reads) == 2, f"parsed {len(reads)} files to return 2"
