"""Local model-health curation for Hermes picker surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


KEEP_STATUSES = {"working", "rate_limited"}


def model_health_cache_path() -> Path:
    """Location for local model-probe results used to curate picker rows."""
    fallback = Path.home() / ".hermes" / "cache" / "omniroute_model_health.json"
    try:
        from hermes_cli.config import get_hermes_home

        profile_path = get_hermes_home() / "cache" / "omniroute_model_health.json"
        return profile_path if profile_path.exists() else fallback
    except Exception:
        return fallback


def load_model_health() -> dict[str, dict]:
    path = model_health_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def lookup_model_health(health: dict[str, dict], provider: str, model: str) -> Optional[dict]:
    """Find health metadata by exact OmniRoute id or provider/model pair."""
    exact = health.get(model)
    if isinstance(exact, dict):
        return exact
    combined = health.get(f"{provider}/{model}") if provider else None
    return combined if isinstance(combined, dict) else None


def health_label(provider: dict, model: str) -> str:
    meta = (provider.get("model_health") or {}).get(model)
    if isinstance(meta, dict) and meta.get("status") == "rate_limited":
        return str(meta.get("label") or "rate limited")
    return ""


def label_model(provider: dict, model: str) -> str:
    label = health_label(provider, model)
    return f"{model} [{label}]" if label else model


def apply_model_health(rows: list[dict]) -> None:
    """Filter rows using local probe results and attach rate-limit metadata.

    The health cache is opt-in at provider granularity. If no model in a
    provider row has matching probe data, that provider is left untouched. Once
    a provider has any probe data, the row is narrowed to models whose status is
    ``working`` or ``rate_limited``; rate-limited models remain selectable with
    ``model_health`` metadata so UIs can badge them.
    """
    health = load_model_health()
    if not health:
        return

    providers_with_probe_data = {
        key.split("/", 1)[0]
        for key in health
        if "/" in key
    }

    for row in rows:
        provider = str(row.get("slug") or "")
        models = list(row.get("models") or [])
        if not models:
            continue

        saw_probe_data = False
        provider_is_probed = provider in providers_with_probe_data
        kept: list[str] = []
        row_health: dict[str, dict] = {}

        for model in models:
            model = str(model)
            meta = lookup_model_health(health, provider, model)
            if not meta:
                if not provider_is_probed:
                    kept.append(model)
                continue

            saw_probe_data = True
            status = str(meta.get("status") or "").strip().lower()
            if status not in KEEP_STATUSES:
                continue

            kept.append(model)
            if status == "rate_limited":
                row_health[model] = {
                    "status": "rate_limited",
                    "label": "rate limited",
                    "http_status": meta.get("http_status"),
                    "message": str(meta.get("message") or "")[:240],
                }

        if not saw_probe_data and not provider_is_probed:
            continue

        row["models"] = kept
        row["total_models"] = len(kept)
        if row_health:
            row["model_health"] = row_health
        else:
            row.pop("model_health", None)
