from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from .models import Workspace, WorkspaceMembership


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
        "/workspaces/select/",
        "/workspaces/create/",
        "/static/",
        "/media/",
    )

    def process_request(self, request):
        request.workspace = None
        request.workspace_role = None
        if not request.user.is_authenticated:
            return None

        path = request.path or ""
        if any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIXES):
            return None

        # Superuser pode operar globalmente ou "impersonar" um workspace via slug
        if request.user.is_superuser:
            slug = request.GET.get("workspace") or request.session.get("workspace_slug")
            if slug:
                ws = Workspace.objects.filter(slug=slug, is_active=True).first()
                if ws:
                    request.workspace = ws
                    request.session["workspace_slug"] = ws.slug
            else:
                # superuser: se tiver membership, usa a primeira como default para nao cair em visao global sempre
                membership = (
                    WorkspaceMembership.objects.select_related("workspace")
                    .filter(user=request.user, workspace__is_active=True)
                    .order_by("id")
                    .first()
                )
                if membership:
                    request.workspace = membership.workspace
                    request.workspace_role = membership.role
                    request.session["workspace_slug"] = membership.workspace.slug
            return None

        memberships = (
            WorkspaceMembership.objects.select_related("workspace")
            .filter(user=request.user, workspace__is_active=True)
            .order_by("id")
        )
        if not memberships.exists():
            messages.error(request, "Você precisa escolher ou criar um workspace para continuar.")
            return redirect("tracker:workspace_select")

        slug = request.GET.get("workspace") or request.session.get("workspace_slug")
        membership = memberships.filter(workspace__slug=slug).first() if slug else None
        if not membership:
            membership = memberships.first()

        request.workspace = membership.workspace
        request.workspace_role = membership.role
        request.session["workspace_slug"] = membership.workspace.slug
        return None






