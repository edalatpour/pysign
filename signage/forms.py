from django import forms

from .assets import validate_upload
from .models import Asset, Device, Playlist, Slide


class AssetForm(forms.Form):
    name = forms.CharField(max_length=100)
    file = forms.FileField(
        help_text="JPEG, PNG, WebP, MP4, WebM, PDF, Word (.doc/.docx) or PowerPoint (.ppt/.pptx). "
        "Documents become page images (up to 50 pages); HTML and SVG are not supported.",
        widget=forms.ClearableFileInput(attrs={"accept": ".jpg,.jpeg,.png,.webp,.mp4,.webm,.pdf,.doc,.docx,.ppt,.pptx"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        self.content_type, self.size, self.sha256, self.page_count = validate_upload(upload)
        return upload


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
    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = owner.assets.all() if owner is not None else Asset.objects.none()
        self.fields["asset"].empty_label = "Use an external URL"

    def clean(self):
        data = super().clean()
        if bool(data.get("url")) == bool(data.get("asset")):
            raise forms.ValidationError("Choose either an external URL or an asset, not both.")
        if data.get("asset") and data.get("show_qr") and not data.get("qr_url"):
            self.add_error("qr_url", "Provide a public QR code URL for private assets, or turn off Show QR code.")
        return data

    class Meta:
        model = Slide
        fields = ["title", "url", "asset", "show_qr", "qr_url", "duration", "position"]
        labels = {
            "show_qr": "Show QR code",
            "qr_url": "QR code URL",
            "duration": "Duration (seconds)", "position": "Order (lowest first)",
        }
        widgets = {
            "url": forms.URLInput(attrs={"placeholder": "https://example.com/scoreboard"}),
            "qr_url": forms.URLInput(attrs={"placeholder": "Leave blank to use the page URL"}),
        }
        help_texts = {
            "url": "Enter a URL or select an uploaded asset below.",
            "asset": "Reuse an image, video or document from your private asset library.",
            "qr_url": "Optional. Leave blank to use the current page URL, including redirects. "
            "Private assets require an explicit public QR code URL if Show QR code is enabled.",
            "duration": "For documents, this is the duration of each page. Videos loop for this duration.",
        }
