"""Load verified packages once at session startup; no combat hot installation."""
from .store import SkillStore
from .weapon import ChargedBuild,register_program


def load_session_skills(session,root):
    store=SkillStore(root/"skills")
    active=store.active()
    if store.quarantines():session.log("skill_quarantines",entries=store.quarantines())
    if active is None:return
    program,calibration,manifest=active
    register_program(program,calibration)
    session.build_resolver.mechanism_adapter=ChargedBuild(program,calibration)
    session.mechanism_package=manifest
    if session.learning:session.learning.gateway.mechanism_adapter=session.build_resolver.mechanism_adapter
    session.log("skill_loaded",origin="reused",program_sha256=program.sha256,provenance=manifest["provenance"],
                support_scope=calibration["scope"],generation_api_calls=0)
