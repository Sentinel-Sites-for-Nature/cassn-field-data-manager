"""Camera identity checks must cover the whole card, including missing evidence."""

import pytest

from cassn.core.quality_control import check_camera_serial


def image(serial="4LPXKT08021269", expected="08021269", filename="RCNX0001.JPG"):
    return {
        "file_type": "image",
        "camera_serial_exif": serial,
        "camera_id": expected,
        "original_filename": filename,
    }


@pytest.mark.parametrize("serial, expected", [
    ("4LPXKT08021269", "08021269"),
    ("4LPXKT08021269", "4LPXKT08021269"),
    (" 4lpxkt08021269 ", " 4LPXKT08021269 "),
])
def test_matching_camera_passes(serial, expected):
    assert check_camera_serial([image(serial, expected)], "p1_ML") == []


def test_mixed_camera_is_found_after_matching_images_and_summarized():
    entries = [image(), image(filename="RCNX0002.JPG"),
               image("4LPXKT08021987", filename="RCNX0003.JPG")]
    warnings = check_camera_serial(entries, "p1_ML")
    assert len(warnings) == 1
    message = warnings[0]
    assert "p1_ML: Mixed-camera card" in message
    assert "4LPXKT08021269: 2 image(s), matches expected camera 08021269" in message
    assert "4LPXKT08021987: 1 image(s), differs from expected camera 08021269" in message
    assert "RCNX0003.JPG" in message


def test_one_wrong_camera_warns_without_claiming_mixed_card():
    message = check_camera_serial([image("4LPXKT08021987")], "p1_ML")[0]
    assert "Camera serial mismatch" in message
    assert "Mixed-camera" not in message


@pytest.mark.parametrize("serial, expected", [
    ("4LPXKT08021269EXTRA", "08021269"),
    ("4LPXKT08021269", "21269"),
    ("PREFIX4LPXKT08021269", "4LPXKT08021269"),
])
def test_only_full_serial_or_eight_digit_suffix_matches(serial, expected):
    assert "Camera serial mismatch" in check_camera_serial(
        [image(serial, expected)], "p1_ML"
    )[0]


def test_missing_serials_are_counted_even_when_other_images_match():
    message = check_camera_serial([image(), image(""), image(None)], "p1_ML")[0]
    assert "2 image(s) have no readable camera serial" in message
    assert "mismatch" not in message


def test_no_readable_serial_is_unverified():
    message = check_camera_serial([image("")], "p1_ML")[0]
    assert "Unable to verify camera identity" in message


def test_missing_expected_id_cannot_pass():
    message = check_camera_serial([image(expected="")], "p1_ML")[0]
    assert "Unable to compare 1 image(s): expected camera ID missing" in message


def test_each_images_expected_id_is_checked():
    message = check_camera_serial(
        [image(), image(expected="08021987")], "p1_ML"
    )[0]
    assert "differs from expected camera 08021987" in message


def test_serial_normalization_does_not_create_false_mixed_camera_warning():
    assert check_camera_serial([image(), image(" 4lpxkt08021269 ")], "p1_ML") == []


@pytest.mark.parametrize("entries", [[], [{"file_type": "audio"}], [{"file_type": "config"}]])
def test_non_images_are_outside_check(entries):
    assert check_camera_serial(entries, "p1_BD") == []
