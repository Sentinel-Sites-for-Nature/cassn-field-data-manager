"""Camera wall-clock resolution and prospective metadata serialization."""
import copy
import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cassn.config import IMAGE_FIELDS, IMAGE_TIMESTAMP_FIELDS
from cassn.core.image_metadata import (
    camera_timestamp_fields, parse_camera_recorded_datetime, resolve_camera_timestamp,
)
from cassn.export.metadata_csv import build_metadata_rows


@pytest.mark.parametrize('clock,offset,effective,utc,status', [
    ('2026:01:15 12:00:00', '-08:00', '-08:00', '2026-01-15T20:00:00Z', 'offset_matches'),
    ('2026:07:15 12:00:00', '-08:00', '-07:00', '2026-07-15T19:00:00Z', 'offset_mismatch_corrected'),
    ('2026:07:15 12:00:00', '-07:00', '-07:00', '2026-07-15T19:00:00Z', 'offset_matches'),
    ('2026:07:15 12:00:00', '', '-07:00', '2026-07-15T19:00:00Z', 'offset_missing'),
    ('2026:07:15 12:00:00', '-8:00', '-07:00', '2026-07-15T19:00:00Z', 'offset_invalid'),
    ('2026:07:15 12:00:00', '-08:60', '-07:00', '2026-07-15T19:00:00Z', 'offset_invalid'),
    ('2026:03:08 01:59:59', '-08:00', '-08:00', '2026-03-08T09:59:59Z', 'offset_matches'),
    ('2026:03:08 03:00:00', '-08:00', '-07:00', '2026-03-08T10:00:00Z', 'offset_mismatch_corrected'),
    ('2026:03:08 02:30:00', '-08:00', '', '', 'nonexistent_capture_time'),
    ('2026:03:08 02:30:00', '', '', '', 'nonexistent_capture_time'),
    ('2026:11:01 00:59:59', '-08:00', '-07:00', '2026-11-01T07:59:59Z', 'offset_mismatch_corrected'),
    ('2026:11:01 01:30:00', '-08:00', '', '', 'ambiguous_capture_time'),
    ('2026:11:01 01:30:00', '-07:00', '', '', 'ambiguous_capture_time'),
    ('2026:11:01 01:30:00', '', '', '', 'ambiguous_capture_time'),
    ('2026:11:01 02:00:00', '-08:00', '-08:00', '2026-11-01T10:00:00Z', 'offset_matches'),
    ('2026:07:15 23:30:00', '-08:00', '-07:00', '2026-07-16T06:30:00Z', 'offset_mismatch_corrected'),
    ('2026:02:30 12:00:00', '-08:00', '', '', 'invalid_capture_time'),
    ('not a date', '-08:00', '', '', 'invalid_capture_time'),
    ('', '', '', '', 'invalid_capture_time'),
])
def test_camera_resolution(clock, offset, effective, utc, status):
    exif = {'DateTimeOriginal': clock, 'OffsetTimeOriginal': offset}
    before = dict(exif)
    result = resolve_camera_timestamp(exif)
    assert result.status == status
    assert result.effective_offset == effective
    assert result.recorded_datetime_utc == utc
    assert parse_camera_recorded_datetime(exif) == result.recorded_datetime
    if utc:
        # The selected local clock must remain exactly the original wall clock.
        local = datetime.fromisoformat(result.recorded_datetime)
        assert local.strftime('%Y:%m:%d %H:%M:%S') == clock
        assert local.astimezone(timezone.utc) == datetime.fromisoformat(utc)
        assert result.check.severity == ('pass' if status == 'offset_matches' else 'warning')
    else:
        assert result.recorded_datetime == ''
        assert result.check.severity == 'error'
    assert exif == before
    assert resolve_camera_timestamp(exif) == result


def test_precedence_fallback_and_exact_evidence():
    exif = {'DateTimeOriginal': '2026:07:15 12:00:00',
            'DateTime': '2020:01:01 00:00:00', 'OffsetTimeOriginal': ' -08:00 '}
    fields = camera_timestamp_fields(exif)
    assert fields['camera_datetime_original'] == exif['DateTimeOriginal']
    assert fields['camera_offset_time_original'] == ' -08:00 '
    assert fields['camera_datetime_source'] == 'DateTimeOriginal'
    assert fields['recorded_datetime_utc'] == '2026-07-15T19:00:00Z'
    exif['DateTimeOriginal'] = 'invalid'
    assert resolve_camera_timestamp(exif).recorded_datetime == ''
    del exif['DateTimeOriginal']
    fields = camera_timestamp_fields(exif)
    assert fields['camera_datetime_source'] == 'DateTime'
    assert fields['camera_datetime_original'] == exif['DateTime']
    assert 'datetime_fallback' in fields['camera_timestamp_status']
    assert fields['camera_timestamp_severity'] == 'warning'
    assert parse_camera_recorded_datetime({}) == ''


def test_csv_preserves_resolution_and_does_not_recalculate_historical_rows():
    placement = {'deployment_id': 'camera', 'deployment_start_date': '2026-01-01',
                 'deployment_end_date': '2026-12-31'}
    lookups = SimpleNamespace(soundhub_config={}, wi_config={},
                              deployment_for_id=lambda _: placement)
    metadata = {'deployment_event_id': 'event', 'site_short_name': 'Test'}
    original = {'DateTimeOriginal': '2026:07:15 12:00:00', 'OffsetTimeOriginal': '-08:00'}
    new = {'file_type': 'image', 'deployment_id': 'camera', 'device_type': 'ML',
           'new_filename': 'new.jpg', **camera_timestamp_fields(original)}
    legacy = {'file_type': 'image', 'deployment_id': 'camera', 'device_type': 'ML',
              'new_filename': 'old.jpg', 'recorded_datetime': '2026-07-15T13:00:00-07:00',
              'camera_datetime_original': original['DateTimeOriginal'],
              'camera_offset_time_original': '-08:00'}
    rows = [new, legacy]
    before = copy.deepcopy(rows)
    images, _ = build_metadata_rows(metadata, rows, lookups)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=IMAGE_FIELDS)
    writer.writeheader()
    writer.writerows(images)
    restored = list(csv.DictReader(io.StringIO(output.getvalue())))
    for field in ['recorded_datetime', *IMAGE_TIMESTAMP_FIELDS]:
        assert restored[0][field] == new[field]
    assert restored[1]['recorded_datetime'] == legacy['recorded_datetime']
    assert restored[1]['recorded_datetime_utc'] == ''
    assert restored[1]['camera_timestamp_source'] == ''
    assert rows == before
    # Re-export only projects saved fields, even for mixed old/new inventories.
    again, _ = build_metadata_rows(metadata, rows, lookups)
    for index in range(2):
        assert {key: again[index][key] for key in IMAGE_TIMESTAMP_FIELDS} == {
            key: images[index][key] for key in IMAGE_TIMESTAMP_FIELDS}


@pytest.mark.parametrize('clock', ['2026:07:15 12:00:00', 'invalid', '2026:11:01 01:30:00'])
def test_explicit_reinventory_is_idempotent_and_clears_stale_times(tmp_path, monkeypatch, clock):
    from utils import fix_camera_clock

    image = tmp_path / 'image.jpg'
    contents = b'original bytes'
    image.write_bytes(contents)
    exif = {'DateTimeOriginal': clock, 'OffsetTimeOriginal': '-08:00'}
    monkeypatch.setattr(fix_camera_clock, 'extract_exif_data', lambda _: (exif, {}))
    row = {'new_filename': image.name, 'recorded_datetime': '2026-07-15T13:00:00-07:00',
           'recorded_datetime_utc': '2026-07-15T20:00:00Z'}
    assert fix_camera_clock.reinventory([row], tmp_path) == 1
    for key, value in camera_timestamp_fields(exif).items():
        assert row[key] == value
    before = dict(row)
    assert fix_camera_clock.reinventory([row], tmp_path) == 1
    assert row == before
    assert image.read_bytes() == contents


def test_capture_time_outside_supported_utc_range_is_unresolved():
    result = resolve_camera_timestamp({'DateTimeOriginal': '9999:12:31 23:59:59'})
    assert result.status == 'invalid_capture_time'
    assert result.recorded_datetime == result.recorded_datetime_utc == ''
