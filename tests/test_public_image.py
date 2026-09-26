"""Anonymous GHCR manifest check must prove unauthenticated pull access."""
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_public_image.py"
spec = importlib.util.spec_from_file_location("verify_public_image", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verify_public_manifest = module.verify_public_manifest


def test_public_manifest_exchanges_anonymous_token_then_head_checks_manifest():
    requests = []

    class Reply(io.BytesIO):
        def __init__(self, data=b"", status=200):
            super().__init__(data)
            self.status = status

    def open_url(req, timeout):
        requests.append(req)
        if len(requests) == 1:
            assert "repository:realbeepmcjeep/yoto-mcp:pull" in req.full_url
            assert not req.has_header("Authorization")
            return Reply(json.dumps({"token": "public-anonymous-token"}).encode())
        assert req.get_method() == "HEAD"
        assert req.full_url.endswith("/v2/realbeepmcjeep/yoto-mcp/manifests/latest")
        assert req.get_header("Authorization") == "Bearer public-anonymous-token"
        return Reply()

    assert verify_public_manifest("latest", open_url=open_url) is True
    assert len(requests) == 2


def test_private_or_missing_manifest_is_not_reported_public():
    def open_url(req, timeout):
        if "ghcr.io/token" in req.full_url:
            return io.BytesIO(b'{"token":"anonymous"}')
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(urllib.error.HTTPError) as error:
        verify_public_manifest("sha-abcdef0", open_url=open_url)
    assert error.value.code == 401


def test_tag_rejects_untrusted_url_components():
    with pytest.raises(ValueError):
        verify_public_manifest("../latest", open_url=lambda *_args, **_kwargs: None)
