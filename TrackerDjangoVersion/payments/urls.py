from django.urls import path

from . import views

app_name = 'payments'

urlpatterns = [
    path('assinar/', views.subscription_start, name='subscription_start'),
    path('sucesso/', views.subscription_success, name='subscription_success'),
    path('falha/', views.subscription_failure, name='subscription_failure'),
    path('pendente/', views.subscription_pending, name='subscription_pending'),
    path('webhook/', views.mp_webhook, name='mp_webhook'),
    path('webhook/<int:pk>/reprocess/', views.mp_webhook_reprocess, name='mp_webhook_reprocess'),
]
