import pytest

from src.nonprofit_video_service import CampaignRequest, InfraiClient, prompt_for


def test_campaign_kind_changes_the_story_direction():
    request = CampaignRequest("donor_receipt", "monthly donors", "Thank you for funding meals")
    assert prompt_for(request).startswith("Create a warm donor receipt for monthly donors")


def test_unknown_campaign_kind_is_rejected_before_an_api_call():
    with pytest.raises(ValueError):
        prompt_for(CampaignRequest("press_release", "supporters", "News"))


def test_wait_for_video_returns_succeeded_job_id(monkeypatch):
    client = InfraiClient("test")
    states = iter([{"state": "running", "job_id": "job-123"},
                   {"state": "succeeded", "job_id": "job-123"}])
    monkeypatch.setattr(client, "_request", lambda *args: {"data": next(states)})
    assert client.wait_for_video("job-123", poll_seconds=0) == "job-123"


@pytest.mark.parametrize("state", ["failed", "cancelled", "expired"])
def test_wait_for_video_rejects_terminal_failure(monkeypatch, state):
    client = InfraiClient("test")
    monkeypatch.setattr(client, "_request", lambda *args: {"data": {"state": state}})
    with pytest.raises(RuntimeError, match=state):
        client.wait_for_video("job-123", poll_seconds=0)


@pytest.mark.parametrize("fail_at", [None, "wait", "publish"])
def test_campaign_always_deletes_generated_job(monkeypatch, fail_at):
    from src import nonprofit_video_service as service

    calls = []
    monkeypatch.setenv("INFRAI_API_KEY", "test")
    monkeypatch.setattr(service.InfraiClient, "ensure_bucket", lambda *args: None)
    monkeypatch.setattr(service.InfraiClient, "generate_video", lambda *args: "job-123")

    def wait(*args):
        if fail_at == "wait":
            raise RuntimeError("wait failed")
        return "job-123"

    def publish(*args):
        if fail_at == "publish":
            raise RuntimeError("publish failed")
        return "https://example.test/clip"

    monkeypatch.setattr(service.InfraiClient, "wait_for_video", wait)
    monkeypatch.setattr(service.InfraiClient, "publish_clip", publish)
    monkeypatch.setattr(service.InfraiClient, "_request", lambda self, *args: calls.append(args))
    request = CampaignRequest("donor_receipt", "donors", "Thank you")
    if fail_at:
        with pytest.raises(RuntimeError, match=f"{fail_at} failed"):
            service.create_campaign_clip(request)
    else:
        assert service.create_campaign_clip(request).video_id == "job-123"
    assert calls == [("DELETE", "/v1/video/delete/job-123")]


def test_cleanup_failure_exposes_job_id(monkeypatch):
    from src import nonprofit_video_service as service

    monkeypatch.setenv("INFRAI_API_KEY", "test")
    monkeypatch.setattr(service.InfraiClient, "ensure_bucket", lambda *args: None)
    monkeypatch.setattr(service.InfraiClient, "generate_video", lambda *args: "job-123")
    monkeypatch.setattr(service.InfraiClient, "wait_for_video", lambda *args: "job-123")
    monkeypatch.setattr(service.InfraiClient, "publish_clip", lambda *args: "url")

    def reject_delete(*args):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(service.InfraiClient, "_request", reject_delete)
    with pytest.raises(RuntimeError, match="Could not delete video job job-123"):
        service.create_campaign_clip(CampaignRequest("donor_receipt", "donors", "Thanks"))


@pytest.mark.parametrize("fields,method", [({}, "PUT"), ({"policy": "signed-policy", "key": "clip.mp4"}, "POST")])
def test_publish_uses_presigned_upload_contract(monkeypatch, fields, method):
    import io
    import urllib.request

    client = InfraiClient("test")
    replies = iter([
        {"data": {"url": "https://source.test/video"}},
        {"data": {"url": "https://upload.test/object", "method": method,
                  "headers": {"X-Signed": "required"}, "fields": fields}},
        {"data": {"url": "https://download.test/object"}},
    ])
    monkeypatch.setattr(client, "_request", lambda *args: next(replies))
    uploads = []

    def open_url(request):
        if isinstance(request, str):
            return io.BytesIO(b"video-bytes")
        uploads.append(request)
        return io.BytesIO()

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    assert client.publish_clip("job", "bucket", "clip.mp4") == "https://download.test/object"
    upload, = uploads
    assert upload.method == method
    assert upload.get_header("X-signed") == "required"
    if fields:
        for name, value in fields.items():
            assert f'name="{name}"\r\n\r\n{value}\r\n'.encode() in upload.data
        assert b"video-bytes" in upload.data
        assert "multipart/form-data; boundary=" in upload.get_header("Content-type")
    else:
        assert upload.data == b"video-bytes"
