"""Parse literal EID rows without executing Lua, conditions or functions."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing

PINNED_COMMIT = "8b59d00c6828e333294639ad2b2b6875eae821cb"
PARSER_VERSION = "eid-literals-1"
TABLES = {"collectible": "collectibles", "trinket": "trinkets", "card": "cards", "pill_effect": "pills"}
LANGUAGES = {"en_us": "en-US", "zh_cn": "zh-CN"}


def dataset_identity(commit, parser_version, sources, target, rights):
    content = [commit,parser_version,sources,target,rights,"item-text-1"]
    return "eid-"+hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()[:24]


def tokens(text):
    i = 0
    while i < len(text):
        if text[i].isspace(): i += 1; continue
        comment = text.startswith("--", i)
        if comment: i += 2
        long = re.match(r"\[(=*)\[", text[i:])
        if long:
            endmark = "]"+long[1]+"]"
            start = i+len(long[0]); end = text.find(endmark, start)
            if end < 0: raise ValueError("Unterminated long Lua text")
            if not comment: yield "str", text[start:end], i
            i = end+len(endmark); continue
        if comment:
            end = text.find("\n", i); i = len(text) if end < 0 else end+1; continue
        if text[i] in ('"', "'"):
            quote, start = text[i], i; i += 1; value = []
            while i < len(text) and text[i] != quote:
                char = text[i]; i += 1
                if char == "\\":
                    if i == len(text): raise ValueError("Unterminated escape")
                    char = text[i]; i += 1
                    if char == "z":
                        while i < len(text) and text[i].isspace(): i += 1
                        continue
                    if char.isdigit():
                        number = char
                        while i < len(text) and len(number) < 3 and text[i].isdigit(): number += text[i]; i += 1
                        char = chr(int(number))
                    else: char = {"n":"\n", "r":"\r", "t":"\t", "a":"\a", "b":"\b", "f":"\f", "v":"\v"}.get(char, char)
                value.append(char)
            if i == len(text): raise ValueError("Unterminated Lua string")
            i += 1; yield "str", "".join(value), start; continue
        yield "symbol", text[i], i
        i += 1


def literal_rows(text, table_pattern):
    match = re.search(table_pattern+r"\s*=\s*\{", text)
    if not match: raise ValueError("Required EID table not found: "+table_pattern)
    depth, row, start = 1, [], 0
    for token in tokens(text[match.end():]):
        kind, value, pos = token
        if kind == "symbol" and value == "{":
            depth += 1
            if depth == 2: row, start = [token], match.end()+pos
            elif depth > 2: row.append(token)
        elif kind == "symbol" and value == "}":
            if depth >= 2: row.append(token)
            if depth == 2:
                strings = [t[1] for t in row if t[0] == "str"]
                if strings and strings[0].isdigit():
                    shapes = [(t[0], t[1] if t[0] == "symbol" else "s") for t in row]
                    expected = [("symbol","{"),("str","s"),("symbol",","),("str","s"),("symbol",","),("str","s"),("symbol","}")]
                    exact = shapes == expected or shapes == expected[:-1]+[("symbol",","), expected[-1]]
                    yield int(strings[0]), strings[1] if len(strings)>1 else "", strings[2] if len(strings)>2 else "", exact, text.count("\n",0,start)+1
            depth -= 1
            if depth == 0: return
        elif depth >= 2: row.append(token)
    raise ValueError("Unterminated EID table")


def build_package(source_root, destination, *, commit=PINNED_COMMIT):
    if commit != PINNED_COMMIT: raise ValueError("Source revision not approved")
    source_root, destination = Path(source_root), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    records, sources = {}, {}
    for code, language in LANGUAGES.items():
        for layer in ("ab+", "rep"):
            relative = f"descriptions/{layer}/{code}.lua"
            raw = (source_root/relative).read_bytes(); text = raw.decode("utf-8-sig")
            sources[relative] = hashlib.sha256(raw).hexdigest()
            for kind, table in TABLES.items():
                pattern = (r"EID\.descriptions\[languageCode\]\."+table) if layer == "ab+" else (r"local\s+rep"+table[0].upper()+table[1:])
                for item_id, name, description, exact, line in literal_rows(text, pattern):
                    key = (kind, item_id, language)
                    previous = records.get(key)
                    records[key] = {"kind":kind,"id":item_id,"language":language,"name":name or (previous or {}).get("name",""),
                        "raw_text":description, "normalized_text":description.replace("#","\n"),
                        "literal_complete":exact,"source_path":relative,"source_line":line}
    target = {"game":"binding_of_isaac","edition":"repentance","build":"1.7.9b.J835","platform":"windows","extensions":[]}
    rights = {"store_text":"unknown","redistribute_text":"unknown","ai_input":"unknown"}
    dataset = dataset_identity(commit,PARSER_VERSION,sources,target,rights)
    db = destination/"knowledge.sqlite"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE records(kind TEXT,id INTEGER,language TEXT,record TEXT,PRIMARY KEY(kind,id,language))")
        connection.executemany("INSERT INTO records VALUES(?,?,?,?)", [(k[0],k[1],k[2],json.dumps(v,ensure_ascii=False)) for k,v in records.items()])
    manifest = {"api_versions":["1.0"],"storage_schema_version":1,"ontology_version":"item-text-1",
        "dataset_id":dataset,"game_target":target,
        "provider_id":"eid_snapshot","commit":commit,"parser_version":PARSER_VERSION,"source_hashes":sources,
        "database_sha256":hashlib.sha256(db.read_bytes()).hexdigest(),"record_count":len(records),
        "languages":list(LANGUAGES.values()),"created_at":datetime.now(timezone.utc).isoformat(),
        "rights":rights,
        "limits":["conditional_modifiers_not_executed","synergy_coverage_not_exhaustive","original_repentance_only"]}
    (destination/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return manifest
