"""「翻译」入口页的分配依据：拖入的路径按扩展名数一数，不读文件内容。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import create_app
from core.source_classify import classify_source_paths
from tests.app_data_isolation import IsolatedAppDataTestCase


class ClassifySourcesTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.app_data_dir.parent / "mixed"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "检查表_翻译输出_20261017").mkdir()
        for name in ("a.xlsx", "b.XLSX", "sub/c.xls", "sub/d.docx", "plan.dwg", "~$a.xlsx", "readme"):
            (self.root / name).write_bytes(b"")
        # 程序自己的输出目录不算：上次的双语结果不该把类型数歪。
        (self.root / "检查表_翻译输出_20261017" / "out.xlsx").write_bytes(b"")

    def test_folder_counts_suffixes_recursively_with_scanner_rules(self) -> None:
        result = classify_source_paths([str(self.root)])
        assert not result.truncated
        [item] = result.items
        assert item.kind == "dir"
        assert item.suffixes == {"xlsx": 2, "xls": 1, "docx": 1, "dwg": 1}

    def test_files_and_missing_paths(self) -> None:
        missing = str(self.root / "gone.pdf")
        result = classify_source_paths([str(self.root / "sub" / "d.docx"), missing])
        assert [(i.kind, i.suffixes) for i in result.items] == [("file", {"docx": 1}), ("missing", {})]

    def test_stops_counting_at_the_limit(self) -> None:
        result = classify_source_paths([str(self.root)], file_limit=2)
        assert result.truncated
        assert sum(result.items[0].suffixes.values()) <= 2

    def test_route(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/sources/classify", json={"paths": [str(self.root), str(self.root / "a.xlsx")]})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["truncated"] is False
            assert [item["kind"] for item in body["items"]] == ["dir", "file"]
            assert body["items"][1]["suffixes"] == {"xlsx": 1}
            assert client.post("/api/sources/classify", json={"paths": []}).status_code == 422
