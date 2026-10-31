"""jsDelivr / GitHub raw addresses get jsDelivr's other nodes as China-reachable fallbacks."""
from __future__ import annotations

from core.component_download import with_cdn_mirrors
from core.ocr_component_install import _tessdata

NODES = ("cdn", "gcore", "fastly", "testingcf")


def test_a_jsdelivr_address_gets_the_other_nodes_right_after_it() -> None:
    assert with_cdn_mirrors(["https://cdn.jsdelivr.net/gh/a/b@main/x.zip"]) == tuple(
        f"https://{node}.jsdelivr.net/gh/a/b@main/x.zip" for node in NODES
    )


def test_a_github_raw_address_keeps_its_place_and_gets_jsdelivr_after_it() -> None:
    urls = with_cdn_mirrors(["https://raw.githubusercontent.com/a/b/v1/dir/x.bin"])
    assert urls[0] == "https://raw.githubusercontent.com/a/b/v1/dir/x.bin"
    assert urls[1:] == tuple(f"https://{node}.jsdelivr.net/gh/a/b@v1/dir/x.bin" for node in NODES)


def test_the_same_file_is_not_listed_twice_and_other_addresses_stay_as_they_are() -> None:
    release = "https://github.com/a/b/releases/download/v1/x.zip"
    urls = with_cdn_mirrors(
        [
            "https://cdn.jsdelivr.net/gh/a/b@main/x.zip",
            "https://raw.githubusercontent.com/a/b/main/x.zip",
            release,
            "https://example.com/x.zip",
        ]
    )
    assert urls == (
        *(f"https://{node}.jsdelivr.net/gh/a/b@main/x.zip" for node in NODES),
        "https://raw.githubusercontent.com/a/b/main/x.zip",
        release,
        "https://example.com/x.zip",
    )
    assert with_cdn_mirrors([release]) == (release,)


def test_a_raw_address_without_a_file_path_is_left_alone() -> None:
    assert with_cdn_mirrors(["https://raw.githubusercontent.com/a/b"]) == ("https://raw.githubusercontent.com/a/b",)


def test_tesseract_language_data_tries_github_then_every_jsdelivr_node() -> None:
    item = _tessdata("eng", 1, "0" * 64)
    assert item.urls == (
        "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata",
        *(f"https://{node}.jsdelivr.net/gh/tesseract-ocr/tessdata_fast@4.1.0/eng.traineddata" for node in NODES),
    )
