from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.metrics import HTTP_REQUESTS, prometheus_http_middleware


def test_http_middleware_records_route_and_status():
    app = FastAPI()
    app.middleware("http")(prometheus_http_middleware)

    @app.get("/items/{item_id}")
    async def read_item(item_id: str):
        return {"item_id": item_id}

    metric = HTTP_REQUESTS.labels(method="GET", route="/items/{item_id}", status="200")
    before = metric._value.get()
    response = TestClient(app).get("/items/123")

    assert response.status_code == 200
    assert metric._value.get() == before + 1
