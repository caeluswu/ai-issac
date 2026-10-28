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
    pools={p.get("Name"):{int(item.get("Id")) for item in p.findall("Item")}
           for p in ET.parse(game/"itempools.xml").getroot().findall("Pool")}
    boss,treasure=pools["boss"],pools["treasure"]
    shop,angel=pools["shop"],pools["angel"]
    treasure_passives={i for i in treasure if collectible_types.get(i) in ("passive","familiar")}
    takeable=set(ITEM_SUPPORT)-{105}
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
            "boss_pool_supported":len(boss&takeable),"boss_pool_total":len(boss),
            "boss_pool_unsupported":[{"id":i,"name":records.get(i,{}).get("name")} for i in sorted(boss-takeable)],
            "shop_pool_takeable":len(shop&takeable),"shop_pool_total":len(shop),
            "angel_pool_takeable":len(angel&takeable),"angel_pool_total":len(angel),
            "treasure_pool_takeable":len(treasure&takeable),"treasure_pool_total":len(treasure),
            "treasure_passive_familiar_takeable":len(treasure_passives&takeable),
            "treasure_passive_familiar_total":len(treasure_passives),
            "failures":failures}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(any(failures.values()))


if __name__=="__main__":raise SystemExit(main())
