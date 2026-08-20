"""Tests for the Wildlife SoundHub staging and export pipeline."""

from __future__ import annotations

import csv
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from cassn.config import (
    SOUNDHUB_DEPLOYMENT_FIELDS,
    SOUNDHUB_PROJECT_SHORT_NAME,
    SOUNDHUB_RECORDING_FIELDS,
)
from cassn.soundhub.export import (
    build_deployment_rows,
    build_recording_rows,
    read_bd_audio_rows,
    refresh_project_csvs,
    write_deployment_copy,
    write_deployment_fragments,
)
from cassn.soundhub.staging import (
    SoundHubStagingError,
    _wav_missing_final_pad,
    flac_available,
    project_root,
    stage_deployment,
    validate_deployment_id,
)
from cassn.box.verification import is_orphan_on_box
from cassn.soundhub.upload import project_prefix, staged_objects

needs_flac = pytest.mark.skipif(not flac_available(), reason="requires the flac encoder")


def make_wav(path: Path, seconds: float = 0.05) -> None:
    """Write a tiny mono 48 kHz WAV — the AudioMoth BD sample rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(48000 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", i % 1000) for i in range(frames)))


def append_unpadded_guano(path: Path, text: bytes = b"GUANO|odd") -> None:
    """Append an odd-sized GUANO chunk with no pad byte, as the AudioMoth does.

    The RIFF size field counts the chunk but not the (absent) pad, reproducing
    the firmware's spec violation byte for byte.
    """
    assert len(text) % 2 == 1, "the quirk only occurs for odd-sized chunks"
    with open(path, "r+b") as f:
        f.seek(4)
        riff_size = int.from_bytes(f.read(4), "little")
        f.seek(4)
        f.write((riff_size + 8 + len(text)).to_bytes(4, "little"))
        f.seek(0, 2)
        f.write(b"guan" + len(text).to_bytes(4, "little") + text)


def audio_row(deployment_id: str, seq: str, **overrides) -> dict:
    row = {
        "filename": f"{deployment_id}_{seq}.wav",
        "original_filename": "20260511_000000.WAV",
        "deployment_id": deployment_id,
        "device_type": "BD",
        "file_type": "audio",
        "recorded_datetime": "2026-05-11T00:00:00-07:00",
        "recording_duration_sec": "3600",
        "placename": "StrathearnRanch_plot1",
        "latitude": "36.84449545",
        "longitude": "-121.1632039",
        "deployment_start_date": "2026-05-11",
        "deployment_end_date": "2026-05-17",
        "deployment_start_time": "00:00",
        "deployment_end_time": "23:59",
        "date_installed": "2026-04-15",
        "frequency": "daily",
        "duration": "12:59",
        "gain": "High",
        "ARU_make": "Open Acoustic Devices",
        "ARU_model": "AudioMoth-Firmware-Basic 1.11.1",
        "ARU_container": "polybag",
        "ARU_microphone": "internal",
        "sensor_height_meters": "2.5",
        "recorded_by": "Imperato, John",
        "subproject": "StrathearnRanch_2026",
    }
    row.update(overrides)
    return row


@pytest.fixture
def deployment(tmp_path):
    """A deployment event with two BD plots, one BT plot, and a CONFIG sidecar."""
    folder = tmp_path / "UC_StrathearnRanch_20260714"
    rows = [
        audio_row("UC_StrathearnRanch_plot1_BD_20260714", "00001"),
        audio_row("UC_StrathearnRanch_plot1_BD_20260714", "00002",
                  recorded_datetime="2026-05-11T20:00:00-07:00"),
        audio_row("UC_StrathearnRanch_plot2_BD_20260713", "00001",
                  placename="StrathearnRanch_plot2",
                  filename="UC_StrathearnRanch_plot2_BD_20260714_00001.wav"),
        audio_row("UC_StrathearnRanch_plot1_BT_20260714", "00001", device_type="BT"),
        audio_row("UC_StrathearnRanch_plot1_BD_20260714", "CONFIG_01", file_type="config"),
    ]
    for row in rows:
        device = row["deployment_id"].split("_")[2].replace("plot", "p") + "_" + row["device_type"]
        make_wav(folder / "raw_data" / device / row["filename"])

    fieldnames = sorted({k for r in rows for k in r})
    with open(folder / "audio_file_metadata.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return folder


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------

def test_read_bd_audio_rows_excludes_bat_and_config(deployment):
    rows = read_bd_audio_rows(deployment)
    assert len(rows) == 3
    assert {r["device_type"] for r in rows} == {"BD"}
    assert {r["file_type"] for r in rows} == {"audio"}


def test_read_bd_audio_rows_excludes_header_only_recordings(deployment):
    """A failed recording (empty data chunk, ~488 bytes) has no audio to submit."""
    rows = read_bd_audio_rows(deployment)
    empty = audio_row(
        "UC_StrathearnRanch_plot1_BD_20260714", "00003",
        file_size_bytes="488", recording_duration_sec="",
        recording_stop_reason="SD card write error",
    )
    kept = audio_row(
        "UC_StrathearnRanch_plot1_BD_20260714", "00004",
        file_size_bytes="339840785",
    )
    blank_size = audio_row(
        "UC_StrathearnRanch_plot1_BD_20260714", "00005",
        file_size_bytes="",
    )
    fieldnames = sorted({k for r in (*rows, empty, kept, blank_size) for k in r})
    with open(deployment / "audio_file_metadata.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([*rows, empty, kept, blank_size])

    names = {r["filename"] for r in read_bd_audio_rows(deployment)}
    assert empty["filename"] not in names, "header-only recording must be excluded"
    assert kept["filename"] in names
    assert blank_size["filename"] in names, "an unknown size must not exclude a row"


def test_read_bd_audio_rows_requires_metadata(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_bd_audio_rows(tmp_path)


# ---------------------------------------------------------------------------
# deployment_id validation — the guard against unfixable S3 keys
# ---------------------------------------------------------------------------

def test_validate_returns_the_shared_id():
    rows = [audio_row("UC_QuailRidge_plot1_BD_20260118", "00001")]
    assert validate_deployment_id(rows) == "UC_QuailRidge_plot1_BD_20260118"


def test_validate_rejects_mixed_ids():
    rows = [
        audio_row("UC_QuailRidge_plot1_BD_20260118", "00001"),
        audio_row("UC_QuailRidge_plot2_BD_20260118", "00001"),
    ]
    with pytest.raises(SoundHubStagingError, match="different deployment_id"):
        validate_deployment_id(rows)


def test_validate_rejects_blank_id():
    with pytest.raises(SoundHubStagingError, match="Blank deployment_id"):
        validate_deployment_id([audio_row("", "00001", filename="x.wav")])


def test_validate_rejects_non_bd_device():
    rows = [audio_row("UC_QuailRidge_plot1_BT_20260118", "00001")]
    with pytest.raises(SoundHubStagingError, match="not a well-formed BD"):
        validate_deployment_id(rows)


def test_validate_rejects_malformed_id():
    rows = [audio_row("UC_QuailRidge_plot1_BD", "00001",
                      filename="UC_QuailRidge_plot1_BD_00001.wav")]
    with pytest.raises(SoundHubStagingError, match="not a well-formed BD"):
        validate_deployment_id(rows)


def test_validate_allows_a_filename_date_differing_from_the_id_date():
    """The id's date is the device's Survey123 retrieval date; the filename's is
    the deployment event's. A recorder collected a day early makes them differ,
    and that is normal — only the identity half has to match."""
    rows = [audio_row("UC_StrathearnRanch_plot2_BD_20260713", "00001",
                      filename="UC_StrathearnRanch_plot2_BD_20260714_00001.wav")]
    assert validate_deployment_id(rows) == "UC_StrathearnRanch_plot2_BD_20260713"


def test_validate_still_rejects_a_different_plot():
    rows = [audio_row("UC_StrathearnRanch_plot2_BD_20260713", "00001",
                      filename="UC_StrathearnRanch_plot3_BD_20260714_00001.wav")]
    with pytest.raises(SoundHubStagingError, match="does not belong"):
        validate_deployment_id(rows)


def test_validate_rejects_filename_not_matching_id():
    rows = [audio_row("UC_QuailRidge_plot1_BD_20260118", "00001",
                      filename="UC_Elsewhere_plot9_BD_20260118_00001.wav")]
    with pytest.raises(SoundHubStagingError, match="does not belong"):
        validate_deployment_id(rows)


# ---------------------------------------------------------------------------
# CSV projection
# ---------------------------------------------------------------------------

def test_deployment_rows_one_per_deployment(deployment):
    rows = build_deployment_rows(read_bd_audio_rows(deployment))
    assert [r["deployment_id"] for r in rows] == [
        "UC_StrathearnRanch_plot1_BD_20260714",
        "UC_StrathearnRanch_plot2_BD_20260713",
    ]
    assert list(rows[0]) == SOUNDHUB_DEPLOYMENT_FIELDS
    assert rows[0]["project_short_name"] == SOUNDHUB_PROJECT_SHORT_NAME


def test_deployment_rows_carry_no_utc_offset(deployment):
    """SoundHub's deployment template takes plain dates and times."""
    row = build_deployment_rows(read_bd_audio_rows(deployment))[0]
    assert row["deployment_start_date"] == "2026-05-11"
    assert row["deployment_start_time"] == "00:00"
    assert "-07:00" not in row["deployment_start_time"]


def test_recording_rows_use_flac_names_and_offsets(deployment):
    rows = build_recording_rows(read_bd_audio_rows(deployment))
    assert list(rows[0]) == SOUNDHUB_RECORDING_FIELDS
    assert rows[0]["filename"].endswith(".flac")
    assert rows[0]["start"] == "2026-05-11 00:00:00-07:00"
    # end = start + recording_duration_sec (3600s)
    assert rows[0]["end"] == "2026-05-11 01:00:00-07:00"


def test_recording_end_blank_without_duration():
    rows = build_recording_rows(
        [audio_row("UC_QuailRidge_plot1_BD_20260118", "00001", recording_duration_sec="")]
    )
    assert rows[0]["start"] == "2026-05-11 00:00:00-07:00"
    assert rows[0]["end"] == ""


def test_recording_blank_timestamp_is_not_invented():
    rows = build_recording_rows(
        [audio_row("UC_QuailRidge_plot1_BD_20260118", "00001", recorded_datetime="")]
    )
    assert rows[0]["start"] == ""
    assert rows[0]["end"] == ""


# ---------------------------------------------------------------------------
# Project manifests
# ---------------------------------------------------------------------------

def test_project_csvs_are_cumulative_and_idempotent(deployment, tmp_path):
    staging = tmp_path / "staging"
    rows = read_bd_audio_rows(deployment)

    write_deployment_fragments(staging, rows)
    first = refresh_project_csvs(staging)
    assert first["deployment_count"] == 2
    assert first["recording_count"] == 3

    # Re-staging the same deployment replaces its fragment rather than appending.
    write_deployment_fragments(staging, rows)
    second = refresh_project_csvs(staging)
    assert second["deployment_count"] == 2
    assert second["recording_count"] == 3

    # A different deployment accumulates alongside it.
    write_deployment_fragments(
        staging, [audio_row("UC_QuailRidge_plot1_BD_20260118", "00001")]
    )
    third = refresh_project_csvs(staging)
    assert third["deployment_count"] == 3
    assert third["recording_count"] == 4


def test_project_csvs_sit_at_the_project_root(deployment, tmp_path):
    staging = tmp_path / "staging"
    write_deployment_fragments(staging, read_bd_audio_rows(deployment))
    result = refresh_project_csvs(staging)
    assert result["deployment_csv"] == staging / SOUNDHUB_PROJECT_SHORT_NAME / "deployment.csv"
    assert result["recording_csv"] == staging / SOUNDHUB_PROJECT_SHORT_NAME / "recording.csv"


def test_fragments_stay_out_of_the_s3_mirror(deployment, tmp_path):
    staging = tmp_path / "staging"
    write_deployment_fragments(staging, read_bd_audio_rows(deployment))
    refresh_project_csvs(staging)
    mirrored = {p.name for p in project_root(staging).rglob("*") if p.is_file()}
    assert mirrored == {"deployment.csv", "recording.csv"}


def test_deployment_copy_lands_in_the_deployment_folder(deployment):
    out = write_deployment_copy(deployment, read_bd_audio_rows(deployment))
    assert (out / "deployment.csv").exists()
    assert (out / "recording.csv").exists()
    assert out == deployment / "soundhub"


# ---------------------------------------------------------------------------
# FLAC staging
# ---------------------------------------------------------------------------

@needs_flac
def test_stage_splits_plots_into_separate_deployments(deployment, tmp_path):
    staging = tmp_path / "staging"
    rows = read_bd_audio_rows(deployment)
    result = stage_deployment(deployment, rows, staging)

    assert result["deployment_ids"] == [
        "UC_StrathearnRanch_plot1_BD_20260714",
        "UC_StrathearnRanch_plot2_BD_20260713",
    ]
    assert result["converted"] == 3
    assert result["skipped"] == 0

    root = project_root(staging)
    assert len(list((root / "UC_StrathearnRanch_plot1_BD_20260714").glob("*.flac"))) == 2
    assert len(list((root / "UC_StrathearnRanch_plot2_BD_20260713").glob("*.flac"))) == 1
    assert not (root / "UC_StrathearnRanch_plot1_BT_20260714").exists()


@needs_flac
def test_stage_is_idempotent(deployment, tmp_path):
    staging = tmp_path / "staging"
    rows = read_bd_audio_rows(deployment)
    stage_deployment(deployment, rows, staging)
    second = stage_deployment(deployment, rows, staging)
    assert second["converted"] == 0
    assert second["skipped"] == 3


@needs_flac
def test_stage_never_touches_the_source_wavs(deployment, tmp_path):
    before = {
        p: p.stat().st_mtime_ns for p in deployment.rglob("*.wav")
    }
    stage_deployment(deployment, read_bd_audio_rows(deployment), tmp_path / "staging")
    assert {p: p.stat().st_mtime_ns for p in deployment.rglob("*.wav")} == before


@needs_flac
def test_flac_decodes_back_to_identical_audio(deployment, tmp_path):
    """FLAC is lossless: the decoded samples must match the source exactly."""
    staging = tmp_path / "staging"
    stage_deployment(deployment, read_bd_audio_rows(deployment), staging)

    flac_file = next(project_root(staging).rglob("*.flac"))
    restored = tmp_path / "restored.wav"
    subprocess.run(
        ["flac", "-d", "--silent", "-f", "--output-name", str(restored), str(flac_file)],
        check=True, capture_output=True,
    )
    source = next(deployment.rglob(flac_file.stem + ".wav"))
    with wave.open(str(source)) as a, wave.open(str(restored)) as b:
        assert a.getparams()[:3] == b.getparams()[:3]
        assert a.readframes(a.getnframes()) == b.readframes(b.getnframes())


@needs_flac
def test_stage_cancels_between_files(deployment, tmp_path):
    calls = {"n": 0}

    def cancel_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    result = stage_deployment(
        deployment, read_bd_audio_rows(deployment), tmp_path / "staging",
        is_cancelled=cancel_after_first,
    )
    assert result["cancelled"] is True
    assert result["converted"] < 3


def test_stage_requires_the_flac_encoder(deployment, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SoundHubStagingError, match="not installed"):
        stage_deployment(deployment, read_bd_audio_rows(deployment), tmp_path / "s")


def test_stage_rejects_a_missing_source_wav(deployment, tmp_path):
    for wav in (deployment / "raw_data" / "p1_BD").glob("*.wav"):
        wav.unlink()
    with pytest.raises(SoundHubStagingError, match="Source WAV not found"):
        stage_deployment(deployment, read_bd_audio_rows(deployment), tmp_path / "s")


@needs_flac
def test_stage_does_not_walk_the_event_tree(deployment, tmp_path, monkeypatch):
    """A flat layout resolves entirely from the prebuilt index.

    Walking the event tree per file is what made staging crawl at camera sites,
    so guard that the common case never calls ``rglob`` on the deployment.
    """
    original = Path.rglob
    walked: list[str] = []

    def tracked(self, pattern):
        if str(deployment) in str(self):
            walked.append(f"{self} :: {pattern}")
        return original(self, pattern)

    monkeypatch.setattr(Path, "rglob", tracked)
    stage_deployment(deployment, read_bd_audio_rows(deployment), tmp_path / "staging")
    assert not walked, f"stage walked the event tree: {walked}"


def test_pad_detector_flags_an_unpadded_odd_chunk(tmp_path):
    wav = tmp_path / "odd.wav"
    make_wav(wav)
    assert not _wav_missing_final_pad(wav)
    append_unpadded_guano(wav)
    assert _wav_missing_final_pad(wav)


def test_pad_detector_accepts_a_properly_padded_chunk(tmp_path):
    wav = tmp_path / "padded.wav"
    make_wav(wav)
    append_unpadded_guano(wav)
    # Restore the pad byte the recorder should have written.
    with open(wav, "r+b") as f:
        f.seek(4)
        riff_size = int.from_bytes(f.read(4), "little")
        f.seek(4)
        f.write((riff_size + 1).to_bytes(4, "little"))
        f.seek(0, 2)
        f.write(b"\x00")
    assert not _wav_missing_final_pad(wav)


@needs_flac
def test_stage_converts_a_wav_with_an_unpadded_guano_chunk(deployment, tmp_path):
    """The AudioMoth's unpadded trailing GUANO chunk must not abort the encode.

    Sources stay byte-identical: the pad is restored on a local copy only.
    """
    wavs = sorted((deployment / "raw_data" / "p1_BD").glob("*.wav"))
    for wav in wavs:
        append_unpadded_guano(wav)
    before = {p: p.read_bytes() for p in wavs}

    staging = tmp_path / "staging"
    result = stage_deployment(deployment, read_bd_audio_rows(deployment), staging)

    assert result["converted"] == 3
    assert {p: p.read_bytes() for p in wavs} == before
    leftovers = [p for p in staging.rglob("*") if p.name.endswith(".padded")]
    assert not leftovers


@needs_flac
def test_stage_falls_back_to_search_for_a_nested_wav(deployment, tmp_path):
    """A WAV outside the flat device folders still resolves via the fallback."""
    src = deployment / "raw_data" / "p2_BD" / "UC_StrathearnRanch_plot2_BD_20260714_00001.wav"
    nested = deployment / "raw_data" / "p2_BD" / "nested" / "deeper" / src.name
    nested.parent.mkdir(parents=True)
    src.rename(nested)
    result = stage_deployment(deployment, read_bd_audio_rows(deployment), tmp_path / "staging")
    assert result["converted"] == 3


# ---------------------------------------------------------------------------
# S3 key layout
# ---------------------------------------------------------------------------

@needs_flac
def test_staged_keys_mirror_the_local_tree(deployment, tmp_path):
    staging = tmp_path / "staging"
    rows = read_bd_audio_rows(deployment)
    stage_deployment(deployment, rows, staging)
    write_deployment_fragments(staging, rows)
    refresh_project_csvs(staging)

    settings = {
        "bucket": "casoundhub",
        "upload_prefix": "upload",
        "project_short_name": SOUNDHUB_PROJECT_SHORT_NAME,
    }
    keys = {o["key"] for o in staged_objects(staging, settings)}
    prefix = project_prefix(settings)
    assert prefix == "upload/UCNature-SSN"
    assert f"{prefix}/deployment.csv" in keys
    assert f"{prefix}/recording.csv" in keys
    assert (
        f"{prefix}/UC_StrathearnRanch_plot1_BD_20260714/"
        "UC_StrathearnRanch_plot1_BD_20260714_00001.flac"
    ) in keys
    # The fragments directory is a sibling of the project root, never uploaded.
    assert not any("fragment" in k for k in keys)


# ---------------------------------------------------------------------------
# Interaction with the Box upload
# ---------------------------------------------------------------------------

def test_soundhub_csvs_are_not_box_orphans():
    """They ride to Box with the deployment, so verification must expect them."""
    assert not is_orphan_on_box("soundhub/deployment.csv")
    assert not is_orphan_on_box("soundhub/recording.csv")
    assert is_orphan_on_box("something_unexpected.csv")
