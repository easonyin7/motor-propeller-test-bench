"""Locate an application-local interpreter without relying on the build PC."""
from pathlib import Path
import sys


def project_python(root):
    root = Path(root)
    for candidate in (root/'runtime/python.exe', root/'cv_mvp/.venv/Scripts/python.exe'):
        if candidate.is_file():
            return str(candidate)
    console = Path(sys.executable).with_name('python.exe')
    return str(console if console.is_file() else Path(sys.executable))
