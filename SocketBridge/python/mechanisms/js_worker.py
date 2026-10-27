"""Pure QuickJS service. No Python callbacks, file, process or network JS APIs."""
import json
import sys
import quickjs

WRAPPER="""function __tick(raw){const p=JSON.parse(raw);let state=p.reset?init(p.context):p.state;
state=observe(p.context,p.feedback,state);return JSON.stringify({state:state,proposals:proposeBatch(p.context,p.candidates,state)});} """


def main():
    sys.stdin.reconfigure(encoding="utf-8");sys.stdout.reconfigure(encoding="utf-8")
    context=None;tick=None
    for raw in sys.stdin:
        try:
            if len(raw.encode())>262144:raise ValueError("JS_INPUT_SIZE")
            message=json.loads(raw)
            if message["type"]=="load":
                source=message["source"]
                if not isinstance(source,str) or not 1<=len(source.encode())<=65536:raise ValueError("JS_SOURCE_SIZE")
                context=quickjs.Context();context.set_memory_limit(32*1024*1024);context.set_max_stack_size(512*1024);context.set_time_limit(.01)
                context.eval(source+"\n"+WRAPPER);tick=context.get("__tick")
                if context.eval("typeof init+' '+typeof observe+' '+typeof proposeBatch")!="function function function":raise ValueError("JS_ENTRYPOINTS")
                value={"loaded":True}
            else:
                if tick is None:raise ValueError("JS_NOT_LOADED")
                value=json.loads(tick(json.dumps(message["payload"],ensure_ascii=False,allow_nan=False,separators=(",",":"))))
                if len(json.dumps(value.get("state"),allow_nan=False).encode())>16384:raise ValueError("JS_STATE_SIZE")
            result={"id":message["id"],"ok":True,"value":value}
        except Exception as exc:
            result={"id":message.get("id") if 'message' in locals() else None,"ok":False,"error":type(exc).__name__+": "+str(exc)[:700]}
        encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(",",":"))
        if len(encoded.encode())>65536:encoded=json.dumps({"id":result["id"],"ok":False,"error":"JS_OUTPUT_SIZE"})
        print(encoded,flush=True)


if __name__=="__main__":main()
