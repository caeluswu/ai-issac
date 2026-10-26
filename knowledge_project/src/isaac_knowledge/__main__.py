import argparse
import json
from pathlib import Path
from .contracts import LookupRequest
from .importer import build_package

parser = argparse.ArgumentParser(description="Build a pinned EID package without executing Lua")
parser.add_argument("source_root", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
manifest = build_package(args.source_root, args.destination)
(args.destination/"lookup-request.schema.json").write_text(json.dumps(LookupRequest.model_json_schema(),indent=2)+"\n",encoding="utf-8")
print(json.dumps({"dataset_id":manifest["dataset_id"],"record_count":manifest["record_count"]}))
