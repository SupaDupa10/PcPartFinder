"""Persistenz der vom Benutzer editierbaren Laufzeit-Einstellungen.

Bootstrap-Konfiguration und Geheimnisse bleiben in der Umgebung (``.env``).
Was die Oberfläche verändern darf, liegt hier in SQLite.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RuntimeSettings, Settings, settings_to_runtime
from ..models import AppSetting

logger = logging.getLogger(__name__)

SETTINGS_KEY = "runtime_settings"
SETTINGS_VERSION = 1


def load_runtime_settings(session: Session, settings: Settings) -> RuntimeSettings:
    """Laedt gespeicherte Einstellungen; fehlende Werte kommen aus der Umgebung."""
    row = session.get(AppSetting, SETTINGS_KEY)
    defaults = settings_to_runtime(settings)
    if row is None or not isinstance(row.value, dict):
        return defaults

    payload = dict(row.value)
    payload.pop("version", None)
    try:
        # Fehlende Felder werden aus den Defaults ergänzt.
        merged = {**defaults.model_dump(), **payload}
        return RuntimeSettings.model_validate(merged)
    except Exception as exc:
        logger.warning(
            "Gespeicherte Einstellungen sind ungültig (%s) - es gelten die Defaults.", exc
        )
        return defaults


def save_runtime_settings(session: Session, runtime: RuntimeSettings) -> RuntimeSettings:
    payload = runtime.model_dump()
    payload["version"] = SETTINGS_VERSION
    row = session.get(AppSetting, SETTINGS_KEY)
    if row is None:
        row = AppSetting(key=SETTINGS_KEY, value=payload)
        session.add(row)
    else:
        row.value = payload
        row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return runtime


def reset_runtime_settings(session: Session, settings: Settings) -> RuntimeSettings:
    row = session.get(AppSetting, SETTINGS_KEY)
    if row is not None:
        session.delete(row)
        session.commit()
    return settings_to_runtime(settings)
