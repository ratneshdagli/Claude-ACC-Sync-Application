"""Build standalone Windows executables with PyInstaller (optional; the .pyz / .cmd launchers need no build).

    python -m venv build/venv && build/venv/Scripts/pip install pyinstaller
    build/venv/Scripts/python build_exe.py
Outputs dist/claude-session-sync.exe (console: CLI + `gui`) and dist/claude-session-sync-gui.exe (no console window;
opens the GUI, or runs any command given, e.g. `watch --quiet`, `launch`).
"""
import os
import subprocess
import sys

root = os.path.dirname(os.path.abspath(__file__))
entry = os.path.join(root, "build", "entry_cli.py")
entry_gui = os.path.join(root, "build", "entry_gui.py")
os.makedirs(os.path.join(root, "build"), exist_ok=True)
open(entry, "w").write("import sys\nfrom claude_session_sync.cli import main\nsys.exit(main())\n")
# the windowless exe opens the GUI by default but accepts any command (the start-with-Windows entry runs `watch --quiet`)
open(entry_gui, "w").write("import sys\nfrom claude_session_sync.cli import main\nsys.exit(main(sys.argv[1:] or ['gui']))\n")
common = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--paths", root,
          "--add-data", os.path.join(root, "claude_session_sync", "ui") + os.pathsep + "claude_session_sync/ui",
          "--distpath", os.path.join(root, "dist"), "--workpath", os.path.join(root, "build", "pyi")]
subprocess.check_call(common + ["--name", "claude-session-sync", entry])
subprocess.check_call(common + ["--noconsole", "--name", "claude-session-sync-gui", entry_gui])
