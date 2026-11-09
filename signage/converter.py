"""Stateless document rasterizer, deployed without storage or admin credentials."""

import ctypes
import errno
import io
import os
import signal
import socket
import subprocess
import tempfile
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 50


def restrict_process():
    import resource

    # Linux-only and fail-closed: conversion must never have network access.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError("Cannot restrict converter privileges.")
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint), ("op", ctypes.c_int),
            ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
        ]

    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Comparison),
    ]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError("Cannot initialize the converter sandbox.")
    try:
        deny = 0x00050000 | errno.EPERM
        syscall = seccomp.seccomp_syscall_resolve_name(b"socket")
        # LibreOffice uses local IPC. Deny every other address family, including IP.
        comparison = Comparison(0, 1, socket.AF_UNIX, 0)  # SCMP_CMP_NE
        if seccomp.seccomp_rule_add_array(context, deny, syscall, 1, ctypes.byref(comparison)) != 0:
            raise OSError("Cannot restrict converter networking.")
        for name in (
            b"ptrace", b"process_vm_readv", b"process_vm_writev", b"pidfd_getfd",
            b"setsid", b"setpgid", b"unshare", b"setns",
            b"io_uring_setup", b"io_uring_enter", b"io_uring_register",
        ):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if syscall >= 0 and seccomp.seccomp_rule_add(context, deny, syscall, 0) != 0:
                raise OSError("Cannot restrict converter process access.")
        if seccomp.seccomp_load(context) != 0:
            raise OSError("Cannot activate the converter sandbox.")
    finally:
        seccomp.seccomp_release(context)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def run_conversion(command, directory, timeout):
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(directory),
        "TMPDIR": str(directory), "LANG": "C.UTF-8",
    }
    with subprocess.Popen(
        command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, preexec_fn=restrict_process,
    ) as process:
        try:
            if process.wait(timeout=timeout) != 0:
                raise ValueError("Document conversion failed.")
        finally:
            # Terminate any conversion descendants, including on timeout.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def render_document(source, directory):
    with source.open("rb") as handle:
        header = handle.read(8)
    if source.suffix == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF signature.")
    elif source.suffix in (".doc", ".ppt"):
        if header != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("Invalid Office document signature.")
    else:
        required = "word/document.xml" if source.suffix == ".docx" else "ppt/presentation.xml"
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if required not in names or "[Content_Types].xml" not in names or any(
                name.lower().endswith("vbaproject.bin") for name in names
            ):
                raise ValueError("Invalid or macro-enabled Office document.")
    if source.suffix == ".pdf":
        pdf = source
    else:
        profile = directory / "profile"
        profile.mkdir()
        (profile / "user").mkdir()
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Common/Load">'
            '<prop oor:name="UpdateLinks" oor:op="fuse"><value>0</value></prop></item>'
            '</oor:items>', encoding="utf-8",
        )
        run_conversion([
            "libreoffice", f"-env:UserInstallation={profile.as_uri()}", "--headless",
            "--nologo", "--nodefault", "--norestore", "--convert-to", "pdf",
            "--outdir", str(directory), str(source),
        ], directory, timeout=90)
        pdf = source.with_suffix(".pdf")
        if not pdf.is_file():
            raise ValueError("The document could not be exported to PDF.")
    pages = directory / "pages"
    pages.mkdir()
    run_conversion([
        "pdftoppm", "-f", "1", "-l", str(MAX_PAGES + 1), "-scale-to", "1920",
        "-png", str(pdf), str(pages / "page"),
    ], directory, timeout=60)
    paths = sorted(pages.glob("page-*.png"), key=lambda path: int(path.stem.split("-")[-1]))
    if not 1 <= len(paths) <= MAX_PAGES:
        raise ValueError("Documents must contain 1 to 50 pages.")
    output = directory / "rendered.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for number, path in enumerate(paths, 1):
            with Image.open(path) as image:
                image.load()
                page = io.BytesIO()
                image.convert("RGB").save(page, format="PNG")
                archive.writestr(f"page-{number}.png", page.getvalue())
            if output.stat().st_size > MAX_BYTES:
                raise ValueError("The rendered document is too large.")
    return output


class ConverterHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        suffix = self.path.removeprefix("/convert/")
        if self.path != f"/convert/{suffix}" or suffix not in ("pdf", "doc", "docx", "ppt", "pptx"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BYTES or self.headers.get("Transfer-Encoding"):
            self.send_error(413, "A document up to 100 MiB with Content-Length is required.")
            return
        self.connection.settimeout(30)
        with tempfile.TemporaryDirectory(prefix="pisign-document-") as temporary:
            directory = Path(temporary)
            source = directory / f"source.{suffix}"
            with source.open("wb") as handle:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        self.send_error(400, "Incomplete document.")
                        return
                    remaining -= len(chunk)
                    handle.write(chunk)
            try:
                output = render_document(source, directory)
            except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile):
                self.log_error("Document conversion failed.")
                self.send_error(422, "Document could not be rendered.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(output.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with output.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    self.wfile.write(chunk)

    def do_GET(self):
        if self.path != "/health/":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), ConverterHandler).serve_forever()
