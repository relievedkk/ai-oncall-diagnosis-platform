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


def test_service_catalog_defaults_missing_remediation_to_disabled(tmp_path):
    catalog_path = tmp_path / "services.json"
    catalog_path.write_text(
        json.dumps({"services": {"orders": {"prometheus_job": "orders"}}}),
        encoding="utf-8",
    )

    catalog = ServiceCatalog(str(catalog_path))

    assert catalog.remediation_policy("orders") == {
        "enabled": False,
        "allowed_actions": [],
    }
    assert catalog.remediation_policy("missing") == {
        "enabled": False,
        "allowed_actions": [],
    }


def test_service_catalog_returns_explicit_remediation_policy(tmp_path):
    remediation = {
        "enabled": True,
        "stateless": True,
        "allowed_actions": [
            "restart_instance",
            "scale_out",
            "rollback_revision",
        ],
        "max_replicas": 10,
        "cooldown_seconds": 900,
        "last_known_good_revisions": ["2026-10-13.1"],
    }
    catalog_path = tmp_path / "services.json"
    catalog_path.write_text(
        json.dumps({"services": {"orders": {"remediation": remediation}}}),
        encoding="utf-8",
    )

    catalog = ServiceCatalog(str(catalog_path))

    assert catalog.remediation_policy("orders") == remediation


def test_service_catalog_fails_closed_for_malformed_remediation(tmp_path):
    catalog_path = tmp_path / "services.json"
    catalog_path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {
                        "remediation": {
                            "enabled": True,
                            "allowed_actions": "restart_instance",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    catalog = ServiceCatalog(str(catalog_path))

    assert catalog.remediation_policy("orders") == {
        "enabled": False,
        "allowed_actions": [],
    }
