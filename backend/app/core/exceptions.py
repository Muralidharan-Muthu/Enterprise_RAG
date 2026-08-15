from fastapi import HTTPException, status


class DocumentNotFoundError(HTTPException):
    def __init__(self, document_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found",
        )


class JobNotFoundError(HTTPException):
    def __init__(self, job_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion job {job_id} not found",
        )


class FileTooLargeError(HTTPException):
    def __init__(self, size_mb: float, max_mb: int):
        super().__init__(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size {size_mb:.1f}MB exceeds maximum {max_mb}MB",
        )


class InvalidFileTypeError(HTTPException):
    def __init__(self, mime_type: str):
        super().__init__(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"File type '{mime_type}' not supported. "
                "Accepted formats: PDF, DOCX, PPTX, XLSX, HTML, Markdown."
            ),
        )


class ParsingError(Exception):
    def __init__(self, filename: str, reason: str):
        self.filename = filename
        self.reason = reason
        super().__init__(f"Failed to parse {filename}: {reason}")


class RouterError(Exception):
    def __init__(self, reason: str):
        super().__init__(f"Document routing failed: {reason}")


class EmbeddingError(Exception):
    def __init__(self, reason: str):
        super().__init__(f"Embedding generation failed: {reason}")


class StorageError(Exception):
    def __init__(self, store: str, reason: str):
        super().__init__(f"Failed to write to {store}: {reason}")
