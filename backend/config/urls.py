"""Root URL configuration."""
from django.contrib import admin
from django.urls import URLPattern, URLResolver, path

from config.api import api

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
