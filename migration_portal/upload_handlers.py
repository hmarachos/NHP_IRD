from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from app.services.storage import MAX_UPLOAD_FILE_SIZE


class PerFileSizeUploadHandler(FileUploadHandler):
    """Stop multipart parsing as soon as any individual file exceeds the limit."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file_bytes_received = 0

    def receive_data_chunk(self, raw_data, start):
        self.file_bytes_received += len(raw_data)
        if self.file_bytes_received > MAX_UPLOAD_FILE_SIZE:
            self.request.upload_file_too_large = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
