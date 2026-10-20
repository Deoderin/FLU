from fleet.params import diff, expected_params, ignored, parse_parm


def test_parse_mission_planner_and_qgc_formats():
    text = """
# comment
FS_ACTION,2
SYSID_THISMAV 3
1\t1\tCOMPASS_OFS_X\t12.5\t9
"""
    assert parse_parm(text) == {"FS_ACTION": 2.0, "SYSID_THISMAV": 3.0, "COMPASS_OFS_X": 12.5}


def test_calibration_is_ignored():
    patterns = ["COMPASS_OFS*", "INS_ACC*OFFS_*", "RC*_MIN"]
    assert ignored("COMPASS_OFS2_Y", patterns)
    assert ignored("INS_ACC2OFFS_Z", patterns)
    assert ignored("RC3_MIN", patterns)
    assert not ignored("RC3_OPTION", patterns)


def test_expected_overrides_base_and_drops_calibration():
    base = {"FS_ACTION": 1, "COMPASS_OFS_X": 10, "WP_SPEED": 2}
    exp = expected_params(base, {"FS_ACTION": 2}, ["COMPASS_OFS*"])
    assert exp == {"FS_ACTION": 2, "WP_SPEED": 2}


def test_diff_tolerates_float32_and_reports_missing():
    expected = {"WP_SPEED": 0.1, "FS_ACTION": 2, "NET_ENABLE": 1}
    actual = {"WP_SPEED": 0.100000001490116, "FS_ACTION": 0}
    assert diff(expected, actual) == [("FS_ACTION", 2, 0), ("NET_ENABLE", 1, None)]
