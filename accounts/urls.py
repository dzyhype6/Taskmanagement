from django.urls import path
from .views import register

urlpatterns = [
    path('register/', register, name='register'),

    # Backward/UX-friendly aliases for the “create new user” page.
    path('create/', register, name='create_user'),
    path('create-user/', register, name='create_user_alias'),
]
