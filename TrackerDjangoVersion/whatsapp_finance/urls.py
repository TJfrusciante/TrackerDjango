from django.urls import path

from . import views

app_name = "whatsapp_finance"

urlpatterns = [
    path("agent/", views.agent, name="agent"),
    path("config/", views.config, name="config"),
    path("webhook/", views.webhook, name="webhook"),
]
