"""Project-root entry for bounded mechanism development and publication."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/"SocketBridge/python"))
from apps.mechanism_development import cli
if __name__=="__main__":raise SystemExit(cli())
