"""User file attachments: two-phase upload into REPORTS_DIR and model input conversion."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse, urlunparse

from chatkit.store import AttachmentStore
from chatkit.types import (
    Attachment,
    AttachmentCreateParams,
    AttachmentUploadDescriptor,
    FileAttachment,
    ImageAttachment,
)
from openai.types.responses import ResponseInputContentParam, ResponseInputTextParam
from openai.types.responses.response_input_file_param import ResponseInputFileParam
from openai.types.responses.response_input_image_param import ResponseInputImageParam

from .traccar import _get_session_id, _get_traccar_url, fleetmap_url

logger = logging.getLogger(__name__)

REPORTS_DIR: Final[Path] = Path(__file__).parent.parent / "reports"

# Uploads are served back from GET /chatkit/{filename}, so only accept types
# that are safe to serve from our own origin (no HTML/SVG).
ALLOWED_MIME_TYPES: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/json": ".json",
    # Office documents: passed to the model as input_file. OpenAI extracts the
    # text of documents and runs a spreadsheet-specific parse on spreadsheets.
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/rtf": ".rtf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
OFFICE_EXTENSIONS: Final[tuple[str, ...]] = (
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".pptx",
    ".xls",
    ".xlsx",
)
# Sent to the model as plain text rather than as a file.
TEXT_MIME_TYPES: Final[frozenset[str]] = frozenset({"text/csv", "text/plain", "application/json"})
MAX_ATTACHMENT_BYTES: Final[int] = 20 * 1024 * 1024
# Text files are inlined into the prompt; cap them so a large CSV can't blow the context.
MAX_TEXT_CHARS: Final[int] = 100_000


def public_base_url(session: str | None, traccar_url: str) -> str:
    """Public origin that routes back to this backend.

    The session is embedded as a subdomain so the server can recover it from
    the hostname on later requests, e.g. https://{session}.rastreon.net
    """
    base_domain = (
        "https://i8ttracker.com.br" if traccar_url == fleetmap_url else "https://rastreon.net"
    )
    parsed = urlparse(base_domain)
    if session:
        parsed = parsed._replace(netloc=f"{session}.{parsed.netloc}")
    return urlunparse(parsed)


def attachment_path(attachment_id: str, mime_type: str) -> Path:
    return REPORTS_DIR / f"{attachment_id}{ALLOWED_MIME_TYPES.get(mime_type, '')}"


class TraccarAttachmentStore(AttachmentStore[dict[str, Any]]):
    """Two-phase uploads: the client PUTs the raw bytes to /chatkit/attachments/{id}."""

    async def create_attachment(
        self, input: AttachmentCreateParams, context: dict[str, Any]
    ) -> Attachment:
        mime_type = input.mime_type.lower()
        # Browsers on Windows with Excel installed report .csv as application/vnd.ms-excel.
        if mime_type == "application/vnd.ms-excel" and input.name.lower().endswith(".csv"):
            mime_type = "text/csv"
        if mime_type not in ALLOWED_MIME_TYPES:
            raise ValueError(f"Unsupported attachment type: {input.mime_type}")
        if input.size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment too large: {input.size} bytes")

        request = context.get("request")
        base_url = public_base_url(_get_session_id(request), _get_traccar_url(request))
        attachment_id = self.generate_attachment_id(mime_type, context)
        upload = AttachmentUploadDescriptor.model_validate(
            {
                "url": f"{base_url}/chatkit/attachments/{attachment_id}",
                "method": "PUT",
                "headers": {"Content-Type": mime_type},
            }
        )
        if mime_type.startswith("image/"):
            filename = attachment_path(attachment_id, mime_type).name
            return ImageAttachment.model_validate(
                {
                    "id": attachment_id,
                    "name": input.name,
                    "mime_type": mime_type,
                    "upload_descriptor": upload,
                    "preview_url": f"{base_url}/chatkit/{filename}",
                }
            )
        return FileAttachment(
            id=attachment_id, name=input.name, mime_type=mime_type, upload_descriptor=upload
        )

    async def delete_attachment(self, attachment_id: str, context: dict[str, Any]) -> None:
        for path in REPORTS_DIR.glob(f"{attachment_id}.*"):
            path.unlink(missing_ok=True)


def attachment_to_input(attachment: Attachment) -> ResponseInputContentParam:
    """Inline an uploaded attachment's bytes into the model input."""
    path = attachment_path(attachment.id, attachment.mime_type)
    if not path.exists():
        logger.warning("Attachment file missing: %s", path)
        return ResponseInputTextParam(
            type="input_text", text=f"(Attachment '{attachment.name}' is no longer available.)"
        )

    data = path.read_bytes()
    if isinstance(attachment, ImageAttachment):
        encoded = base64.b64encode(data).decode()
        return ResponseInputImageParam(
            type="input_image",
            image_url=f"data:{attachment.mime_type};base64,{encoded}",
            detail="auto",
        )
    if attachment.mime_type not in TEXT_MIME_TYPES:
        encoded = base64.b64encode(data).decode()
        return ResponseInputFileParam(
            type="input_file",
            filename=attachment.name,
            file_data=f"data:{attachment.mime_type};base64,{encoded}",
        )

    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n...(truncated)"
    return ResponseInputTextParam(
        type="input_text", text=f"Attached file '{attachment.name}':\n{text}"
    )
