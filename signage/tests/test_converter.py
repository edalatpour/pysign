import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from signage.converter import render_document, run_conversion


@unittest.skipUnless(
    sys.platform == "linux" and shutil.which("libreoffice") and shutil.which("pdftoppm"),
    "Document integration tests require Linux, LibreOffice, Poppler and libseccomp.",
)
class DocumentConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def assert_pages(self, result, count):
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.namelist(), [f"page-{n}.png" for n in range(1, count + 1)])
            for name in archive.namelist():
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    self.assertLessEqual(max(image.size), 1920)
                    self.assertEqual(image.format, "PNG")

    def write_docx(self, path):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>')
            archive.writestr("_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>')
            archive.writestr("word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>PiSign private document</w:t></w:r></w:p>'
                '<w:p><w:r><w:br w:type="page"/><w:t>Second page</w:t></w:r></w:p>'
                '</w:body></w:document>')

    def test_pdf_rasterizes_all_pages_without_original_content(self):
        source = self.directory / "source.pdf"
        Image.new("RGB", (200, 100), "red").save(
            source, "PDF", save_all=True, append_images=[Image.new("RGB", (200, 100), "blue")],
        )
        self.assert_pages(render_document(source, self.directory), 2)

    def test_internal_http_converter_accepts_documents_and_rejects_bad_inputs(self):
        process = subprocess.Popen([
            sys.executable, "-c",
            "from http.server import HTTPServer;from signage.converter import ConverterHandler;"
            "server=HTTPServer(('127.0.0.1',0),ConverterHandler);"
            "print(server.server_port,flush=True);server.serve_forever()",
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            origin = f"http://127.0.0.1:{int(process.stdout.readline())}"
            with urlopen(origin + "/health/", timeout=5) as response:
                self.assertEqual(response.read(), b"ok")
            source = io.BytesIO()
            Image.new("RGB", (200, 100), "red").save(source, "PDF")
            request = Request(origin + "/convert/pdf", data=source.getvalue())
            with urlopen(request, timeout=30) as response:
                output = self.directory / "result.zip"
                output.write_bytes(response.read())
                self.assertEqual(response.headers["Content-Type"], "application/zip")
                self.assert_pages(output, 1)
            for path, data, status in [
                ("/convert/html", b"<h1>Unsupported</h1>", 404),
                ("/convert/pdf", b"<h1>Renamed HTML</h1>", 422),
                ("/convert/docx", b"", 413),
            ]:
                with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                    urlopen(Request(origin + path, data=data), timeout=5)
                self.assertEqual(error.exception.code, status)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_word_docx_conversion_preserves_page_count(self):
        source = self.directory / "source.docx"
        self.write_docx(source)
        self.assert_pages(render_document(source, self.directory), 2)

    def test_documents_over_fifty_pages_are_rejected(self):
        source = self.directory / "source.pdf"
        page = Image.new("RGB", (16, 9), "red")
        page.save(source, "PDF", save_all=True, append_images=[page] * 50)
        with self.assertRaisesRegex(ValueError, "1 to 50 pages"):
            render_document(source, self.directory)

    def test_word_legacy_doc_conversion(self):
        source = self.directory / "input.docx"
        self.write_docx(source)
        run_conversion([
            "libreoffice", "--headless", "--convert-to", "doc", "--outdir",
            str(self.directory), str(source),
        ], self.directory, 90)
        self.assert_pages(render_document(source.with_suffix(".doc"), self.directory), 2)

    def test_powerpoint_pptx_and_legacy_ppt_conversion(self):
        source = self.directory / "input.fodp"
        source.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
            'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
            'office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.presentation">'
            '<office:body><office:presentation>'
            '<draw:page draw:name="One"><draw:frame><draw:text-box><text:p>Slide one</text:p>'
            '</draw:text-box></draw:frame></draw:page>'
            '<draw:page draw:name="Two"><draw:frame><draw:text-box><text:p>Slide two</text:p>'
            '</draw:text-box></draw:frame></draw:page>'
            '</office:presentation></office:body></office:document>', encoding="utf-8",
        )
        for extension in ("pptx", "ppt"):
            with self.subTest(extension=extension):
                work = self.directory / extension
                work.mkdir()
                run_conversion([
                    "libreoffice", "--headless", "--convert-to", extension, "--outdir",
                    str(work), str(source),
                ], self.directory, 90)
                self.assert_pages(render_document(work / f"input.{extension}", work), 2)

    def test_renamed_html_and_macro_enabled_documents_are_rejected(self):
        for suffix in ("pdf", "doc", "ppt", "docx", "pptx"):
            source = self.directory / f"bad.{suffix}"
            source.write_bytes(b"<html><script>alert(1)</script></html>")
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, zipfile.BadZipFile)):
                render_document(source, self.directory)
        source = self.directory / "macro.docx"
        self.write_docx(source)
        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("word/vbaProject.bin", b"macro")
        with self.assertRaisesRegex(ValueError, "macro-enabled"):
            render_document(source, self.directory)

    def test_network_is_denied_and_admin_environment_is_not_inherited(self):
        report = self.directory / "report"
        code = (
            "import errno,os,socket;from pathlib import Path;"
            "assert 'PISIGN_TEST_ADMIN_SECRET' not in os.environ;"
            "s=socket.socket(socket.AF_UNIX);s.close();"
            "\ntry: socket.socket(socket.AF_INET)\n"
            "except PermissionError: Path('report').write_text('network denied')\n"
            "else: raise RuntimeError('network was allowed')\n"
        )
        with patch.dict(os.environ, {"PISIGN_TEST_ADMIN_SECRET": "not-a-real-secret"}):
            run_conversion([sys.executable, "-c", code], self.directory, 5)
        self.assertEqual(report.read_text(), "network denied")

    def test_conversion_timeout_terminates_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_conversion([sys.executable, "-c", "import time; time.sleep(30)"], self.directory, 0.1)
