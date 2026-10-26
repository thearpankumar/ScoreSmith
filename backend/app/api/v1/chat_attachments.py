"""Files a user attaches to a chat message (`/api/v1/chat/attachments`; OpenRouter provider only).

Flow (same pattern as the evaluation uploads: the bytes go browser -> S3 directly, never through this API):
1. `POST /chat/attachments` `{filename, size, content_type}`: validates the type and size, creates the row
   (`pending`) and an S3 multipart upload for `chat/{user_id}/{attachment_id}.{ext}`, and returns the presigned
   part URLs.
2. The browser uploads the parts (frontend/lib/ai-upload.ts `uploadFileMultipart`).
3. `POST /chat/attachments/{id}/complete` `{upload_id, parts}`: completes the upload, checks the size and the first
   bytes, then extracts the text once (app/ai/attachments.py: PDF / DOCX in the extraction Lambda, spreadsheets and
   text here, images described by the builder model) and returns the row as `ready` (or `failed` with a reason).
4. The message is sent with `attachment_ids` (app/api/v1/chat.py binds them to it).
`DELETE /chat/attachments/{id}` removes a file that was not sent yet. Every route is owner-only (404 otherwise).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import attachments as att
from app.ai import providers
from app.ai.bedrock_client import BedrockClientProtocol
from app.config import get_settings
from app.db import get_db
from app.deps import get_aws_jobs, get_bedrock_client, get_current_user
from app.models.chat_attachment import ChatAttachment
from app.models.user import User
from app.pipeline import uploads as up
from app.pipeline.aws_jobs import AwsJobsError, AwsJobsProtocol, AwsNotConfiguredError
from app.ratelimit import rate_limit
from app.schemas.chat import ChatAttachmentRead
from app.schemas.evaluation_ai import CompletedPart, UploadPart

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/attachments", tags=["chat"])


class ChatAttachmentInit(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    content_type: str | None = Field(default=None, max_length=200)


class ChatAttachmentUpload(BaseModel):
    attachment: ChatAttachmentRead
    upload_id: str
    part_size: int
    parts: list[UploadPart]


class ChatAttachmentComplete(BaseModel):
    upload_id: str = Field(min_length=1, max_length=2048)
    parts: list[CompletedPart] = Field(min_length=1, max_length=10000)


async def _require_openrouter() -> None:
    if await providers.active_provider() != "openrouter":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="File attachments are available when the AI provider is OpenRouter."
        )


async def _owned(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> ChatAttachment:
    row = await db.get(ChatAttachment, attachment_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
    return row


def _aws_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AwsNotConfiguredError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="File storage is not configured.")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail="File storage is unavailable; try again.")


@router.post(
    "",
    response_model=ChatAttachmentUpload,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("uploads", lambda s: s.rate_limit_uploads, per_user=True))],
)
async def init_chat_attachment(
    payload: ChatAttachmentInit,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatAttachmentUpload:
    await _require_openrouter()
    settings = get_settings()
    ext = att.extension_of(payload.filename)
    kind = att.ATTACHMENT_EXTENSIONS.get(ext)
    if kind is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(att.ATTACHMENT_EXTENSIONS))}.",
        )
    if payload.size <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The file is empty.")
    if payload.size > settings.chat_attachment_max_bytes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Files can be at most {settings.chat_attachment_max_bytes // (1024 * 1024)} MB.",
        )
    attachment_id = uuid.uuid4()
    key = f"chat/{current_user.id}/{attachment_id}.{ext}"
    content_type = att.CONTENT_TYPES.get(ext) or payload.content_type
    part_size = up.part_size_for(payload.size, settings.upload_part_size)
    try:
        upload_id = await asyncio.to_thread(aws.create_multipart, key, content_type)
        parts = [
            UploadPart(
                part_number=n,
                url=await asyncio.to_thread(aws.presign_part, key, upload_id, n, settings.upload_url_ttl_seconds),
            )
            for n in range(1, up.part_count(payload.size, part_size) + 1)
        ]
    except (AwsJobsError, AwsNotConfiguredError) as exc:
        raise _aws_error(exc) from exc
    row = ChatAttachment(
        id=attachment_id, user_id=current_user.id, filename=payload.filename.strip()[:255], kind=kind,
        size_bytes=payload.size, s3_key=key, status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ChatAttachmentUpload(
        attachment=ChatAttachmentRead.model_validate(row), upload_id=upload_id, part_size=part_size, parts=parts
    )


async def _extract(
    row: ChatAttachment, aws: AwsJobsProtocol, client: BedrockClientProtocol
) -> att.Extracted:
    """The file's text, extracted once (see app/ai/attachments.py). Raises ValueError with a user-facing reason."""
    ext = att.extension_of(row.s3_key)
    settings = get_settings()
    if row.kind in ("pdf", "docx"):
        out_key = row.s3_key.rsplit(".", 1)[0] + ".json"
        result = await asyncio.to_thread(
            aws.invoke_function,
            settings.chat_extract_function,
            {
                "mode": "chat_extract", "bucket": settings.s3_bucket, "key": row.s3_key, "kind": row.kind,
                "out_key": out_key, "max_chars": att.MAX_EXTRACTED_CHARS,
            },
        )
        return att.from_lambda_result(result)
    data = await asyncio.to_thread(aws.get_object_bytes, row.s3_key, settings.chat_attachment_max_bytes)
    if row.kind == "xlsx":
        return await asyncio.to_thread(att.extract_spreadsheet, data)
    if row.kind == "csv":
        return await asyncio.to_thread(att.extract_csv, data)
    if row.kind == "image":
        return await asyncio.to_thread(
            att.describe_image, client, providers.models_of(client).builder, data, ext
        )
    return att.extract_plain(data)


@router.post("/{attachment_id}/complete", response_model=ChatAttachmentRead)
async def complete_chat_attachment(
    attachment_id: uuid.UUID,
    payload: ChatAttachmentComplete,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    client: BedrockClientProtocol = Depends(get_bedrock_client),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatAttachment:
    row = await _owned(db, current_user, attachment_id)
    if row.status == "ready":
        return row  # a retried completion
    if row.status == "processing":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This file is still being read.")
    settings = get_settings()
    ext = att.extension_of(row.s3_key)
    try:
        parts = [p.model_dump() for p in payload.parts]
        await asyncio.to_thread(aws.complete_multipart, row.s3_key, payload.upload_id, parts)
        head = await asyncio.to_thread(aws.head_object, row.s3_key)
        problem = None
        if head is None:
            problem = "The uploaded file was not found."
        elif head.size <= 0:
            problem = "The uploaded file is empty."
        elif head.size > settings.chat_attachment_max_bytes:
            problem = "The uploaded file exceeds the size limit."
        elif not att.magic_ok(ext, await asyncio.to_thread(aws.read_range, row.s3_key, 0, 16)):
            problem = f"The file content does not look like a valid .{ext} file."
    except (AwsJobsError, AwsNotConfiguredError) as exc:
        raise _aws_error(exc) from exc
    if problem:
        try:
            await asyncio.to_thread(aws.delete_object, row.s3_key)
        except Exception:  # noqa: BLE001 - the lifecycle rule expires it anyway
            logger.warning("could not delete rejected chat attachment %s", row.s3_key)
        row.status, row.error = "failed", problem
        await db.commit()
        await db.refresh(row)
        return row

    row.size_bytes = head.size
    row.status = "processing"
    await db.commit()
    try:
        extracted = await _extract(row, aws, client)
    except ValueError as exc:
        row.status, row.error = "failed", f"Could not read the file: {exc}"
    except (AwsJobsError, AwsNotConfiguredError):
        logger.warning("chat attachment %s: extraction call failed", row.id, exc_info=True)
        row.status, row.error = "failed", "Could not read the file right now; try uploading it again."
    except Exception:  # noqa: BLE001 - a malformed file must never 500 the upload
        logger.warning("chat attachment %s: extraction failed", row.id, exc_info=True)
        row.status, row.error = "failed", "Could not read the file: it may be damaged or password-protected."
    else:
        row.status, row.error = "ready", None
        row.extracted_text = extracted.text
        row.outline = extracted.outline
        row.needs_native = extracted.needs_native
        row.token_estimate = att.estimate_tokens(extracted.text)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_chat_attachment(
    attachment_id: uuid.UUID,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    row = await _owned(db, current_user, attachment_id)
    if row.message_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This file was already sent with a message.")
    key = row.s3_key
    await db.delete(row)
    await db.commit()
    try:
        await asyncio.to_thread(aws.delete_object, key)
    except Exception:  # noqa: BLE001 - the lifecycle rule expires it anyway
        logger.warning("could not delete chat attachment object %s", key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
