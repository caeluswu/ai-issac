r"""Project-root entry point: .venv\Scripts\python.exe run_first_room.py."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent/"SocketBridge/python"))
from apps.auto_first_room import cli

if __name__ == "__main__":
    raise SystemExit(cli())
