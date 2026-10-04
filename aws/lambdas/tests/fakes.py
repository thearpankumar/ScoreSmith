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
