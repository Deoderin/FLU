"""Камери НРК у налаштуваннях Termite QGroundControl."""
import pytest

from fleet.termite import TermiteError, ini_value, update_ini, write_cameras

STREAMS = [
    {"cam": "cam1", "ip": "10.150.16.24", "rtsp": "rtsp://admin:a12345677@10.150.16.24:554/cam/realmonitor?channel=1&subtype=0",
     "rtsp2": "rtsp://admin:a12345677@10.150.16.24:554/cam/realmonitor?channel=1&subtype=1"},
    {"cam": "cam2", "ip": "10.150.16.25", "rtsp": "rtsp://admin:a12345677@10.150.16.25:554/cam/realmonitor?channel=1&subtype=0",
     "rtsp2": "rtsp://admin:a12345677@10.150.16.25:554/cam/realmonitor?channel=1&subtype=1"},
]
# Уривок справжнього файлу Termite: CRLF, значення з комою в лапках
SETTINGS = ('[General]\r\nfirstRunPromptIdsShown="1,2"\r\n\r\n[TermitCamera1]\r\nisEnabled=false\r\n'
            'rtspUrl=rtsp://old\r\naspectRatio=1.777777\r\n\r\n[TermitVideo]\r\nvideoSource=RTSP Video Stream\r\n\r\n'
            '[Units]\r\ndistanceUnits=1\r\n')
NO_TERMITE = {"paths": [], "running": False}


def test_qsettings_quoting():
    assert ini_value(True) == "true"
    assert ini_value("RTSP Video Stream") == "RTSP Video Stream"
    assert ini_value("rtsp://h/x?channel=1&subtype=0") == '"rtsp://h/x?channel=1&subtype=0"'  # «=» — у лапках


def test_cameras_written_other_settings_kept(tmp_path):
    path = tmp_path / "Termit QGroundControl Daily.ini"
    path.write_bytes(SETTINGS.encode())
    result = write_cameras(STREAMS, path=path, installs=NO_TERMITE)
    text = path.read_bytes().decode()
    assert result == {"path": str(path), "warnings": []}
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")  # кінці рядків як у Termite
    assert 'firstRunPromptIdsShown="1,2"' in text and "distanceUnits=1" in text and "aspectRatio=1.777777" in text
    cam1 = text.split("[TermitCamera1]")[1].split("[")[0]
    assert "isEnabled=true" in cam1 and "rtsp://old" not in cam1 and "visibleName=front" in cam1
    # rtspt:// - відео по TCP: через тунель RTP по UDP до Termite не доходить, а сам на TCP він не перемикається
    assert 'rtspUrl="rtspt://admin:a12345677@10.150.16.24:554/cam/realmonitor?channel=1&subtype=0"' in cam1
    assert cam1.split("secondaryRtspUrl=")[1].startswith('"rtspt://') and "subtype=1" in cam1
    assert "[TermitCamera2]" in text and "visibleName=back" in text and "lowLatencyMode=true" in text
    assert text.count("[TermitVideo]") == 1 and text.count("videoSource=") == 1
    assert update_ini(text, {}) == text  # повторний запис нічого не ламає


def test_fresh_settings_file(tmp_path):
    """Termite ще не запускали: файл створюється з потрібними розділами."""
    path = tmp_path / "tencore.org" / "Termit QGroundControl Daily.ini"
    write_cameras(STREAMS, path=path, installs=NO_TERMITE)
    assert path.read_bytes().decode().startswith("[TermitVideo]\r\n")


def test_open_termite_and_cyrillic_folder(tmp_path):
    path = tmp_path / "t.ini"
    with pytest.raises(TermiteError, match="закрийте Termite"):
        write_cameras(STREAMS, path=path, installs={"paths": [], "running": True})
    assert not path.exists()
    result = write_cameras(STREAMS, path=path, installs={"paths": ["C:\\Users\\РУЛЕТКА\\Termit\\QGroundControl.exe"], "running": False})
    assert "C:\\Termit" in result["warnings"][0] and "РУЛЕТКА" in result["warnings"][0]
