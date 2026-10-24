import json
from pathlib import Path

from fastapi.testclient import TestClient

from shop.settings import ShopSettings
from tests.conftest import ADMIN


def read_log_lines(settings: ShopSettings) -> list[dict]:
    return [json.loads(line) for line in Path(settings.log_file).read_text().splitlines()]


def request_lines(settings: ShopSettings) -> list[dict]:
    return [r for r in read_log_lines(settings) if r.get("event") == "http_request"]


# --- core API ---------------------------------------------------------------------------
def test_health(shop: TestClient) -> None:
    assert shop.get("/health").json() == {"status": "ok", "db": "ok"}


def test_create_order_reduces_stock(shop: TestClient) -> None:
    before = shop.get("/products").json()[0]["stock"]
    r = shop.post("/orders", json={"product_id": 1, "quantity": 2})
    assert r.status_code == 201
    assert r.json()["total"] == 1300.0
    assert shop.get("/products").json()[0]["stock"] == before - 2


def test_order_errors(shop: TestClient) -> None:
    assert shop.post("/orders", json={"product_id": 999, "quantity": 1}).status_code == 404
    assert shop.post("/orders", json={"product_id": 1, "quantity": 0}).status_code == 422


def test_list_orders_newest_first_and_limit(shop: TestClient) -> None:
    ids = [
        shop.post("/orders", json={"product_id": 1, "quantity": 1}).json()["id"] for _ in range(3)
    ]
    listed = shop.get("/orders", params={"limit": 2}).json()
    assert [o["id"] for o in listed] == [ids[2], ids[1]]
    assert shop.get("/orders", params={"limit": 101}).status_code == 422


# --- logging ---------------------------------------------------------------------------
def test_one_json_log_line_per_request(shop: TestClient, shop_settings: ShopSettings) -> None:
    r = shop.get("/orders/42", headers={"X-Request-ID": "abc123"})
    assert r.status_code == 404
    assert r.headers["X-Request-ID"] == "abc123"
    lines = [x for x in request_lines(shop_settings) if x.get("request_id") == "abc123"]
    assert len(lines) == 1
    line = lines[0]
    assert line["path"] == "/orders/{order_id}"  # route template, not the raw URL
    assert line["status"] == 404 and line["method"] == "GET"
    assert {"timestamp", "level", "service", "latency_ms"} <= line.keys()


# --- admin auth ------------------------------------------------------------------------
def test_admin_requires_token(shop: TestClient) -> None:
    assert shop.post("/admin/faults/slow_db").status_code == 401
    assert shop.post("/admin/faults/slow_db", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert shop.get("/internal/stats").status_code == 401


def test_unknown_fault(shop: TestClient) -> None:
    assert shop.post("/admin/faults/nope", headers=ADMIN).status_code == 404


def test_faults_blocked_in_production(shop_settings: ShopSettings) -> None:
    from shop.main import create_app

    prod = shop_settings.model_copy(update={"env": "production"})
    with TestClient(create_app(prod)) as client:
        assert client.post("/admin/faults/slow_db", headers=ADMIN).status_code == 403


# --- faults ----------------------------------------------------------------------------
def test_buggy_deploy_breaks_only_unpromoted_products(
    shop: TestClient, shop_settings: ShopSettings
) -> None:
    shop.post("/admin/faults/buggy_deploy", headers=ADMIN)
    assert shop.post("/orders", json={"product_id": 1, "quantity": 1}).status_code == 201
    broken = shop.post("/orders", json={"product_id": 2, "quantity": 1})
    assert broken.status_code == 500
    assert "Traceback" not in broken.text  # no stack trace leaks to clients
    errors = [x for x in request_lines(shop_settings) if x["status"] == 500]
    assert errors and errors[-1]["error_type"] == "KeyError"
    assert errors[-1]["path"] == "/orders"
    shop.delete("/admin/faults/buggy_deploy", headers=ADMIN)
    assert shop.post("/orders", json={"product_id": 2, "quantity": 1}).status_code == 201


def test_bad_config_fails_health_and_db_routes(shop: TestClient) -> None:
    shop.post("/admin/faults/bad_config", headers=ADMIN)
    assert shop.get("/health").status_code == 503
    assert shop.get("/products").status_code == 500
    shop.delete("/admin/faults/bad_config", headers=ADMIN)
    assert shop.get("/health").status_code == 200


def test_slow_db_adds_latency(shop: TestClient, shop_settings: ShopSettings) -> None:
    shop.post("/admin/faults/slow_db", headers=ADMIN, json={"params": {"delay_ms": 300}})
    shop.get("/products")
    assert request_lines(shop_settings)[-1]["latency_ms"] >= 300


def test_memory_leak_grows_and_clears(shop: TestClient) -> None:
    shop.post("/admin/faults/memory_leak", headers=ADMIN, json={"params": {"kb_per_request": 1024}})
    for _ in range(5):
        shop.get("/products")
    assert shop.get("/internal/stats", headers=ADMIN).json()["leak_mb"] >= 5
    shop.delete("/admin/faults/memory_leak", headers=ADMIN)
    assert shop.get("/internal/stats", headers=ADMIN).json()["leak_mb"] == 0


def test_stock_exhaustion_is_409_not_500(shop: TestClient) -> None:
    shop.post("/admin/faults/stock_exhaustion", headers=ADMIN, json={"params": {"product_id": 2}})
    r = shop.post("/orders", json={"product_id": 2, "quantity": 1})
    assert r.status_code == 409
    shop.delete("/admin/faults/stock_exhaustion", headers=ADMIN)
    stock = {p["id"]: p["stock"] for p in shop.get("/products").json()}
    assert stock[2] == 300  # restored
