"""Query one pinned EID item and persist it in the local knowledge database.

No game, Lua execution, network fetch or DeepSeek request is started here.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"SocketBridge/python"))
from combat.item_knowledge import KnowledgeGateway


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id",nargs="?",type=int)
    parser.add_argument("--kind",choices=("collectible","trinket","card","pill_effect"),default="collectible")
    parser.add_argument("--purpose",choices=("display","planning","component_development"),default="display")
    parser.add_argument("--language",choices=("zh-CN","en-US"),default="zh-CN")
    parser.add_argument("--refresh",action="store_true",help="reread the pinned local source and upsert this item; no online update")
    parser.add_argument("--list",action="store_true",help="list already queried items in the current dataset")
    args=parser.parse_args()
    if args.item_id is None and not args.list:parser.error("provide one item ID or --list")
    if args.item_id is not None and (args.item_id<0 or args.list):parser.error("use a nonnegative ID, or --list alone")
    try:
        gateway=KnowledgeGateway.from_config(ROOT)
        store=gateway.package.item_store
        if store is None:raise ValueError("LOCAL_ITEM_KNOWLEDGE_NOT_CONFIGURED")
        if args.list:
            result={"local_database":str(store.path),"dataset_id":gateway.package.dataset_id,
                "items":store.list_items(provider_id=gateway.package.manifest["provider_id"],dataset_id=gateway.package.dataset_id)}
            success=True
        else:
            answer=gateway.lookup(args.kind,args.item_id,args.purpose,language=args.language,refresh=args.refresh)
            result={"local_database":str(store.path),**answer}
            success=answer["status"]!="error" and any(record["summary"]["text"] for r in answer["results"] for record in r["records"])
        output=json.dumps(result,ensure_ascii=False,indent=2)
        if sys.stdout.encoding and sys.stdout.encoding.lower()!="utf-8":sys.stdout.reconfigure(encoding="utf-8")
        print(output)
        return 0 if success else 1
    except (ValueError,OSError,RuntimeError) as exc:
        print(json.dumps({"status":"error","error_type":type(exc).__name__,"message":str(exc)},ensure_ascii=True))
        return 1


if __name__=="__main__":raise SystemExit(cli())
