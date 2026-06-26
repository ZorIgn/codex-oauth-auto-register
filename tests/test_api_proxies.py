"""Proxy management endpoint tests."""
from __future__ import annotations


def test_list_proxies_empty(client):
    resp = client.get("/api/proxies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_add_proxy(client):
    resp = client.post("/api/proxies", json={"url": "http://127.0.0.1:7890", "region": "US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "http://127.0.0.1:7890"
    assert data["region"] == "US"


def test_add_and_list_proxy(client):
    client.post("/api/proxies", json={"url": "http://127.0.0.1:7890"})
    resp = client.get("/api/proxies")
    data = resp.json()
    assert len(data) == 1


def test_delete_proxy(client):
    create_resp = client.post("/api/proxies", json={"url": "http://127.0.0.1:7890"})
    proxy_id = create_resp.json()["id"]
    del_resp = client.delete(f"/api/proxies/{proxy_id}")
    assert del_resp.status_code == 200
    # Verify deleted
    list_resp = client.get("/api/proxies")
    assert len(list_resp.json()) == 0


def test_bulk_add_proxies(client):
    resp = client.post("/api/proxies/bulk", json={
        "proxies": ["http://1.1.1.1:8080", "http://2.2.2.2:8080"],
        "region": "SG",
    })
    assert resp.status_code == 200
    list_resp = client.get("/api/proxies")
    assert len(list_resp.json()) == 2
