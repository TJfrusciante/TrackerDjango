from django.contrib import admin

from .models import (
    Category,
    Task,
    Transaction,
    Workspace,
    SubscriptionInvite,
    MetricEvent,
)


class WorkspaceScopedAdmin(admin.ModelAdmin):
    """
    Restringe itens para staff nao-superuser ao que pertence aos seus workspaces.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(workspace__owner=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'workspace' and not request.user.is_superuser:
            kwargs['queryset'] = Workspace.objects.filter(owner=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Category)
class CategoryAdmin(WorkspaceScopedAdmin):
    list_display = ('name', 'workspace', 'color', 'created_at')
    list_filter = ('workspace', 'created_at')
    search_fields = ('name',)


@admin.register(Task)
class TaskAdmin(WorkspaceScopedAdmin):
    list_display = ('title', 'workspace', 'due_date', 'status', 'selected', 'created_at')
    list_filter = ('workspace', 'status', 'due_date')
    search_fields = ('title',)
    ordering = ('-due_date',)


@admin.register(Transaction)
class TransactionAdmin(WorkspaceScopedAdmin):
    list_display = ('description', 'workspace', 'date', 'type', 'value', 'category', 'selected', 'created_at')
    list_filter = ('workspace', 'type', 'category', 'date')
    search_fields = ('description',)
    ordering = ('-date',)


@admin.register(SubscriptionInvite)
class SubscriptionInviteAdmin(admin.ModelAdmin):
    list_display = ('code', 'plan_cycle', 'used_count', 'max_uses', 'is_active', 'expires_at', 'created_at')
    list_filter = ('plan_cycle', 'is_active')
    search_fields = ('code',)


@admin.register(MetricEvent)
class MetricEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'user', 'workspace', 'created_at')
    list_filter = ('event_type', 'created_at')
    search_fields = ('user__username', 'workspace__name')

