"""Typed, total scalar AST. Every evaluated node consumes shared host fuel."""
from dataclasses import dataclass
import hashlib
import json
import math
import re


class ProgramError(ValueError): pass


@dataclass
class Fuel:
    remaining: int = 12000
    used: int = 0

    def spend(self):
        if self.remaining <= 0: raise ProgramError("PROGRAM_FUEL_EXHAUSTED")
        self.remaining -= 1
        self.used += 1


OBSERVE_INPUTS = {"feedback_valid":"bool", "holding":"bool", "shoot_x":"number", "shoot_y":"number",
                  "delta_frames":"number", "new_tear":"bool"}
DECIDE_INPUTS = {"has_target":"bool", "hit":"number", "target_hp":"number", "shoot_x":"number",
                 "shoot_y":"number", "last_shoot_x":"number", "last_shoot_y":"number",
                 "min_charge_frames":"number", "max_charge_frames":"number"}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()


def scalar(value):
    if type(value) is bool: return "bool"
    if type(value) in (float,int) and math.isfinite(value) and abs(value)<=1_000_000: return "number"
    raise ProgramError("INVALID_SCALAR")


def parse(raw):
    if not isinstance(raw,(bytes,str)) or len(raw if isinstance(raw,bytes) else raw.encode())>65536:
        raise ProgramError("PROGRAM_SIZE")
    # Bound JSON nesting before the standard parser allocates recursive objects.
    quoted=escaped=False; depth=0
    for ch in raw.decode("utf-8") if isinstance(raw,bytes) else raw:
        if escaped: escaped=False; continue
        if quoted and ch=="\\": escaped=True; continue
        if ch=='"': quoted=not quoted
        if not quoted:
            if ch in "[{":
                depth+=1
                if depth>24: raise ProgramError("JSON_DEPTH")
            elif ch in "]}": depth-=1
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ProgramError("DUPLICATE_JSON_KEY")
            result[key]=value
        return result
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ProgramError("INVALID_NUMBER")))
    except (ValueError,UnicodeError,RecursionError) as exc: raise ProgramError("INVALID_PROGRAM_JSON") from exc


class Program:
    VERSION=1
    def __init__(self, value):
        # Roundtrip also excludes aliasing/cycles and ensures a bounded input.
        self.document=parse(value if isinstance(value,(bytes,str)) else canonical(value))
        d=self.document
        if not isinstance(d,dict) or set(d)!={"schema_version","component_id","target_build","target_item","state","observe","decide","source_refs","assumptions"}:
            raise ProgramError("PROGRAM_FIELDS")
        if (type(d["schema_version"]) is not int or d["schema_version"]!=1 or
                not isinstance(d["target_build"],str) or not 0<len(d["target_build"])<=64 or
                type(d["target_item"]) is not int or not 0<d["target_item"]<=2147483647):
            raise ProgramError("PROGRAM_SCOPE")
        if not isinstance(d["component_id"],str) or not re.fullmatch(r"weapon\.[a-z][a-z0-9_]{0,63}",d["component_id"]):
            raise ProgramError("PROGRAM_ID")
        for field in ("source_refs","assumptions"):
            if not isinstance(d[field],list) or len(d[field])>16 or any(not isinstance(x,str) or not 0<len(x)<=500 for x in d[field]):
                raise ProgramError("PROGRAM_METADATA")
        self.declarations=d["state"]
        if not isinstance(self.declarations,dict) or len(self.declarations)>16:raise ProgramError("STATE_SIZE")
        for key,spec in self.declarations.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}",key) or not isinstance(spec,dict):raise ProgramError("STATE_DECLARATION")
            expected={"type","initial"} if spec.get("type")=="bool" else {"type","initial","min","max"}
            if set(spec)!=expected or spec["type"] not in ("bool","number") or scalar(spec["initial"])!=spec["type"]:raise ProgramError("STATE_TYPE")
            if spec["type"]=="number":
                if any(scalar(spec[k])!="number" for k in ("min","max")) or not spec["min"]<=spec["initial"]<=spec["max"]:raise ProgramError("STATE_RANGE")
        self.nodes=0
        self._block(d["observe"],0)
        if not isinstance(d["decide"],dict) or set(d["decide"])!={"mode","shoot_x","shoot_y"}:raise ProgramError("DECISION_FIELDS")
        for value in d["decide"].values():
            if self._type(value,DECIDE_INPUTS,0)!="number":raise ProgramError("DECISION_TYPE")
        self.sha256=hashlib.sha256(canonical(d)).hexdigest()

    def initial_state(self):return {k:v["initial"] for k,v in self.declarations.items()}

    def validate_state(self,state):
        if not isinstance(state,dict) or set(state)!=set(self.declarations):raise ProgramError("STATE_FIELDS")
        for key,spec in self.declarations.items():
            value=state[key]
            if scalar(value)!=spec["type"] or (spec["type"]=="number" and not spec["min"]<=value<=spec["max"]):raise ProgramError("STATE_VALUE")

    def _node(self,depth):
        self.nodes+=1
        if self.nodes>512 or depth>16:raise ProgramError("AST_LIMIT")

    def _type(self,node,inputs,depth):
        self._node(depth)
        if not isinstance(node,list):return scalar(node)
        if not node or not isinstance(node[0],str):raise ProgramError("AST_NODE")
        op=node[0]
        if op in ("input","state"):
            if len(node)!=2 or not isinstance(node[1],str):raise ProgramError("REFERENCE")
            names=inputs if op=="input" else {k:v["type"] for k,v in self.declarations.items()}
            if node[1] not in names:raise ProgramError("UNKNOWN_REFERENCE")
            return names[node[1]]
        arity={"add":2,"sub":2,"mul":2,"div":2,"min":2,"max":2,"gt":2,"ge":2,"lt":2,"le":2,
               "eq":2,"and":2,"or":2,"not":1,"abs":1,"if":3}
        if op not in arity or len(node)!=arity[op]+1:raise ProgramError("AST_OPERATOR")
        types=[self._type(x,inputs,depth+1) for x in node[1:]]
        if op=="if":
            if types[0]!="bool" or types[1]!=types[2]:raise ProgramError("IF_TYPE")
            return types[1]
        if op in ("and","or","not"):
            if any(t!="bool" for t in types):raise ProgramError("BOOL_TYPE")
            return "bool"
        if op=="eq":
            if types[0]!=types[1]:raise ProgramError("EQUALITY_TYPE")
            return "bool"
        if any(t!="number" for t in types):raise ProgramError("NUMBER_TYPE")
        return "bool" if op in ("gt","ge","lt","le") else "number"

    def _block(self,block,depth):
        if not isinstance(block,list) or len(block)>64:raise ProgramError("BLOCK")
        for stmt in block:
            self._node(depth)
            if not isinstance(stmt,list) or not stmt:raise ProgramError("STATEMENT")
            if stmt[0]=="set" and len(stmt)==3 and isinstance(stmt[1],str) and stmt[1] in self.declarations:
                if self._type(stmt[2],OBSERVE_INPUTS,depth+1)!=self.declarations[stmt[1]]["type"]:raise ProgramError("ASSIGN_TYPE")
            elif stmt[0]=="if" and len(stmt)==4:
                if self._type(stmt[1],OBSERVE_INPUTS,depth+1)!="bool":raise ProgramError("CONDITION_TYPE")
                self._block(stmt[2],depth+1);self._block(stmt[3],depth+1)
            else:raise ProgramError("STATEMENT_OPERATOR")

    def _inputs(self,inputs,types):
        if not isinstance(inputs,dict) or set(inputs)!=set(types):raise ProgramError("INPUT_FIELDS")
        for k,t in types.items():
            if scalar(inputs[k])!=t:raise ProgramError("INPUT_TYPE")

    def _eval(self,node,inputs,state,fuel):
        fuel.spend()
        if not isinstance(node,list):return node
        op=node[0]
        if op=="input":return inputs[node[1]]
        if op=="state":return state[node[1]]
        ev=lambda i:self._eval(node[i],inputs,state,fuel)
        if op=="if":return ev(2) if ev(1) else ev(3)
        if op=="and":return ev(1) and ev(2)
        if op=="or":return ev(1) or ev(2)
        if op=="not":return not ev(1)
        if op=="abs":return abs(ev(1))
        a,b=ev(1),ev(2)
        if op=="add":v=a+b
        elif op=="sub":v=a-b
        elif op=="mul":v=a*b
        elif op=="div":
            if b==0:raise ProgramError("DIVISION_BY_ZERO")
            v=a/b
        elif op=="min":v=min(a,b)
        elif op=="max":v=max(a,b)
        elif op=="gt":v=a>b
        elif op=="ge":v=a>=b
        elif op=="lt":v=a<b
        elif op=="le":v=a<=b
        elif op=="eq":v=a==b
        else:raise ProgramError("AST_OPERATOR")
        scalar(v)
        return v

    def observe(self,inputs,state,fuel):
        self._inputs(inputs,OBSERVE_INPUTS);self.validate_state(state)
        next_state=dict(state)
        def block(stmts):
            for stmt in stmts:
                fuel.spend()
                if stmt[0]=="set":next_state[stmt[1]]=self._eval(stmt[2],inputs,next_state,fuel)
                else:block(stmt[2] if self._eval(stmt[1],inputs,next_state,fuel) else stmt[3])
        block(self.document["observe"])
        self.validate_state(next_state)
        return next_state

    def decide(self,inputs,state,fuel):
        self._inputs(inputs,DECIDE_INPUTS);self.validate_state(state)
        result={k:self._eval(v,inputs,state,fuel) for k,v in self.document["decide"].items()}
        mode,x,y=result["mode"],result["shoot_x"],result["shoot_y"]
        if mode not in (0,1,2) or x not in (-1,0,1) or y not in (-1,0,1) or abs(x)+abs(y) not in (0,1):raise ProgramError("INVALID_TIMING_OUTPUT")
        if mode==1 and abs(x)+abs(y)!=1:raise ProgramError("HOLD_REQUIRES_DIRECTION")
        return {k:int(v) for k,v in result.items()}


def sdk_contract():
    return {"schema_version":1,"language":"typed_scalar_ast","observe_inputs":OBSERVE_INPUTS,
        "decide_inputs":DECIDE_INPUTS,"decision":{"mode":"0 idle, 1 hold, 2 release","shoot_x":"-1/0/1","shoot_y":"-1/0/1; cardinal only"},
        "expressions":{"references":["input","state"],"numeric":["add","sub","mul","div","min","max","abs"],
                       "boolean":["gt","ge","lt","le","eq","and","or","not"],"conditional":"[if, bool, same_type_value, same_type_value]"},
        "examples":{"literal":3,"read_input":["input","hit"],"numeric_expression":["add",["state","counter"],1],
                    "assignment":["set","counter",["min",100,["add",["state","counter"],1]]],
                    "conditional_expression":["if",["gt",["input","hit"],0.5],1,0]},
        "observe_statements":["[set, declared_state_name, expression]","[if, bool_expression, then_statements, else_statements]"],
        "units":"time in game updates; hit is heuristic 0..1, not probability; pause does not advance time",
        "program_fields":{"schema_version":1,"component_id":"weapon.<lowercase_identifier>","target_build":"1.7.9b.J835","target_item":69,
                          "state":"state declarations dict","observe":"statement list","decide":"exactly {mode:expression,shoot_x:expression,shoot_y:expression}",
                          "source_refs":"list of provided source IDs","assumptions":"list of strings"},
        "state":"dict name -> {type:number,initial:number,min:number,max:number} or {type:bool,initial:bool}; <=16 bounded scalars",
        "limits":{"bytes":65536,"ast_nodes":512,"ast_depth":16,"fuel_per_frame":12000},
        "entrypoints":"observe runs only on deduplicated applied feedback; decide is pure and may run many times per frame"}
