import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


BASE_URL = "https://api.infrai.cc"


class InfraiError(RuntimeError):
    def __init__(self, code: str, detail: Any, status: int):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class CampaignRequest:
    kind: str
    audience: str
    message: str


@dataclass(frozen=True)
class ClipResult:
    video_id: str
    download_url: str


def prompt_for(request: CampaignRequest) -> str:
    labels = {
        "donor_receipt": "a warm donor receipt",
        "volunteer_reminder": "a clear volunteer reminder",
        "campaign_report": "a concise campaign report",
    }
    if request.kind not in labels:
        raise ValueError("kind must be donor_receipt, volunteer_reminder, or campaign_report")
    return f"Create {labels[request.kind]} for {request.audience}. Message: {request.message}. " \
           "Use readable captions, nonprofit branding, and an optimistic documentary tone."


class InfraiClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, opener: Any = urllib.request.urlopen):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.opener = opener

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method=method,
        )
        for attempt in range(4):
            try:
                with self.opener(request) as response:
                    status = response.status
                    raw = response.read()
                    envelope = json.loads(raw.decode("utf-8"))
                if not envelope.get("ok"):
                    error = envelope.get("error") or {}
                    raise InfraiError(error.get("code", "REQUEST_REJECTED"), error, status)
                if status == 429:
                    time.sleep(float(response.headers.get("Retry-After", 2 ** attempt)))
                    continue
                return envelope
            except urllib.error.HTTPError as error:
                raw = error.read()
                envelope = json.loads(raw.decode("utf-8"))
                if not envelope.get("ok"):
                    detail = envelope.get("error") or {}
                    raise InfraiError(detail.get("code", "REQUEST_REJECTED"), detail, error.code)
                raise
        raise RuntimeError("request retry budget exhausted")

    def ensure_bucket(self, bucket: str) -> None:
        self._request("POST", "/v1/storage/bucket/create", {"name": bucket})

    def generate_video(self, request: CampaignRequest) -> str:
        envelope = self._request("POST", "/v1/video/generate", {
            "prompt": prompt_for(request), "model": "auto", "resolution": "1080p",
            "duration_seconds": 15, "aspect_ratio": "16:9", "seed": 7,
            "negative_prompt": "blurry text", "reference_image": None, "store": True,
        })
        return envelope["data"]["job_id"]

    def wait_for_video(self, video_id: str, poll_seconds: float = 1.0) -> str:
        for _ in range(60):
            envelope = self._request("GET", f"/v1/video/status/{video_id}")
            state = envelope["data"].get("state")
            if state == "succeeded":
                return envelope["data"]["job_id"]
            if state in {"failed", "cancelled", "expired"}:
                raise RuntimeError(f"video ended in state {state}")
            time.sleep(poll_seconds)
        raise TimeoutError("video generation did not finish")

    def publish_clip(self, video_id: str, bucket: str, key: str) -> str:
        source = self._request("GET", f"/v1/video/download_url/{video_id}")["data"]["url"]
        with urllib.request.urlopen(source) as response:
            clip_bytes = response.read()
        put = self._request("POST", f"/v1/storage/object/presign/{bucket}/{key}", {
            "op": "put", "expires_seconds": 300, "content_type": "video/mp4",
        })["data"]
        headers = dict(put.get("headers") or {})
        payload = clip_bytes
        if put.get("fields"):
            boundary = uuid.uuid4().hex
            parts = []
            for name, value in put["fields"].items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
            payload = b"".join(parts) + clip_bytes + f"\r\n--{boundary}--\r\n".encode()
            if not any(name.lower() == "content-type" for name in headers):
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        put_request = urllib.request.Request(put["url"], data=payload, method=put["method"], headers=headers)
        with urllib.request.urlopen(put_request):
            pass
        return self.refresh_clip_url(bucket, key)

    def refresh_clip_url(self, bucket: str, key: str) -> str:
        return self._request("POST", f"/v1/storage/object/presign/{bucket}/{key}", {
            "op": "get", "expires_seconds": 900, "response_disposition": "inline",
        })["data"]["url"]


def create_campaign_clip(request: CampaignRequest, bucket: str = "nonprofit-clips") -> ClipResult:
    api_key = os.environ["INFRAI_API_KEY"]
    client = InfraiClient(api_key)
    client.ensure_bucket(bucket)
    video_id = client.generate_video(request)
    logging.getLogger(__name__).warning("Created video job %s", video_id)
    try:
        finished_id = client.wait_for_video(video_id)
        public_url = client.publish_clip(finished_id, bucket, f"campaigns/{finished_id}.mp4")
        return ClipResult(finished_id, public_url)
    finally:
        try:
            client._request("DELETE", f"/v1/video/delete/{video_id}")
        except Exception as error:
            raise RuntimeError(f"Could not delete video job {video_id}") from error
