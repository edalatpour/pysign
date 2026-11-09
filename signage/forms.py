from django import forms

from .models import Device, Slide


class ClaimForm(forms.Form):
    code = forms.CharField(max_length=11, label="Pairing code")
    name = forms.CharField(max_length=100, label="Display name")

    def clean_code(self):
        return self.cleaned_data["code"].upper().replace("-", "").replace(" ", "")


class DeviceForm(forms.ModelForm):
    class Meta:
        model = Device
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
