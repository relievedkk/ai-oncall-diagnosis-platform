import json

from app.services.service_catalog import ServiceCatalog


def test_service_catalog_resolves_alias_and_falls_back(tmp_path):
    catalog_path = tmp_path / "services.json"
    catalog_path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {
                        "aliases": ["order-api"],
                        "prometheus_job": "orders-job",
                        "metrics": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = ServiceCatalog(str(catalog_path))

    name, service = catalog.resolve({"service": "order-api"})
    assert name == "orders"
    assert service["prometheus_job"] == "orders-job"

    name, service = catalog.resolve({"job": "unknown-job"})
    assert name == "unknown-job"
    assert service["prometheus_job"] == "unknown-job"
