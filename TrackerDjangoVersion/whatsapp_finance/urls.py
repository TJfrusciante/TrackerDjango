from django.urls import path

from . import views

app_name = "whatsapp_finance"

urlpatterns = [
    path("webhook/", views.webhook, name="webhook"),
]
