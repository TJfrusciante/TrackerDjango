from django.urls import path

from . import views

app_name = 'assistant'

urlpatterns = [
    path('', views.chat, name='chat'),
    path('embed/', views.chat_embed, name='chat_embed'),
]
