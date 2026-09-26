import hashlib
from pathlib import Path

import pytest

from yoto_mcp.media import ascii_header_filename, file_sha256, resolve_image, resolve_mp3


def test_hash_matches_browser_base64url_without_padding(tmp_path: Path):
    sample = tmp_path / "song.mp3"
    sample.write_bytes(b"ID3" + b"audio frames\x00")
    import base64

    expected = base64.urlsafe_b64encode(hashlib.sha256(sample.read_bytes()).digest()).rstrip(b"=")
    assert file_sha256(sample) == expected.decode("ascii")


def test_resolve_mp3_rejects_escaped_and_non_mp3_files(tmp_path: Path):
    root = tmp_path / "songs"
    root.mkdir()
    song = root / "a.mp3"
    song.write_bytes(b"ID3" + b"x" * 1024)
    assert resolve_mp3(root, str(song)) == song
    outside = tmp_path / "private.mp3"
    outside.write_bytes(b"ID3" + b"x" * 1024)
    (root / "link.mp3").symlink_to(outside)
    for bad in (str(outside), str(root / "link.mp3"), str(root / "not.mp3")):
        with pytest.raises(ValueError):
            resolve_mp3(root, bad)
    text = root / "fake.mp3"
    text.write_text("not an mp3")
    with pytest.raises(ValueError):
        resolve_mp3(root, str(text))


def test_resolve_image_sniffs_type_and_rejects_escapes_and_non_images(tmp_path: Path):
    root = tmp_path / "icons"
    root.mkdir()
    png = root / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    path, mime = resolve_image(root, str(png))
    assert path == png
    assert mime == "image/png"

    jpg = root / "b.jpg"
    jpg.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
    assert resolve_image(root, str(jpg))[1] == "image/jpeg"

    gif = root / "c.gif"
    gif.write_bytes(b"GIF89a" + b"x" * 32)
    assert resolve_image(root, str(gif))[1] == "image/gif"

    outside = tmp_path / "private.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    (root / "link.png").symlink_to(outside)
    for bad in (str(outside), str(root / "link.png"), str(root / "missing.png")):
        with pytest.raises(ValueError):
            resolve_image(root, bad)

    fake = root / "fake.png"
    fake.write_text("not an image")
    with pytest.raises(ValueError):
        resolve_image(root, str(fake))


def test_ascii_header_filename_folds_diacritics_and_substitutes_non_latin():
    assert ascii_header_filename("12_Médio grave & Jb no beat.mp3") == "12_Medio grave & Jb no beat.mp3"
    assert ascii_header_filename("猫咪之歌.mp3") == "upload.mp3"
    assert ascii_header_filename("plain.mp3") == "plain.mp3"
