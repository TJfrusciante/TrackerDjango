from django.contrib import admin

from .models import MpSubscription, MpWebhookEvent


@admin.register(MpSubscription)
class MpSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan_cycle', 'status', 'preapproval_id', 'next_payment_at', 'grace_until', 'updated_at')
    list_filter = ('plan_cycle', 'status')
    search_fields = ('user__username', 'preapproval_id', 'payer_email')


@admin.register(MpWebhookEvent)
class MpWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('topic', 'mp_id', 'status', 'created_at')
    list_filter = ('topic', 'status')
    search_fields = ('mp_id',)

# Register your models here.
