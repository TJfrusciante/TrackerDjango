from __future__ import annotations

import datetime

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from .models import UserProfile, Workspace, WorkspaceMembership
from .notifications import notify_subscription_grace


class WorkspaceMiddleware(MiddlewareMixin):
    """
    Define request.workspace (tenant atual) e protege rotas para quem nao tem vinculo.
    - Superuser: pode navegar em modo global (workspace=None) ou escolher um workspace por slug.
    - Usuario comum: precisa ter membership; valida slug da sessao/querystring e redireciona para selecao se nao tiver.
    """

    PUBLIC_PREFIXES = (
        "/admin/",
        "/login/",
        "/logout/",
        "/register/",
        "/help/",
        "/workspaces/invites/",
        "/workspaces/request/",
        "/workspaces/select/",
        "/workspaces/create/",
        "/pagamentos/",
        "/static/",
        "/media/",
        "/password-reset/",
        "/termos/",
        "/privacidade/",
        "/contato/",
        "/perfil/",
    )

    PAYMENT_PREFIXES = (
        "/pagamentos/",
        "/logout/",
        "/login/",
        "/register/",
        "/help/",
        "/workspaces/invites/",
        "/workspaces/request/",
        "/static/",
        "/media/",
        "/password-reset/",
        "/termos/",
        "/privacidade/",
        "/contato/",
        "/perfil/",
    )

    def process_request(self, request):
        request.workspace = None
        request.workspace_role = None
        request.subscription_grace = False
        request.subscription_grace_until = None
        request.subscription_grace_remaining = None
        if not request.user.is_authenticated:
            return None

        path = request.path or ""

        if not request.user.is_superuser:
            profile = getattr(request.user, "profile", None)
            if not profile:
                profile, _ = UserProfile.objects.get_or_create(user=request.user)
            trial_active = profile.trial_active() if profile else False
            today = timezone.localdate()
            expires = profile.subscription_expires if profile else None
            grace_days = int(getattr(settings, "SUBSCRIPTION_GRACE_DAYS", 7))
            grace_until = expires + datetime.timedelta(days=grace_days) if expires else None
            if profile and not profile.is_guest:
                if expires and today > expires:
                    if grace_until and today <= grace_until:
                        request.subscription_grace = True
                        request.subscription_grace_until = grace_until
                        request.subscription_grace_remaining = (grace_until - today).days
                        if not profile.grace_notified_at or profile.grace_notified_at.date() < expires:
                            notify_subscription_grace(request.user, grace_until)
                            profile.grace_notified_at = timezone.now()
                            profile.save(update_fields=["grace_notified_at"])
                    else:
                        if not any(path.startswith(prefix) for prefix in self.PAYMENT_PREFIXES):
                            messages.info(request, "Assinatura expirada. Renove para continuar usando o iTracker.")
                            return redirect("payments:subscription_start")
                elif not trial_active and not expires and not profile.payment_confirmed:
                    if not any(path.startswith(prefix) for prefix in self.PAYMENT_PREFIXES):
                        messages.info(request, "Finalize a assinatura para liberar o acesso ao sistema.")
                        return redirect("payments:subscription_start")

        if any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIXES):
            return None

        # Superuser pode operar globalmente ou "impersonar" um workspace via slug
        if request.user.is_superuser:
            if request.session.get("workspace_global"):
                return None
            slug = request.GET.get("workspace") or request.session.get("workspace_slug")
            if slug:
                ws = Workspace.objects.filter(slug=slug, is_active=True).first()
                if ws:
                    request.workspace = ws
                    request.session["workspace_slug"] = ws.slug
                    request.session["workspace_global"] = False
            return None

        memberships = (
            WorkspaceMembership.objects.select_related("workspace")
            .filter(user=request.user, workspace__is_active=True)
            .order_by("id")
        )
        if not memberships.exists():
            messages.error(
                request,
                "Crie um workspace para poder adicionar dados ao sistema. Um workspace é seu espaço no sistema.",
            )
            return redirect("tracker:workspace_select")

        slug = request.GET.get("workspace") or request.session.get("workspace_slug")
        membership = memberships.filter(workspace__slug=slug).first() if slug else None
        if not membership:
            membership = memberships.first()

        request.workspace = membership.workspace
        request.workspace_role = membership.role
        request.session["workspace_slug"] = membership.workspace.slug
        return None






