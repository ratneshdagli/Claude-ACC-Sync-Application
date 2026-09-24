"""Build dist/claude-session-sync.pyz : a single-file, dependency-free executable Python archive.

    python build_pyz.py
    python dist/claude-session-sync.pyz scan
"""
import os
import shutil
import zipapp

root = os.path.dirname(os.path.abspath(__file__))
stage = os.path.join(root, "build", "pyz")
shutil.rmtree(stage, ignore_errors=True)
os.makedirs(stage)
shutil.copytree(os.path.join(root, "claude_session_sync"), os.path.join(stage, "claude_session_sync"),
                ignore=shutil.ignore_patterns("__pycache__"))
with open(os.path.join(stage, "__main__.py"), "w") as f:
    f.write("import sys\nfrom claude_session_sync.cli import main\nsys.exit(main())\n")
os.makedirs(os.path.join(root, "dist"), exist_ok=True)
out = os.path.join(root, "dist", "claude-session-sync.pyz")
zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3", compressed=True)
print("built", out, os.path.getsize(out), "bytes")
