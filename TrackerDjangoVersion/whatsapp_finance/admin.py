from django.contrib import admin

from .models import CategoryPreference, ParsedTransaction, WhatsAppMessage, WhatsAppProfile


@admin.register(WhatsAppProfile)
class WhatsAppProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "phone_number", "workspace", "is_active", "last_seen_at")
    list_filter = ("is_active", "workspace")
    search_fields = ("phone_number", "user__username", "user__email")


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "direction", "from_number", "to_number", "created_at")
    list_filter = ("direction", "created_at")
    search_fields = ("from_number", "to_number", "body", "message_sid")


@admin.register(ParsedTransaction)
class ParsedTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "workspace", "amount", "type", "status", "date", "requires_confirmation")
    list_filter = ("status", "type", "workspace")
    search_fields = ("description", "category_label")


@admin.register(CategoryPreference)
class CategoryPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "keyword", "category", "usage_count", "updated_at")
    list_filter = ("workspace",)
    search_fields = ("keyword", "category__name", "user__username")
