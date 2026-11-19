# -*- coding: utf-8 -*-
"""访客能传论文进来（经中继，主持人这边导入）：Word / PPT 是 zip，压缩炸弹（几百 KB 解开几十 GB）在转换前就挡掉。
沙盒 ZUHUI_HOME，不碰真的 ~/.zuhui。上限临时调小，几 MB 就能测。

    python -m unittest tests.test_security_import -v
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ZUHUI_HOME", tempfile.mkdtemp(prefix="zuhui_sec_home_"))
sys.path.insert(0, str(ROOT))

from zuhui import papers  # noqa: E402


def _pptx(path: Path, slide_pad: int) -> Path:
    ns = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("ppt/presentation.xml", f'<p:presentation {ns}><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>')
        z.writestr("ppt/slides/slide1.xml", f'<p:sld {ns}><!--{"A" * slide_pad}--></p:sld>')
    return path


class ZipBombTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bloom_sec_zip_"))
        self._old = (papers.ZIP_PART_MAX, papers.ZIP_XML_MAX, papers.ZIP_ALL_MAX)
        papers.ZIP_PART_MAX, papers.ZIP_XML_MAX, papers.ZIP_ALL_MAX = 4_000_000, 2_000_000, 6_000_000

    def tearDown(self):
        papers.ZIP_PART_MAX, papers.ZIP_XML_MAX, papers.ZIP_ALL_MAX = self._old

    def test_bomb_refused_before_conversion(self):
        p = _pptx(self.d / "bomb.pptx", 3_000_000)          # 3 MB 的 XML 压成几 KB
        self.assertLess(p.stat().st_size, 100_000)
        with self.assertRaises(ValueError) as cm:
            papers.import_file(p)
        self.assertIn("过大", str(cm.exception))
        docx = self.d / "bomb.docx"
        with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", "<w:document>" + "B" * 3_000_000 + "</w:document>")
        with self.assertRaises(ValueError):
            papers.import_file(docx)

    def test_lying_size_header_refused(self):
        """头里把解开后的大小写小：Python 读到写的大小就停、CRC 对不上，算坏文件（不让 pandoc 去解）。"""
        p = _pptx(self.d / "liar.pptx", 1_000_000)
        raw = bytearray(p.read_bytes())
        name = b"ppt/slides/slide1.xml"
        for sig, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):       # 本地头、中央目录里的"解开后大小"
            i = 0
            while True:
                i = raw.find(sig, i)
                if i < 0:
                    break
                n = struct.unpack_from("<H", raw, i + (26 if sig == b"PK\x03\x04" else 28))[0]
                fn_at = i + (30 if sig == b"PK\x03\x04" else 46)
                if bytes(raw[fn_at:fn_at + n]) == name:
                    struct.pack_into("<I", raw, i + off, 1000)
                i += 4
        p.write_bytes(bytes(raw))
        with self.assertRaises(ValueError) as cm:
            papers._zip_precheck(p)
        self.assertIn("损坏", str(cm.exception))

    def test_normal_files_pass(self):
        papers._zip_precheck(_pptx(self.d / "ok.pptx", 10_000))
        papers._zip_precheck(self.d / "missing.docx") if (self.d / "missing.docx").exists() else None
        not_zip = self.d / "not.docx"
        not_zip.write_bytes(b"hello")
        papers._zip_precheck(not_zip)                       # 不是 zip：交给后面照旧报错


if __name__ == "__main__":
    unittest.main(verbosity=2)
