from django.contrib.auth.views import LogoutView
from django.urls import path

from . import api, auth, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("accounts/login/", views.signin, name="login"),
    path("accounts/microsoft/", auth.begin, name="entra-login"),
    path("accounts/callback/", auth.callback, name="entra-callback"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("devices/claim/", views.claim, name="claim"),
    path("devices/<uuid:device_id>/", views.device_detail, name="device"),
    path("devices/<uuid:device_id>/remove/", views.remove_device, name="remove-device"),
    path("devices/<uuid:device_id>/slides/add/", views.edit_slide, name="add-slide"),
    path("devices/<uuid:device_id>/slides/<int:slide_id>/", views.edit_slide, name="edit-slide"),
    path("devices/<uuid:device_id>/slides/<int:slide_id>/remove/", views.remove_slide, name="remove-slide"),
    path("api/devices/register/", api.register, name="api-register"),
    path("api/device/config/", api.config, name="api-config"),
    path("api/device/pairing/", api.pairing, name="api-pairing"),
]
