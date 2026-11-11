import json
import os
import shutil
import tempfile
from pathlib import Path


def restrict_windows_access(path, directory=False):
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(user, ctypes.POINTER(wintypes.LPVOID))[0]
        sid_string = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            inherit = "OICI" if directory else ""
            sddl = f"D:P(A;{inherit};FA;;;{sid_string.value})(A;{inherit};FA;;;SY)"
            descriptor = wintypes.LPVOID()
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                # Set a protected DACL, replacing inherited and explicit grants.
                if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel.LocalFree(descriptor)
        finally:
            kernel.LocalFree(sid_string)
    finally:
        kernel.CloseHandle(token)


def reset_state(state_dir, clear_identity=False):
    names = ["playlist.json", "status.html", "restart.json"]
    if clear_identity:
        names += ["credentials.json", "reset.json"]
    for name in names:
        (state_dir / name).unlink(missing_ok=True)
    for name in ("browser-profile", "assets"):
        path = state_dir / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def private_directory(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        restrict_windows_access(path, directory=True)
    else:
        path.chmod(0o700)
    return path


def read_json(path):
    try:
        if os.name == "nt" and Path(path).name == "credentials.json" and Path(path).exists():
            restrict_windows_access(path)
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def write_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".pisign-", dir=path.parent)
    try:
        if os.name == "nt" and path.name == "credentials.json":
            restrict_windows_access(temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)
