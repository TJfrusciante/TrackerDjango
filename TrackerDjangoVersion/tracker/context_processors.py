from __future__ import annotations

from .models import WorkspaceMembership, Workspace
from django.core.files.storage import default_storage


def workspace_context(request):
    """
    Injeta o workspace atual e a lista de workspaces do usuÇ­rio autenticado.
    Superuser enxerga todos os workspaces ativos e pode operar em modo global (None).
    """
    current = getattr(request, "workspace", None)
    user = getattr(request, "user", None)

    available = []
    if user and user.is_authenticated:
        if user.is_superuser:
            available = list(Workspace.objects.filter(is_active=True).order_by("name"))
        else:
            available = [
                m.workspace
                for m in WorkspaceMembership.objects.select_related("workspace")
                .filter(user=user, workspace__is_active=True)
                .order_by("workspace__name")
            ]

    return {
        "current_workspace": current,
        "available_workspaces": available,
        "current_workspace_role": getattr(request, "workspace_role", None),
        "user_avatar_url": _avatar_url(user),
    }


def _avatar_url(user):
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    profile = getattr(user, "profile", None)
    if profile and profile.avatar:
        try:
            return profile.avatar.url
        except Exception:
            return ""
    return ""
