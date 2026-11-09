from django import forms

from .models import Device, Playlist, Slide


class ClaimForm(forms.Form):
    code = forms.CharField(max_length=11, label="Pairing code")
    name = forms.CharField(max_length=100, label="Display name")

    def clean_code(self):
        return self.cleaned_data["code"].upper().replace("-", "").replace(" ", "")


class DeviceForm(forms.ModelForm):
    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["playlist"].queryset = owner.playlists.all()
        self.fields["playlist"].empty_label = "No playlist (ready screen)"

    class Meta:
        model = Device
        fields = ["name", "playlist"]


class PlaylistForm(forms.ModelForm):
    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.owner = owner

    def clean_name(self):
        name = self.cleaned_data["name"]
        if Playlist.objects.filter(owner=self.instance.owner, name=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("You already have a playlist with this name.")
        return name

    class Meta:
        model = Playlist
        fields = ["name"]


class SlideForm(forms.ModelForm):
    class Meta:
        model = Slide
        fields = ["title", "url", "show_qr", "qr_url", "duration", "position"]
        labels = {
            "show_qr": "Show QR code",
            "qr_url": "QR code URL",
            "duration": "Duration (seconds)", "position": "Order (lowest first)",
        }
        widgets = {
            "url": forms.URLInput(attrs={"placeholder": "https://example.com/scoreboard"}),
            "qr_url": forms.URLInput(attrs={"placeholder": "Leave blank to use the page URL"}),
        }
        help_texts = {"qr_url": "Optional. Leave blank to use the current page URL, including redirects."}
