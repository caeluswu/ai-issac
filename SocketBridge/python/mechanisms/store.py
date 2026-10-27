"""Immutable verified packages. Model code cannot call publication methods."""
import hashlib
import json
import re
from uuid import uuid4
from pathlib import Path
from .program import Program,ProgramError,canonical
from .validation import validate_candidate
from .normalization import model_program


def read_candidate(path):
    path=Path(path)
    program=Program((path/"program.json").read_bytes())
    calibration=json.loads((path/"calibration.json").read_text(encoding="utf-8"))
    provenance=json.loads((path/"provenance.json").read_text(encoding="utf-8"))
    if provenance["program_sha256"]!=program.sha256:raise ProgramError("CANDIDATE_HASH_MISMATCH")
    request=Path(provenance["request_artifact"])
    raw=(request/"response.json").read_bytes()
    usage=json.loads((request/"usage.json").read_text(encoding="utf-8"))
    response,normalization=model_program(raw,allow_recovery=True)
    if (hashlib.sha256(raw).hexdigest()!=provenance.get("response_sha256") or response.sha256!=program.sha256 or
            normalization!=provenance.get("normalization",{"kind":"identity"}) or
            usage.get("response_id")!=provenance.get("response_id") or usage.get("response_model")!=provenance.get("response_model")):
        raise ProgramError("MODEL_RESPONSE_PROVENANCE_MISMATCH")
    validation=validate_candidate(program,calibration)
    if not validation["passed"]:raise ProgramError("CANDIDATE_INDEPENDENT_CHECKS_FAILED")
    return program,calibration,provenance,validation


class SkillStore:
    def __init__(self,root):self.root=Path(root)

    def publish(self,candidate,trial_directory):
        program,calibration,provenance,validation=read_candidate(candidate)
        directory=Path(trial_directory)
        trial=json.loads((directory/"mechanism-trial.json").read_text(encoding="utf-8"))
        report=json.loads((directory/"report.json").read_text(encoding="utf-8"))
        if (provenance.get("author")!="deepseek" or not provenance.get("response_id") or
                trial.get("program_sha256")!=program.sha256 or trial.get("normal_combat") is not True or
                trial.get("released_actions",0)<1 or trial.get("adopted_actions",0)<1 or
                trial.get("applied_releases",0)<1 or trial.get("new_tears_observed",0)<1 or
                trial.get("game_build")!="1.7.9b.J835" or trial.get("calibration_id")!=calibration["calibration_id"] or
                trial.get("mechanism_failures")!=[] or trial.get("passed") is not True or
                report.get("success") is not True or report.get("result")!="CLEARED"):
            raise ProgramError("PUBLISH_REQUIRES_MODEL_AUTHORSHIP_AND_REAL_TRIAL")
        manifest={"schema_version":1,"status":"supported","program_sha256":program.sha256,
            "calibration_sha256":hashlib.sha256(canonical(calibration)).hexdigest(),"provenance":provenance,
            "trial":{"directory":str(directory),"report_sha256":hashlib.sha256((directory/"report.json").read_bytes()).hexdigest(),**trial},
            "validation":validation,"target_build":program.document["target_build"],"target_item":69}
        destination=self.root/program.document["component_id"]/program.sha256
        if destination.exists():
            existing=self.load(destination)
            if existing[0].sha256!=program.sha256:raise ProgramError("IMMUTABLE_PACKAGE_CONFLICT")
            self._activate(destination)
            return destination
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.parent/(program.sha256+".tmp")
        temporary.mkdir(exist_ok=False)
        (temporary/"program.json").write_bytes(canonical(program.document))
        (temporary/"calibration.json").write_bytes(canonical(calibration))
        (temporary/"manifest.json").write_bytes(canonical(manifest))
        (temporary/"model-response.json").write_bytes((Path(provenance["request_artifact"])/"response.json").read_bytes())
        temporary.rename(destination)
        self._activate(destination)
        return destination

    def _activate(self,destination):
        index={"schema_version":1,"active":{"69":{"path":str(destination.relative_to(self.root)).replace("\\","/"),
               "manifest_sha256":hashlib.sha256((destination/"manifest.json").read_bytes()).hexdigest()}}}
        temp=self.root/"index.json.tmp"
        temp.write_bytes(canonical(index));temp.replace(self.root/"index.json")

    def active(self):
        index=self.root/"index.json"
        if not index.exists():
            if self.packages():raise ProgramError("ACTIVE_SKILL_INDEX_MISSING")
            return None
        value=json.loads(index.read_text(encoding="utf-8"))
        if value.get("schema_version")!=1 or set(value.get("active",{}))!={"69"}:raise ProgramError("SKILL_INDEX_UNSUPPORTED")
        entry=value["active"]["69"]
        if not isinstance(entry,dict) or set(entry)!={"path","manifest_sha256"} or not isinstance(entry["path"],str):raise ProgramError("SKILL_INDEX_INVALID")
        path=(self.root/entry["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):raise ProgramError("PACKAGE_OUTSIDE_SKILL_STORE")
        if path.name in self.quarantines():return None
        if hashlib.sha256((path/"manifest.json").read_bytes()).hexdigest()!=entry["manifest_sha256"]:raise ProgramError("SKILL_MANIFEST_HASH_MISMATCH")
        return self.load(path)

    def quarantines(self):
        path=self.root/"quarantine.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def quarantine(self,program_sha256,reason,evidence):
        if not re.fullmatch(r"[0-9a-f]{64}",program_sha256) or not isinstance(reason,str) or not 0<len(reason)<=500:
            raise ProgramError("INVALID_QUARANTINE")
        self.root.mkdir(parents=True,exist_ok=True)
        value={**self.quarantines(),program_sha256:{"reason":reason,"evidence":str(evidence)}}
        path=self.root/("quarantine-"+uuid4().hex+".tmp")
        path.write_bytes(canonical(value));path.replace(self.root/"quarantine.json")

    def load(self,path):
        path=Path(path)
        if not path.resolve().is_relative_to(self.root.resolve()):raise ProgramError("PACKAGE_OUTSIDE_SKILL_STORE")
        manifest=json.loads((path/"manifest.json").read_text(encoding="utf-8"))
        p=Program((path/"program.json").read_bytes())
        calibration=json.loads((path/"calibration.json").read_text(encoding="utf-8"))
        if (manifest.get("schema_version")!=1 or manifest.get("status")!="supported" or manifest.get("target_build")!="1.7.9b.J835" or
                manifest.get("program_sha256")!=p.sha256 or manifest.get("calibration_sha256")!=hashlib.sha256(canonical(calibration)).hexdigest() or
                manifest.get("provenance",{}).get("program_sha256")!=p.sha256 or
                manifest.get("provenance",{}).get("author")!="deepseek" or not manifest.get("provenance",{}).get("response_id") or
                path.name!=p.sha256 or path.parent.name!=p.document["component_id"]):raise ProgramError("SKILL_PACKAGE_CORRUPT_OR_UNSUPPORTED")
        if not validate_candidate(p,calibration)["passed"]:raise ProgramError("SKILL_REVALIDATION_FAILED")
        raw=(path/"model-response.json").read_bytes()
        response,normalization=model_program(raw,allow_recovery=True)
        if (hashlib.sha256(raw).hexdigest()!=manifest["provenance"].get("response_sha256") or response.sha256!=p.sha256 or
                normalization!=manifest["provenance"].get("normalization",{"kind":"identity"})):
            raise ProgramError("PUBLISHED_MODEL_RESPONSE_MISMATCH")
        return p,calibration,manifest

    def packages(self):
        if not self.root.exists():return []
        paths=sorted(p for p in self.root.glob("weapon.*/*/manifest.json") if not p.parent.name.endswith(".tmp"))
        if len(paths)>128:raise ProgramError("SKILL_STORE_LIMIT")
        return [self.load(p.parent) for p in paths]
