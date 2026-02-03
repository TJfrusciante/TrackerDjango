from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from .models import WorkspaceMembership, Workspace, Notification
from django.core.files.storage import default_storage


def workspace_context(request):
    """
    Injeta o workspace atual e a lista de workspaces do usuario autenticado.
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

    undo_tx = _get_undo_payload(request, 'undo_tx')
    undo_task = _get_undo_payload(request, 'undo_task')
    undo_tx_bulk = _get_undo_payload(request, 'undo_tx_bulk')
    undo_task_bulk = _get_undo_payload(request, 'undo_task_bulk')
    grace_active = bool(getattr(request, "subscription_grace", False))
    grace_until = getattr(request, "subscription_grace_until", None)
    grace_remaining = getattr(request, "subscription_grace_remaining", None)
    avatar_missing = _avatar_missing(user)
    return {
        "current_workspace": current,
        "available_workspaces": available,
        "current_workspace_role": getattr(request, "workspace_role", None),
        "user_avatar_url": _avatar_url(user),
        "user_avatar_missing": avatar_missing,
        "user_is_guest": _is_guest(user),
        "notifications_unread": _notification_unread_count(user),
        "app_version": getattr(settings, "APP_VERSION", ""),
        "social_facebook_url": getattr(settings, "SOCIAL_FACEBOOK_URL", ""),
        "social_instagram_url": getattr(settings, "SOCIAL_INSTAGRAM_URL", ""),
        "sidebar_hover_expand": _sidebar_hover_expand(user),
        "undo_tx": undo_tx,
        "undo_task": undo_task,
        "undo_tx_bulk": undo_tx_bulk,
        "undo_task_bulk": undo_task_bulk,
        "subscription_grace_active": grace_active,
        "subscription_grace_until": grace_until,
        "subscription_grace_remaining": grace_remaining,
    }


def _avatar_url(user):
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    profile = getattr(user, "profile", None)
    if profile and profile.avatar:
        try:
            if default_storage.exists(profile.avatar.name):
                return profile.avatar.url
        except Exception:
            return ""
    return ""


def _avatar_missing(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    profile = getattr(user, "profile", None)
    if profile and profile.avatar:
        try:
            return not default_storage.exists(profile.avatar.name)
        except Exception:
            return True
    return False


def _is_guest(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    profile = getattr(user, "profile", None)
    return bool(profile and profile.is_guest)


def _notification_unread_count(user):
    if not user or not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def _get_undo_payload(request, key):
    session = getattr(request, 'session', None)
    if not session:
        return None
    payload = session.get(key)
    expires = session.get(f"{key}_expires")
    if not payload:
        return None
    if expires:
        try:
            expires_at = timezone.datetime.fromisoformat(expires)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
            if timezone.now() > expires_at:
                session.pop(key, None)
                session.pop(f"{key}_expires", None)
                return None
        except Exception:
            pass
    return payload


def _sidebar_hover_expand(user):
    if not user or not getattr(user, "is_authenticated", False):
        return True
    profile = getattr(user, "profile", None)
    if profile is None:
        return True
    return bool(getattr(profile, "sidebar_hover_expand", True))

