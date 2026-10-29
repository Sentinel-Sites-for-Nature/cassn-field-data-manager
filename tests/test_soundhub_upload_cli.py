"""Exercise the recursive CLI subprocess without AWS credentials or network."""

import json
import sys
from pathlib import Path

import pytest

from cassn.soundhub.staging import project_root
from cassn.soundhub.upload import SoundHubUploadError, upload_project


@pytest.fixture
def cli_upload(tmp_path, monkeypatch):
    staging = tmp_path / "staging with spaces"
    root = project_root(staging)
    root.mkdir(parents=True)
    for relative in ("deployment.csv", "recording.csv", "pending/a.flac", "done/b.flac", ".hidden"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    arguments = tmp_path / "arguments.json"
    fake = tmp_path / "aws"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['SOUNDHUB_TEST_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))\n"
        "mode = os.environ.get('SOUNDHUB_TEST_MODE', 'success')\n"
        "if mode == 'failure':\n"
        "    print('AccessDenied: test failure', flush=True)\n"
        "    sys.exit(1)\n"
        "if mode == 'wait':\n"
        "    time.sleep(30)\n"
        "for path in Path(sys.argv[3]).rglob('*'):\n"
        "    if path.is_file():\n"
        "        assert path.is_symlink()\n"
        "        print(f'upload: {path} to {sys.argv[4]}{path.relative_to(sys.argv[3]).as_posix()}', flush=True)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SOUNDHUB_TEST_ARGUMENTS", str(arguments))
    monkeypatch.setattr("cassn.soundhub.upload.shutil.which", lambda name: str(fake))
    monkeypatch.setattr("cassn.soundhub.upload._client", lambda settings: object())
    monkeypatch.setattr("cassn.soundhub.upload._existing_sizes", lambda *args: {})
    settings = dict(bucket="test-bucket", upload_prefix="upload",
                    project_short_name="UCNature-SSN", aws_profile="soundhub-test", region="us-east-2")
    return staging, settings, arguments


def test_recursive_copy_scope_options_and_cleanup(cli_upload):
    staging, settings, arguments = cli_upload
    progress = []
    result = upload_project(staging, settings=settings, deployment_ids={"pending"},
                            progress=lambda *args: progress.append(args))
    command = json.loads(arguments.read_text())
    assert command[:2] == ["s3", "cp"]
    assert command[3] == "s3://test-bucket/upload/UCNature-SSN/"
    assert "--recursive" in command
    assert command[command.index("--profile") + 1] == "soundhub-test"
    assert command[command.index("--region") + 1] == "us-east-2"
    assert command[command.index("--checksum-algorithm") + 1] == "SHA256"
    assert result == dict(cancelled=False, uploaded=3, skipped=0, uploaded_bytes=12, total=3)
    assert progress[-1][:2] == (3, 3)
    assert not Path(command[2]).exists()


def test_cli_failure_is_not_success_and_cleans_selector(cli_upload, monkeypatch):
    staging, settings, arguments = cli_upload
    monkeypatch.setenv("SOUNDHUB_TEST_MODE", "failure")
    with pytest.raises(SoundHubUploadError, match="AccessDenied"):
        upload_project(staging, settings=settings)
    assert not Path(json.loads(arguments.read_text())[2]).exists()


def test_persistent_cli_log_survives_subprocess_failure(cli_upload, monkeypatch):
    staging, settings, arguments = cli_upload
    log = staging.parent / "transfers" / "test-attempt" / "upload.log"
    monkeypatch.setenv("SOUNDHUB_TEST_MODE", "failure")
    with pytest.raises(SoundHubUploadError, match="AccessDenied"):
        upload_project(staging, settings=settings, log_path=log)
    assert "AccessDenied: test failure" in log.read_text()
    assert not Path(json.loads(arguments.read_text())[2]).exists()


def test_cancellation_stops_quiet_cli_and_cleans_selector(cli_upload, monkeypatch):
    staging, settings, arguments = cli_upload
    monkeypatch.setenv("SOUNDHUB_TEST_MODE", "wait")
    result = upload_project(staging, settings=settings, is_cancelled=arguments.exists)
    assert result["cancelled"] is True
    assert result["uploaded"] == 0
    assert not Path(json.loads(arguments.read_text())[2]).exists()


def test_missing_aws_cli_blocks_upload(cli_upload, monkeypatch):
    staging, settings, arguments = cli_upload
    monkeypatch.setattr("cassn.soundhub.upload.shutil.which", lambda name: None)
    with pytest.raises(SoundHubUploadError, match="AWS CLI"):
        upload_project(staging, settings=settings)
    assert not arguments.exists()
