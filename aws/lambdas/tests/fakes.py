"""In-memory fakes: no AWS, no network."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone


class MemoryStore:
    """Same API as worker.store.S3Store."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.mtime: dict[str, datetime] = {}

    def put_bytes(self, key, data, content_type=None):
        self.objects[key] = bytes(data)
        self.mtime[key] = datetime.now(timezone.utc)

    def get_bytes(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def get_range(self, key, start, end):
        return self.get_bytes(key)[start:end + 1]

    def put_json(self, key, obj):
        self.put_bytes(key, json.dumps(obj).encode())

    def get_json(self, key):
        return json.loads(self.get_bytes(key))

    def put_text(self, key, text, content_type=None):
        self.put_bytes(key, text.encode())

    def get_text(self, key):
        return self.get_bytes(key).decode()

    def download(self, key, path):
        with open(path, "wb") as f:
            f.write(self.get_bytes(key))

    def upload(self, path, key, content_type=None):
        with open(path, "rb") as f:
            self.put_bytes(key, f.read())

    def copy(self, src, dst):
        self.put_bytes(dst, self.get_bytes(src))

    def head(self, key):
        if key not in self.objects:
            return None
        return {"size": len(self.objects[key]), "last_modified": self.mtime[key]}

    def exists(self, key):
        return key in self.objects

    def list(self, prefix):
        return [{"key": k, "size": len(v), "last_modified": self.mtime[k]}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]


class FakeTranscribe:
    """Amazon Transcribe client stand-in. get_transcription_job reports `statuses` in order (the last one repeats);
    a COMPLETED job's TranscriptFileUri is "https://transcripts.test/<job name>". `start_exc` is raised by every
    start_transcription_job. Every call is recorded in `.calls` as (operation, kwargs)."""

    def __init__(self, statuses=("COMPLETED",), start_exc=None, failure_reason="bad media"):
        self.statuses = list(statuses)
        self.start_exc = start_exc
        self.failure_reason = failure_reason
        self.calls: list[tuple[str, dict]] = []

    def start_transcription_job(self, **kw):
        self.calls.append(("start", kw))
        if self.start_exc is not None:
            raise self.start_exc

    def get_transcription_job(self, TranscriptionJobName):
        self.calls.append(("get", {"TranscriptionJobName": TranscriptionJobName}))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        job = {"TranscriptionJobName": TranscriptionJobName, "TranscriptionJobStatus": status}
        if status == "COMPLETED":
            job["Transcript"] = {"TranscriptFileUri": f"https://transcripts.test/{TranscriptionJobName}"}
        if status == "FAILED":
            job["FailureReason"] = self.failure_reason
        return {"TranscriptionJob": job}

    def delete_transcription_job(self, TranscriptionJobName):
        self.calls.append(("delete", {"TranscriptionJobName": TranscriptionJobName}))


def client_error(code: str, operation: str = "StartTranscriptionJob"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


class FakeConverse:
    """bedrock-runtime stand-in. `plan` maps modelId -> text | Exception | callable(kwargs)."""

    def __init__(self, plan):
        self.plan = plan
        self.calls: list[dict] = []

    def converse(self, **kw):
        self.calls.append(kw)
        action = self.plan[kw["modelId"]]
        if callable(action) and not isinstance(action, Exception):
            action = action(kw)
        if isinstance(action, Exception):
            raise action
        return {"output": {"message": {"content": [{"text": action}]}}, "usage": {"inputTokens": 1, "outputTokens": 2}}


class FakeHttpResponse:
    """requests.Response stand-in for OpenRouter."""

    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)
        self.headers = dict(headers or {})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def chat_ok(text, prompt_tokens=1, completion_tokens=2):
    return FakeHttpResponse(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                                  "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


def chat_err(status, message="boom", **extra):
    return FakeHttpResponse(status, {"error": {"code": status, "message": message, **extra}})


def stt_ok(text, seconds=60):
    """/audio/transcriptions answer: no choices, the transcript in `text`, audio seconds in usage."""
    return FakeHttpResponse(200, {"text": text, "usage": {"seconds": seconds, "cost": 0.001}})


class FakeOrSession:
    """requests.Session stand-in for OpenRouter. `plan` maps model -> response | text | Exception | callable(json) |
    list of those (consumed in order, the last one repeats). Every POST is recorded in `.calls`."""

    def __init__(self, plan, key_response=None):
        self.plan = plan
        self.calls: list[dict] = []
        # GET /key: a response, or an Exception to raise (default: a key with no limit)
        self.key_response = key_response if key_response is not None else FakeHttpResponse(200, {"data": {"limit": None, "limit_remaining": None}})

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": headers, "json": None, "timeout": timeout})
        if isinstance(self.key_response, Exception):
            raise self.key_response
        return self.key_response

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        action = self.plan[json["model"]]
        if isinstance(action, list):
            action = action.pop(0) if len(action) > 1 else action[0]
        if callable(action) and not isinstance(action, (Exception, FakeHttpResponse)):
            action = action(json)
        if isinstance(action, str):
            action = chat_ok(action)
        if isinstance(action, Exception):
            raise action
        return action


class FakeSsm:
    """ssm client stand-in: get_parameter returns `value` or raises `exc`."""

    def __init__(self, value="ssm-secret-key", exc=None):
        self.value, self.exc, self.calls = value, exc, []

    def get_parameter(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"Parameter": {"Value": self.value}}


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", location=None):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if location:
            self.headers["location"] = location
        self._body = body
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        self.closed = True


class FakeSession:
    """Maps a URL prefix to a FakeResponse (or list of them, consumed in order)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, **kw):
        self.requests.append((url, params))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise OSError(f"no route for {url}")


def public_resolver(host):
    return ["142.250.80.46"]


def tmp_cleanup(path):
    shutil.rmtree(path, ignore_errors=True)
