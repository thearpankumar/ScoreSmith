"""AWS side of the AI evaluation pipeline: S3 (presigned multipart uploads, derived artefacts) and
Step Functions (one execution per evaluation).

`AwsJobsProtocol` is the seam; `Boto3AwsJobs` is the real implementation and `tests/fakes.py::
FakeAwsJobs` the test double. All methods are synchronous (boto3); async callers use
`asyncio.to_thread`. The boto3 clients are built lazily and with the dedicated APP access keys
passed explicitly - never the Bedrock bearer token / ambient credentials."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)

SFN_RUNNING = "RUNNING"
SFN_SUCCEEDED = "SUCCEEDED"
SFN_TERMINAL_FAILURES = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})


class AwsNotConfiguredError(RuntimeError):
    """S3 / Step Functions settings or app keys are missing."""


class AwsJobsError(RuntimeError):
    """Any AWS call failure, normalised."""


def derived_key(evaluation_id: str, name: str) -> str:
    return f"derived/{evaluation_id}/{name}"


def execution_name(evaluation_id: str, attempt: int) -> str:
    return str(evaluation_id) if attempt <= 1 else f"{evaluation_id}-a{attempt}"


@dataclass
class ExecutionInfo:
    status: str  # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED
    error: str | None = None
    cause: str | None = None
    start_date: datetime | None = None
    stop_date: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObjectHead:
    size: int
    content_type: str | None = None


class AwsJobsProtocol(Protocol):
    # --- S3 uploads ---
    def create_multipart(self, key: str, content_type: str | None) -> str: ...
    def presign_part(self, key: str, upload_id: str, part_number: int, expires: int) -> str: ...
    def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> None: ...
    def abort_multipart(self, key: str, upload_id: str) -> None: ...
    def head_object(self, key: str) -> ObjectHead | None: ...
    def read_range(self, key: str, start: int, length: int) -> bytes: ...
    def get_object_bytes(self, key: str, max_bytes: int) -> bytes: ...
    def delete_object(self, key: str) -> None: ...
    # --- derived artefacts (None when missing) ---
    def read_json(self, key: str) -> dict[str, Any] | None: ...
    # --- Step Functions ---
    def start_execution(self, name: str, payload: dict[str, Any]) -> str: ...
    def describe_execution(self, arn: str) -> ExecutionInfo: ...
    def stop_execution(self, arn: str, cause: str) -> None: ...


def read_progress(aws: AwsJobsProtocol, evaluation_id: str) -> dict[str, Any] | None:
    return aws.read_json(derived_key(str(evaluation_id), "progress.json"))


def read_corpus_json(aws: AwsJobsProtocol, evaluation_id: str) -> dict[str, Any] | None:
    return aws.read_json(derived_key(str(evaluation_id), "corpus.json"))


def read_status(aws: AwsJobsProtocol, evaluation_id: str) -> dict[str, Any] | None:
    return aws.read_json(derived_key(str(evaluation_id), "status.json"))


def _error_code(exc: Exception) -> str | None:
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


class Boto3AwsJobs:
    """Real implementation. Nothing touches AWS at construction time."""

    def __init__(self) -> None:
        self._s3: Any = None
        self._sfn: Any = None
        self._lock = threading.Lock()

    def _session_kwargs(self) -> dict[str, Any]:
        s = get_settings()
        if not (s.aws_app_access_key_id and s.aws_app_secret_access_key):
            raise AwsNotConfiguredError("AWS_APP_ACCESS_KEY_ID / AWS_APP_SECRET_ACCESS_KEY are not configured.")
        return {
            "region_name": s.aws_region,
            "aws_access_key_id": s.aws_app_access_key_id,
            "aws_secret_access_key": s.aws_app_secret_access_key,
        }

    def _bucket(self) -> str:
        bucket = get_settings().s3_bucket
        if not bucket:
            raise AwsNotConfiguredError("S3_BUCKET is not configured.")
        return bucket

    def _s3_client(self) -> Any:
        with self._lock:
            if self._s3 is None:
                import boto3
                from botocore.config import Config

                self._s3 = boto3.client(
                    "s3",
                    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
                    **self._session_kwargs(),
                )
            return self._s3

    def _sfn_client(self) -> Any:
        with self._lock:
            if self._sfn is None:
                import boto3

                self._sfn = boto3.client("stepfunctions", **self._session_kwargs())
            return self._sfn

    @staticmethod
    def _wrap(op: str, exc: Exception) -> AwsJobsError:
        logger.warning("AWS %s failed: %s: %s", op, type(exc).__name__, exc)
        return AwsJobsError(f"AWS {op} failed: {exc}")

    # --- S3 ---
    def create_multipart(self, key: str, content_type: str | None) -> str:
        kwargs: dict[str, Any] = {"Bucket": self._bucket(), "Key": key}
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            return self._s3_client().create_multipart_upload(**kwargs)["UploadId"]
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("CreateMultipartUpload", exc) from exc

    def presign_part(self, key: str, upload_id: str, part_number: int, expires: int) -> str:
        try:
            return self._s3_client().generate_presigned_url(
                "upload_part",
                Params={"Bucket": self._bucket(), "Key": key, "UploadId": upload_id, "PartNumber": part_number},
                ExpiresIn=expires,
            )
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("presign upload_part", exc) from exc

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
        try:
            self._s3_client().complete_multipart_upload(
                Bucket=self._bucket(),
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": int(p["part_number"]), "ETag": p["etag"]}
                        for p in sorted(parts, key=lambda p: int(p["part_number"]))
                    ]
                },
            )
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("CompleteMultipartUpload", exc) from exc

    def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            self._s3_client().abort_multipart_upload(Bucket=self._bucket(), Key=key, UploadId=upload_id)
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("AbortMultipartUpload", exc) from exc

    def head_object(self, key: str) -> ObjectHead | None:
        try:
            r = self._s3_client().head_object(Bucket=self._bucket(), Key=key)
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise self._wrap("HeadObject", exc) from exc
        return ObjectHead(size=int(r["ContentLength"]), content_type=r.get("ContentType"))

    def read_range(self, key: str, start: int, length: int) -> bytes:
        try:
            r = self._s3_client().get_object(
                Bucket=self._bucket(), Key=key, Range=f"bytes={start}-{start + length - 1}"
            )
            return r["Body"].read()
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("GetObject(range)", exc) from exc

    def get_object_bytes(self, key: str, max_bytes: int) -> bytes:
        try:
            r = self._s3_client().get_object(Bucket=self._bucket(), Key=key)
            data = r["Body"].read(max_bytes + 1)
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("GetObject", exc) from exc
        if len(data) > max_bytes:
            raise AwsJobsError(f"Object {key} is larger than {max_bytes} bytes.")
        return data

    def delete_object(self, key: str) -> None:
        try:
            self._s3_client().delete_object(Bucket=self._bucket(), Key=key)
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("DeleteObject", exc) from exc

    def read_json(self, key: str) -> dict[str, Any] | None:
        s3 = self._s3_client()
        try:
            r = s3.get_object(Bucket=self._bucket(), Key=key)
            return json.loads(r["Body"].read())
        except AwsNotConfiguredError:
            raise
        except ValueError:  # half-written / invalid JSON: treat as not there yet
            return None
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise self._wrap("GetObject(json)", exc) from exc

    # --- Step Functions ---
    def start_execution(self, name: str, payload: dict[str, Any]) -> str:
        arn = get_settings().sfn_state_machine_arn
        if not arn:
            raise AwsNotConfiguredError("SFN_STATE_MACHINE_ARN is not configured.")
        try:
            r = self._sfn_client().start_execution(stateMachineArn=arn, name=name, input=json.dumps(payload))
            return r["executionArn"]
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) == "ExecutionAlreadyExists":
                # Idempotent restart: the execution for this name already exists; reuse its ARN.
                return f"{arn.replace(':stateMachine:', ':execution:')}:{name}"
            raise self._wrap("StartExecution", exc) from exc

    def describe_execution(self, arn: str) -> ExecutionInfo:
        try:
            r = self._sfn_client().describe_execution(executionArn=arn)
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("DescribeExecution", exc) from exc
        return ExecutionInfo(
            status=r["status"],
            error=r.get("error"),
            cause=r.get("cause"),
            start_date=r.get("startDate"),
            stop_date=r.get("stopDate"),
        )

    def stop_execution(self, arn: str, cause: str) -> None:
        try:
            self._sfn_client().stop_execution(executionArn=arn, error="cancelled", cause=cause[:32000])
        except AwsNotConfiguredError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("StopExecution", exc) from exc
