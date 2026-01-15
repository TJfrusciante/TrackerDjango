from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import AiUsage


DEFAULT_LIMITS = {
    "starter": 20000,
    "pro": 80000,
    "team": 200000,
}


def _plan_limit(plan: str) -> int:
    limits = getattr(settings, "AI_PLAN_TOKEN_LIMITS", DEFAULT_LIMITS)
    if not isinstance(limits, dict):
        limits = DEFAULT_LIMITS
    return int(limits.get(plan, limits.get("starter", 0)) or 0)


def get_ai_quota(user):
    window_days = int(getattr(settings, "AI_USAGE_WINDOW_DAYS", 30))
    profile = getattr(user, "profile", None)
    plan = getattr(profile, "plan", "starter")

    if user.is_superuser:
        return {
            "allowed": True,
            "limit": 0,
            "used": 0,
            "remaining": None,
            "plan": plan,
            "window_days": window_days,
            "reason": "superuser",
        }

    if profile and profile.is_guest:
        return {
            "allowed": False,
            "limit": 0,
            "used": 0,
            "remaining": 0,
            "plan": plan,
            "window_days": window_days,
            "reason": "guest",
        }

    limit = _plan_limit(plan)
    if limit <= 0:
        return {
            "allowed": True,
            "limit": 0,
            "used": 0,
            "remaining": None,
            "plan": plan,
            "window_days": window_days,
            "reason": "unlimited",
        }

    since = timezone.now() - timedelta(days=window_days)
    usage_qs = AiUsage.objects.filter(user=user, created_at__gte=since)
    used = usage_qs.aggregate(total=Sum("total_tokens")).get("total") or 0
    remaining = max(limit - used, 0)
    next_reset = None
    if used >= limit:
        oldest = usage_qs.order_by("created_at").first()
        if oldest:
            next_reset = oldest.created_at + timedelta(days=window_days)
    return {
        "allowed": used < limit,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "plan": plan,
        "window_days": window_days,
        "reason": "limit",
        "next_reset": next_reset,
    }
