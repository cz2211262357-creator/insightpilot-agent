from pathlib import Path

from tests.worker_process import write_marker


def test_marker_appears_only_after_complete_content_is_written(tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    original = Path.write_text
    observed = []

    def inspect_write(path, value, *args, **kwargs):
        observed.append((path, marker.exists(), value))
        return original(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", inspect_write)
    write_marker(marker, "12345")
    assert observed and observed[0][0] != marker and not observed[0][1]
    assert marker.read_text(encoding="utf-8") == "12345"
