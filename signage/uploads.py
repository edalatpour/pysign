from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadhandler import FileUploadHandler


class LimitedAssetUploadHandler(FileUploadHandler):
    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > settings.ASSET_MAX_BYTES:
            raise RequestDataTooBig("The uploaded file exceeds the asset size limit.")
        return raw_data

    def file_complete(self, file_size):
        return None
