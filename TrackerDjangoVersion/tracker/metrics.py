from __future__ import annotations

from typing import Any

from .models import MetricEvent


def record_metric(event_type: str, user=None, workspace=None, metadata: dict[str, Any] | None = None) -> None:
    try:
        MetricEvent.objects.create(
            event_type=event_type,
            user=user,
            workspace=workspace,
            metadata=metadata or {},
        )
    except Exception:
        # Nao interromper fluxos criticos por conta de métricas.
        return
