"""Detect how Claude Desktop is installed and where its per-account session store really lives.

Nothing here writes anything.  Findings on the reference machine (Claude Desktop 2.7032, MSIX):

* Physical data root is  %LOCALAPPDATA%\\Packages\\<PFN>\\LocalCache\\Roaming\\Claude .
  Processes that run *inside* the package (Claude Desktop itself and everything it spawns, including
  Claude Code sessions and their shells) see that folder through file-system virtualisation as
  %APPDATA%\\Claude .  A normal desktop process sees the physical path (and a possibly stale/empty
  %APPDATA%\\Claude).  We therefore always prefer the physical package path and report aliasing.
* Claude Code transcripts (~/.claude) are NOT virtualised and are shared by all accounts.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .common import norm_path


@dataclass
class Installation:
    kind: str = "unknown"                # msix | win32 | unknown
    version: str | None = None
    package_family_name: str | None = None
    package_full_name: str | None = None
    install_location: str | None = None
    app_user_model_id: str | None = None
    exe_path: str | None = None
    data_root: str | None = None          # folder that contains claude-code-sessions
    data_root_source: str = ""
    virtualized_view: str | None = None   # %APPDATA%\Claude
    virtualized_view_aliases_data_root: bool | None = None
    sessions_root: str | None = None
    claude_home: str | None = None        # ~/.claude (transcripts)
    projects_root: str | None = None
    running_inside_package: bool | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _ps_json(script: str, timeout: int = 25):
    if sys.platform != "win32":
        return None
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _find_msix() -> dict | None:
    info = _ps_json("Get-AppxPackage -Name Claude | Select-Object Name,Version,PackageFamilyName,"
                    "PackageFullName,InstallLocation | ConvertTo-Json -Compress")
    if isinstance(info, list):
        info = info[0] if info else None
    if info and info.get("PackageFamilyName"):
        return {"version": info.get("Version"), "pfn": info["PackageFamilyName"],
                "full": info.get("PackageFullName"), "loc": info.get("InstallLocation")}
    # Fallback: look for the package data folder itself.
    la = os.environ.get("LOCALAPPDATA")
    if la:
        pk = Path(la) / "Packages"
        if pk.is_dir():
            for d in pk.iterdir():
                if d.name.startswith("Claude_") and (d / "LocalCache" / "Roaming" / "Claude").is_dir():
                    return {"version": None, "pfn": d.name, "full": None, "loc": None}
    return None


def _manifest_app_id(install_location: str | None) -> str | None:
    if not install_location:
        return None
    try:
        txt = Path(install_location, "AppxManifest.xml").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'<Application\s+[^>]*Id="([^"]+)"', txt)
        return m.group(1) if m else None
    except OSError:
        return None


def _same_file(a: str, b: str) -> bool | None:
    try:
        sa, sb = os.stat(a), os.stat(b)
        return (sa.st_ino, sa.st_dev) == (sb.st_ino, sb.st_dev) and sa.st_ino != 0
    except OSError:
        return None


def default_claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def detect(data_root: str | None = None, claude_home: str | None = None) -> Installation:
    inst = Installation()
    inst.claude_home = claude_home or default_claude_home()
    inst.projects_root = os.path.join(inst.claude_home, "projects")
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        inst.virtualized_view = os.path.join(appdata, "Claude")

    msix = _find_msix()
    if msix:
        inst.kind = "msix"
        inst.version = msix["version"]
        inst.package_family_name = msix["pfn"]
        inst.package_full_name = msix["full"]
        inst.install_location = msix["loc"]
        if not inst.version and msix["full"]:
            m = re.match(r"Claude_([\d.]+)_", msix["full"])
            inst.version = m.group(1) if m else None
        app_id = _manifest_app_id(msix["loc"]) or "Claude"
        inst.app_user_model_id = f'{msix["pfn"]}!{app_id}'
        if msix["loc"]:
            exe = os.path.join(msix["loc"], "app", "claude.exe")
            inst.exe_path = exe if os.path.exists(exe) else None
        if localappdata:
            phys = os.path.join(localappdata, "Packages", msix["pfn"], "LocalCache", "Roaming", "Claude")
            if os.path.isdir(phys):
                inst.data_root = phys
                inst.data_root_source = "MSIX package LocalCache\\Roaming (physical path)"
    else:
        for base in filter(None, [localappdata and os.path.join(localappdata, "AnthropicClaude"),
                                  localappdata and os.path.join(localappdata, "Programs", "Claude"),
                                  os.environ.get("ProgramFiles") and os.path.join(os.environ["ProgramFiles"], "Claude")]):
            if os.path.isdir(base):
                inst.kind = "win32"
                inst.install_location = base
                apps = sorted(Path(base).glob("app-*"), reverse=True)
                if apps:
                    inst.version = apps[0].name[4:]
                    inst.exe_path = str(apps[0] / "claude.exe")
                elif (Path(base) / "Claude.exe").exists():
                    inst.exe_path = str(Path(base) / "Claude.exe")
                break

    if data_root:
        inst.data_root = data_root
        inst.data_root_source = "explicit --data-root / environment override"
    elif not inst.data_root and inst.virtualized_view and os.path.isdir(inst.virtualized_view):
        inst.data_root = inst.virtualized_view
        inst.data_root_source = "%APPDATA%\\Claude"
        if inst.kind == "unknown":
            inst.kind = "win32"
            inst.notes.append("No MSIX package found; assuming a classic (Win32) installation using %APPDATA%\\Claude.")

    if inst.data_root:
        inst.sessions_root = os.path.join(inst.data_root, "claude-code-sessions")
        if inst.virtualized_view and inst.kind == "msix":
            # Aliasing check on a file that always exists in a used data root.
            for probe in ("config.json", "lockfile", "Local State"):
                a, b = os.path.join(inst.data_root, probe), os.path.join(inst.virtualized_view, probe)
                same = _same_file(a, b) if os.path.exists(a) and os.path.exists(b) else None
                if same is not None:
                    inst.virtualized_view_aliases_data_root = same
                    break
            if inst.virtualized_view_aliases_data_root is True:
                inst.running_inside_package = True
                inst.notes.append("This process is running inside Claude Desktop's package identity: "
                                  "%APPDATA%\\Claude is a virtualised alias of the package data folder.")
            elif inst.virtualized_view_aliases_data_root is False:
                inst.running_inside_package = False
                inst.notes.append("%APPDATA%\\Claude exists but is NOT the MSIX data folder (stale/legacy data). "
                                  "It is ignored; the package path is authoritative.")
    if inst.sessions_root and not os.path.isdir(inst.sessions_root):
        inst.notes.append("claude-code-sessions folder does not exist yet (no Code-tab session was ever created).")
    if not os.path.isdir(inst.projects_root):
        inst.notes.append(f"Claude Code projects folder not found: {inst.projects_root}")
    return inst


def is_live_store(inst: Installation, real: Installation | None = None) -> bool:
    """Is ``inst.data_root`` the data root of the real installed Claude Desktop (vs. a sandbox copy)?"""
    real = real or detect()
    return bool(inst.data_root and real.data_root and norm_path(inst.data_root) == norm_path(real.data_root))
