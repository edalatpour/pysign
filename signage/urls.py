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
    path("devices/<uuid:device_id>/restart/", views.restart_player, name="restart-player"),
    path("devices/<uuid:device_id>/reset/", views.reset_player, name="reset-player"),
    path("devices/<uuid:device_id>/playlists/new/", views.create_playlist, name="create-device-playlist"),
    path("playlists/", views.playlists, name="playlists"),
    path("assets/", views.assets, name="assets"),
    path("assets/<uuid:asset_id>/content/", views.asset_content, name="asset-content"),
    path("assets/<uuid:asset_id>/preview/<int:page>/", views.asset_preview, name="asset-preview"),
    path("assets/<uuid:asset_id>/pages/<int:page>/", views.asset_preview, {"image": True}, name="asset-page"),
    path("assets/<uuid:asset_id>/remove/", views.remove_asset, name="remove-asset"),
    path("playlists/new/", views.create_playlist, name="create-playlist"),
    path("playlists/<int:playlist_id>/", views.playlist_detail, name="playlist"),
    path("playlists/<int:playlist_id>/remove/", views.remove_playlist, name="remove-playlist"),
    path("playlists/<int:playlist_id>/slides/add/", views.edit_slide, name="add-slide"),
    path("playlists/<int:playlist_id>/slides/<int:slide_id>/", views.edit_slide, name="edit-slide"),
    path("playlists/<int:playlist_id>/slides/<int:slide_id>/remove/", views.remove_slide, name="remove-slide"),
    path("api/devices/register/", api.register, name="api-register"),
    path("api/device/config/", api.config, name="api-config"),
    path("api/device/releases/<int:sequence>/", api.release_content, name="api-release"),
    path("api/device/assets/<uuid:asset_id>/", api.asset_content, name="api-asset"),
    path("api/device/restarts/<uuid:request_id>/completed/", api.restart_completed, name="api-restart-completed"),
    path("api/device/resets/<uuid:request_id>/completed/", api.reset_completed, name="api-reset-completed"),
    path("api/device/pairing/", api.pairing, name="api-pairing"),
]
