"""Offline syntax recovery, never behavioral repair or combat-time parsing.

Only one excess structural closing token may be deleted. All possible deletes
are checked against the unchanged typed language; multiple distinct ASTs reject.
No behavioral oracle participates in choosing a parse.
"""
import hashlib
from .program import Program,ProgramError,parse


def model_program(raw,*,allow_recovery=False):
    if not isinstance(raw,(bytes,str)):raise ProgramError("INVALID_MODEL_RESPONSE")
    if isinstance(raw,bytes):raw=raw.decode("utf-8")
    try:return Program(raw),{"kind":"identity"}
    except ProgramError as strict_error:
        if not allow_recovery:raise
        # Well-formed JSON with wrong types/opcodes is never rewritten.
        try:parse(raw)
        except ProgramError:pass
        else:raise strict_error
    if len(raw.encode())>65536:raise ProgramError("PROGRAM_SIZE")
    quoted=escaped=False;offsets=[]
    for i,ch in enumerate(raw):
        if escaped:escaped=False;continue
        if quoted and ch=="\\":escaped=True;continue
        if ch=='"':quoted=not quoted
        if not quoted and ch in "]}":offsets.append(i)
    if len(offsets)>256:raise ProgramError("SYNTAX_RECOVERY_LIMIT")
    candidates={}
    for offset in offsets:
        try:p=Program(raw[:offset]+raw[offset+1:])
        except (ProgramError,UnicodeError):continue
        candidates.setdefault(p.sha256,(p,offset))
        if len(candidates)>1:raise ProgramError("AMBIGUOUS_SYNTAX_RECOVERY")
    if len(candidates)!=1:raise ProgramError("NO_UNAMBIGUOUS_SYNTAX_RECOVERY")
    program,offset=next(iter(candidates.values()))
    return program,{"kind":"delete_one_excess_closing_delimiter_v1","offset":offset,"offset_unit":"unicode_code_points","deleted":raw[offset],
                    "original_sha256":hashlib.sha256(raw.encode()).hexdigest(),"well_typed_results":1}
