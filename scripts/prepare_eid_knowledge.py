"""Reproduce the pinned, local EID package; no game or model requests."""
import hashlib
import json
from pathlib import Path
import urllib.request
from isaac_knowledge import KnowledgePackage, LookupRequest
from isaac_knowledge.importer import build_package

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT/"configs/knowledge/eid-package.json").read_text(encoding="utf-8"))
    source = ROOT/"artifacts/sources/eid"/config["commit"]
    for relative, expected in config["source_hashes"].items():
        path = source/relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            url = "https://raw.githubusercontent.com/wofsauge/External-Item-Descriptions/"+config["commit"]+"/"+relative.replace("+","%2B")
            with urllib.request.urlopen(url, timeout=30) as response: data=response.read()
            if hashlib.sha256(data).hexdigest() != expected: raise ValueError("EID_SOURCE_HASH_MISMATCH")
            path.write_bytes(data)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected: raise ValueError("EID_SOURCE_HASH_MISMATCH")
    destination = ROOT/config["path"]
    if not destination.exists():
        build_package(source,destination)
        (destination/"lookup-request.schema.json").write_text(json.dumps(LookupRequest.model_json_schema(),indent=2)+"\n",encoding="utf-8")
    package = KnowledgePackage(destination)
    if package.dataset_id != config["dataset_id"]: raise ValueError("KNOWLEDGE_DATASET_MISMATCH")
    print(json.dumps({"dataset_id":package.dataset_id,"record_count":package.manifest["record_count"],"status":"READY"}))


if __name__ == "__main__": main()
