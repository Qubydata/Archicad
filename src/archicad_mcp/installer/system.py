"""Everything the installer needs to know about the computer it runs on.

Windows and macOS are the real targets (Archicad runs on nothing else). Linux
gets a harmless layout under the home folder so the installer can be developed
and tested there.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "Archicad MCP"
APP_ID = "ArchicadMCP"
PUBLISHER = "Archicad MCP"
SUPPORTED_VERSIONS = range(25, 31)  # what the Tapir add-on supports
TAPIR_RELEASES = "https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest/download"
UV_RELEASES = "https://github.com/astral-sh/uv/releases/latest/download"
LAUNCH_AGENT_LABEL = "com.archicadmcp.panel"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"


@dataclass
class ArchicadInstall:
    version: int
    path: str
    detected: bool

    def to_dict(self) -> dict:
        return asdict(self)


class InstallerError(Exception):
    """A step failed in a way the user should be told about."""


class System:
    """Paths and operating system actions, overridable in tests."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or Path.home()
        self.platform = sys.platform

    # --- where things go ----------------------------------------------------

    @property
    def is_windows(self) -> bool:
        return self.platform == "win32"

    @property
    def is_mac(self) -> bool:
        return self.platform == "darwin"

    @property
    def install_dir(self) -> Path:
        if self.is_windows:
            base = Path(os.environ.get("LOCALAPPDATA", self.home / "AppData" / "Local"))
            return base / "Programs" / APP_NAME
        if self.is_mac:
            return self.home / "Applications" / f"{APP_NAME}.app"
        return self.home / ".local" / "share" / "archicad-mcp" / "app"

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("ARCHICAD_MCP_HOME", self.home / ".archicad-mcp"))

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "install.json"

    @property
    def documents_dir(self) -> Path:
        if self.is_windows:
            known = _windows_documents_folder()
            if known:
                return known
        return self.home / "Documents"

    @property
    def tapir_scripts_dir(self) -> Path:
        """The folder whose scripts the Tapir palette in Archicad lists."""
        return self.documents_dir / "Tapir" / "custom-scripts"

    @property
    def claude_config_path(self) -> Path:
        if self.is_windows:
            return Path(os.environ.get("APPDATA", self.home / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
        if self.is_mac:
            return self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return self.home / ".config" / "Claude" / "claude_desktop_config.json"

    @property
    def start_menu_shortcut(self) -> Path:
        base = Path(os.environ.get("APPDATA", self.home / "AppData" / "Roaming"))
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"

    @property
    def launch_agent_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    @property
    def uv_dir(self) -> Path:
        return self.home / ".local" / "bin"

    # --- executables --------------------------------------------------------

    @property
    def frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    def installed_executable(self, name: str) -> Path:
        if self.is_mac:
            return self.install_dir / "Contents" / "MacOS" / name
        return self.install_dir / (f"{name}.exe" if self.is_windows else name)

    def app_command(self, *args: str) -> list[str]:
        """Command line of the installed Archicad MCP app (the panel and service)."""
        if self.frozen:
            return [str(self.installed_executable("ArchicadMCP")), *args]
        return [sys.executable, "-m", "archicad_mcp.launcher", *args]

    def server_command(self) -> list[str]:
        """Command line of the stdio MCP server, for Claude Desktop and other MCP clients."""
        if self.frozen:
            return [str(self.installed_executable("archicad-mcp-server"))]
        return [sys.executable, "-m", "archicad_mcp.launcher", "mcp"]

    def has_uv(self) -> bool:
        exe = "uv.exe" if self.is_windows else "uv"
        return (self.uv_dir / exe).exists() or shutil.which("uv") is not None

    def uv_asset(self) -> str:
        arm = platform.machine().lower() in ("arm64", "aarch64")
        if self.is_windows:
            return f"uv-{'aarch64' if arm else 'x86_64'}-pc-windows-msvc.zip"
        if self.is_mac:
            return f"uv-{'aarch64' if arm else 'x86_64'}-apple-darwin.tar.gz"
        return f"uv-{'aarch64' if arm else 'x86_64'}-unknown-linux-gnu.tar.gz"

    def tapir_installer_asset(self) -> str:
        return "TapirInstaller_Win.exe" if self.is_windows else "TapirInstaller_Mac.zip"

    # --- Archicad -----------------------------------------------------------

    def archicad_executable(self, install: ArchicadInstall) -> Path:
        folder = Path(install.path)
        if self.is_windows:
            return folder / "Archicad.exe"
        if self.is_mac:
            return folder / f"Archicad {install.version}.app"
        return folder / "Archicad"

    def default_archicad_path(self, version: int) -> str:
        if self.is_windows:
            return rf"C:\Program Files\Graphisoft\Archicad {version}" + "\\"
        if self.is_mac:
            return f"/Applications/GRAPHISOFT/Archicad {version}/"
        return str(self.home / "GRAPHISOFT" / f"Archicad {version}") + "/"

    def detect_archicad(self) -> list[ArchicadInstall]:
        """Find installed Archicad versions the add-on supports, newest first."""
        found: dict[int, str] = {}
        candidates: list[Path] = []
        if self.is_windows:
            for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"}:
                candidates += [Path(base) / "Graphisoft", Path(base) / "GRAPHISOFT"]
            for version, location in _windows_uninstall_entries():
                if version in SUPPORTED_VERSIONS and (Path(location) / "Archicad.exe").exists():
                    found.setdefault(version, location)
        elif self.is_mac:
            candidates += [Path("/Applications/GRAPHISOFT"), self.home / "Applications" / "GRAPHISOFT"]
        else:
            candidates += [self.home / "GRAPHISOFT"]
        for base in candidates:
            if not base.is_dir():
                continue
            for folder in base.iterdir():
                match = re.fullmatch(r"Archicad (\d\d)(?: .*)?", folder.name, flags=re.IGNORECASE)
                if not match or int(match.group(1)) not in SUPPORTED_VERSIONS:
                    continue
                install = ArchicadInstall(int(match.group(1)), str(folder), True)
                if self.archicad_executable(install).exists():
                    found.setdefault(install.version, str(folder))
        return [ArchicadInstall(v, _with_separator(p), True) for v, p in sorted(found.items(), reverse=True)]

    def version_from_path(self, path: str, fallback: int) -> int:
        match = re.search(r"Archicad (\d\d)", path, flags=re.IGNORECASE)
        return int(match.group(1)) if match and int(match.group(1)) in SUPPORTED_VERSIONS else fallback

    def addons_folder(self, install: ArchicadInstall) -> Path:
        return Path(install.path) / "Add-Ons"

    def open_archicad(self, install: ArchicadInstall) -> None:
        exe = self.archicad_executable(install)
        if self.is_mac:
            subprocess.Popen(["open", "-a", str(exe)])
        else:
            spawn_detached([str(exe)])

    # --- processes ----------------------------------------------------------

    def run(self, command: list[str], timeout: float = 900) -> int:
        kwargs = {"creationflags": 0x08000000} if self.is_windows else {}  # CREATE_NO_WINDOW
        completed = subprocess.run(command, capture_output=True, timeout=timeout, **kwargs)
        return completed.returncode

    def run_elevated(self, command: list[str], timeout: float = 900) -> int:
        """Run a command with administrator rights (asks the user)."""
        if self.is_windows:
            return _windows_run_elevated(command, timeout)
        if self.is_mac:
            shell = " ".join(_sh_quote(part) for part in command)
            script = f'do shell script "{_applescript_quote(shell)}" with administrator privileges'
            return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=timeout).returncode
        return self.run(command, timeout)

    def stop_running_app(self) -> None:
        """Stop a running Archicad MCP service so its files can be replaced."""
        import json

        session = self.data_dir / "session.json"
        try:
            pid = json.loads(session.read_text(encoding="utf8")).get("pid")
        except (OSError, ValueError):
            pid = None
        if pid and pid != os.getpid():
            if self.is_windows:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=0x08000000)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            time.sleep(0.5)
        if self.is_windows:
            subprocess.run(
                ["taskkill", "/F", "/IM", "ArchicadMCP.exe", "/FI", f"PID ne {os.getpid()}"],
                capture_output=True, creationflags=0x08000000,
            )


def spawn_detached(command: list[str]) -> None:
    """Start a process that outlives the installer and has no console window."""
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(command, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(command, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _with_separator(path: str) -> str:
    return path if path.endswith(("/", "\\")) else path + ("\\" if "\\" in path else "/")


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _applescript_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# --- Windows only -----------------------------------------------------------


def _windows_documents_folder() -> Path | None:
    """The real Documents folder (it may be redirected, for example to OneDrive)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", wintypes.BYTE * 8)]

        documents = GUID(0xFDD39AD0, 0x238F, 0x46AF, (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(documents), 0, None, ctypes.byref(path)) == 0:
            result = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return result
    except Exception:
        pass
    return None


def _windows_uninstall_entries() -> list[tuple[int, str]]:
    entries = []
    try:
        import winreg
    except ImportError:
        return entries
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0,
                                  winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        name = winreg.QueryValueEx(key, "DisplayName")[0]
                        location = winreg.QueryValueEx(key, "InstallLocation")[0]
                except OSError:
                    continue
                match = re.match(r"Archicad (\d\d)\b", str(name), flags=re.IGNORECASE)
                if match and location:
                    entries.append((int(match.group(1)), str(location)))
    return entries


def _windows_run_elevated(command: list[str], timeout: float) -> int:
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = command[0]
    info.lpParameters = subprocess.list2cmdline(command[1:])
    info.nShow = 0  # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.GetLastError()
        if error == 1223:  # ERROR_CANCELLED: the user said no to the permission prompt
            raise InstallerError("Administrator permission was not given, so the Archicad add-on was not installed.")
        raise InstallerError(f"Could not start {Path(command[0]).name} (Windows error {error}).")
    try:
        if ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise InstallerError(f"{Path(command[0]).name} did not finish in time.")
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)


def windows_create_shortcut(shortcut: Path, target: str, arguments: str, icon: str, description: str) -> None:
    quote = lambda text: "'" + text.replace("'", "''") + "'"  # noqa: E731  PowerShell single-quoted string
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({quote(str(shortcut))});"
        f"$s.TargetPath = {quote(target)}; $s.Arguments = {quote(arguments)};"
        f"$s.IconLocation = {quote(icon)}; $s.Description = {quote(description)};"
        f"$s.WorkingDirectory = {quote(str(Path(target).parent))}; $s.Save()"
    )
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, creationflags=0x08000000)
    if result.returncode != 0:
        raise InstallerError("Could not create the Start menu shortcut.")


def windows_set_registry(key_path: str, values: dict[str, str | int]) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)


def windows_delete_registry(key_path: str, value: str | None = None) -> None:
    import winreg

    try:
        if value is None:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, value)
    except OSError:
        pass
