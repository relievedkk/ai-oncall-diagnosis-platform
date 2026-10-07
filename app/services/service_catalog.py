"""Resolve alert labels to metrics, logs, and ownership configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config


class ServiceCatalog:
    def __init__(self, path: str | None = None):
        self.path = Path(path or config.service_catalog_path)
        self._services: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            services = payload.get("services", {})
            self._services = services if isinstance(services, dict) else {}
        except FileNotFoundError:
            logger.warning("Service catalog not found: {}", self.path)
            self._services = {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load service catalog {}: {}", self.path, exc)
            self._services = {}

    def resolve(self, labels: dict[str, str] | None) -> tuple[str, dict[str, Any]]:
        labels = labels or {}
        candidates = [
            labels.get("service", ""),
            labels.get("job", ""),
            labels.get("app", ""),
            labels.get("instance", ""),
        ]
        normalized = {candidate.casefold() for candidate in candidates if candidate}

        for name, entry in self._services.items():
            aliases = {name, str(entry.get("prometheus_job", ""))}
            aliases.update(str(alias) for alias in entry.get("aliases", []))
            if normalized.intersection(alias.casefold() for alias in aliases if alias):
                return name, dict(entry)

        fallback = next((candidate for candidate in candidates if candidate), "unknown")
        return fallback, {
            "display_name": fallback,
            "aliases": [],
            "prometheus_job": labels.get("job") or labels.get("service") or fallback,
            "metrics": {},
            "cls": {},
            "owner": "unknown",
        }

    def resolve_name(self, service_name: str) -> tuple[str, dict[str, Any]]:
        return self.resolve({"service": service_name, "job": service_name})


service_catalog = ServiceCatalog()
