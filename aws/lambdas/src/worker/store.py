"""Thin S3 wrapper. All S3 access in the worker goes through this so tests can swap in a fake."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class S3Store:
    def __init__(self, bucket: str, client: Any = None):
        self.bucket = bucket
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("s3", config=Config(retries={"max_attempts": 6, "mode": "adaptive"}))
        self.s3 = client

    # --- bytes / json / text
    def get_bytes(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def get_range(self, key: str, start: int, end: int) -> bytes:
        r = self.s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")
        return r["Body"].read()

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def get_json(self, key: str) -> Any:
        return json.loads(self.get_bytes(key).decode("utf-8"))

    def put_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")

    def put_text(self, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self.put_bytes(key, text.encode("utf-8"), content_type)

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8", errors="replace")

    # --- files (streamed; boto3's transfer manager does multipart)
    def download(self, key: str, path: str) -> None:
        self.s3.download_file(self.bucket, key, path)

    def upload(self, path: str, key: str, content_type: str | None = None) -> None:
        extra = {"ExtraArgs": {"ContentType": content_type}} if content_type else {}
        self.s3.upload_file(path, self.bucket, key, **extra)

    def copy(self, src_key: str, dst_key: str) -> None:
        self.s3.copy({"Bucket": self.bucket, "Key": src_key}, self.bucket, dst_key)

    # --- metadata
    def head(self, key: str) -> dict | None:
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # botocore ClientError
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": r["ContentLength"], "last_modified": r.get("LastModified") or datetime.now(timezone.utc)}

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def list(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                out.append({"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"]})
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]


def derived(eid: str, *parts: str) -> str:
    return "/".join(["derived", eid, *parts])
