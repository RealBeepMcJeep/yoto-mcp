from pathlib import Path

from yoto_mcp.config import Settings


def test_settings_reads_only_named_env_vars(tmp_path: Path):
    settings = Settings.from_env({
        "YOTO_CLIENT_ID": "public-client-id",
        "YOTO_SESSION_FILE": str(tmp_path / "session.json"),
        "YOTO_UPLOAD_ROOT": str(tmp_path / "uploads"),
        "YOTO_JOB_ROOT": str(tmp_path / "private-jobs"),
        "YOTO_ALLOW_WRITES": "1",
    })
    assert settings.client_id == "public-client-id"
    assert settings.session_file == tmp_path / "session.json"
    assert settings.upload_root == tmp_path / "uploads"
    assert settings.job_root == tmp_path / "private-jobs"
    assert settings.allow_writes is True


def test_writes_and_uploads_are_off_unless_explicitly_configured():
    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client-id"})
    assert settings.allow_writes is False
    assert settings.upload_root is None
    assert settings.job_root is None
