"""Splice model-authored named JavaScript functions without editing their body."""
import hashlib
import re


def function_span(source,name):
    matches=list(re.finditer(r"\bfunction\s+"+re.escape(name)+r"\s*\(",source))
    if len(matches)!=1:raise ValueError("JS_PATCH_FUNCTION_IDENTITY")
    start=matches[0].start();begin=source.find("{",matches[0].end())
    if begin<0:raise ValueError("JS_PATCH_NO_BODY")
    depth=0;quote=None;escape=False;comment=None
    for i in range(begin,len(source)):
        c=source[i];n=source[i+1] if i+1<len(source) else ""
        if comment=="line":
            if c=="\n":comment=None
            continue
        if comment=="block":
            if c=="*" and n=="/":comment=None
            continue
        if quote:
            if escape:escape=False
            elif c=="\\":escape=True
            elif c==quote:quote=None
            continue
        if c=="/" and n=="/":comment="line";continue
        if c=="/" and n=="*":comment="block";continue
        if c in ("'",'"',"`"):quote=c;continue
        if c=="{":depth+=1
        elif c=="}":
            depth-=1
            if depth==0:return start,i+1
    raise ValueError("JS_PATCH_UNCLOSED_BODY")


def apply_patch(source,name,replacement):
    if name not in ("proposeBatch","observe","init","buildProposal") or not isinstance(replacement,str):raise ValueError("JS_PATCH_NAME")
    if not replacement.lstrip().startswith("function "+name+"(") or len(replacement.encode())>32768:
        raise ValueError("JS_PATCH_REPLACEMENT")
    left,right=function_span(source,name)
    if function_span(replacement,name)!=(len(replacement)-len(replacement.lstrip()),len(replacement.rstrip())):
        raise ValueError("JS_PATCH_EXTRA_SOURCE")
    result=source[:left]+replacement.strip()+source[right:]
    if len(result.encode())>65536 or result==source:raise ValueError("JS_PATCH_SIZE_OR_NO_CHANGE")
    return result


def sha256(source):return hashlib.sha256(source.encode()).hexdigest()
