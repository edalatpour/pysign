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
        fields = ["title", "url", "duration", "position"]
        labels = {"duration": "Duration (seconds)", "position": "Order (lowest first)"}
        widgets = {
            "url": forms.URLInput(attrs={"placeholder": "https://example.com/scoreboard"}),
        }
