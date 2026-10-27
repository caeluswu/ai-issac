"""Load verified packages once at session startup; no combat hot installation."""
from .store import SkillStore
from .weapon import AdaptiveChargedBuild,register_program


def load_session_skills(session,skills_root):
    store=SkillStore(skills_root)
    active=store.active()
    if store.quarantines():session.log("skill_quarantines",entries=store.quarantines())
    if active is None:return
    program,calibration,manifest=active
    register_program(program,calibration)
    session.build_resolver.mechanism_adapter=AdaptiveChargedBuild(program,calibration,skills_root.parent/"artifacts/item-adaptation/calibrations")
    session.mechanism_package=manifest
    if session.learning:session.learning.gateway.mechanism_adapter=session.build_resolver.mechanism_adapter
    session.log("skill_loaded",origin="reused",program_sha256=program.sha256,provenance=manifest["provenance"],
                support_scope=calibration["scope"],generation_api_calls=0)
