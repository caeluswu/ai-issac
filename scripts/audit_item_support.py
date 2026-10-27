"""Audit reviewed collectible support against the pinned EID and J835 data."""
import json
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from combat.item_capabilities import ITEM_SUPPORT


def main():
    config=json.loads((ROOT/"configs/knowledge/eid-package.json").read_text(encoding="utf-8"))
    knowledge=ROOT/config["path"]/"knowledge.sqlite"
    game=ROOT/"The Binding of Isaac Rebirth Repentance/resources-dlc3"
    items=ET.parse(game/"items.xml").getroot()
    collectible_types={int(e.get("id")):e.tag for e in items if e.get("id","").isdigit()
                       and e.tag in ("passive","familiar","active")}
    pool=next(p for p in ET.parse(game/"itempools.xml").getroot().findall("Pool")
              if p.get("Name")=="boss")
    boss={int(item.get("Id")) for item in pool.findall("Item")}
    records={}
    with sqlite3.connect(f"file:{knowledge}?mode=ro",uri=True) as db:
        for item_id,raw in db.execute("SELECT id,record FROM records WHERE kind='collectible' AND language='en-US'"):
            records[item_id]=json.loads(raw)
    failures={
        "missing_or_partial_eid":[i for i in ITEM_SUPPORT if i not in records or not records[i]["literal_complete"]],
        "unsupported_item_type":[i for i in ITEM_SUPPORT if i!=105 and collectible_types.get(i) not in ("passive","familiar")],
        "unexpected_active":[i for i in ITEM_SUPPORT if collectible_types.get(i)=="active" and i!=105],
    }
    result={"game_build":config["build"],"eid_dataset_id":config["dataset_id"],
            "supported_collectible_ids":len(ITEM_SUPPORT)-1,
            "boss_pool_supported":len(boss&ITEM_SUPPORT.keys()),"boss_pool_total":len(boss),
            "boss_pool_unsupported":[{"id":i,"name":records.get(i,{}).get("name")} for i in sorted(boss-ITEM_SUPPORT.keys())],
            "failures":failures}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(any(failures.values()))


if __name__=="__main__":raise SystemExit(main())
