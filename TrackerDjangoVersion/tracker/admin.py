from django.contrib import admin

from .models import Category, Task, Transaction


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'color', 'created_at')
    search_fields = ('name',)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'due_date', 'status', 'selected', 'created_at')
    list_filter = ('status', 'due_date')
    search_fields = ('title',)
    ordering = ('-due_date',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('description', 'date', 'type', 'value', 'category', 'selected', 'created_at')
    list_filter = ('type', 'category', 'date')
    search_fields = ('description',)
    ordering = ('-date',)

# Register your models here.
