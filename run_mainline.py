"""Project-root bounded multi-floor mainline runner."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent / "SocketBridge/python"))
from apps.auto_two_floors import cli

if __name__=="__main__":raise SystemExit(cli())
