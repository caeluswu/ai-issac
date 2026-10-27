"""Evidence-linked diagnostics. Counterfactual scores remain model predictions."""
import math
from .builds import digest

ANALYZER_VERSION=1


def analyze_episode(episode):
    rows=episode["records"];start,end=episode["start_frame"],episode["end_frame"]
    evidence={};facts=[];hypotheses=[];missing=[]
    def fact(code,value,row=None):
        ref=f"{episode['episode_id']}:{len(evidence)+1}"
        evidence[ref]={"code":code,"value":value,"trace_sequence":row.get("sequence") if row else episode["trace_sequence"],
                       "frame":row.get("frame") if row else end}
        facts.append({"code":code,"value":value,"evidence_refs":[ref]});return ref
    loss_ref=fact("confirmed_health_loss",{"interval":[start,end],"loss":episode["loss"],
        "before":episode["health_before"],"after":episode["health_after"]})
    snapshots=sorted((r for r in rows if r["kind"]=="snapshot"),key=lambda r:(r["frame"],r["sequence"]))
    before=next((r for r in reversed(snapshots) if r["frame"]<=start),None)
    after=next((r for r in snapshots if r["frame"]>=end),None)
    for label,snap in (("before",before),("after",after)):
        if not snap:continue
        pos=snap["player"]["pos"]
        def nearest(entities,limit=6):
            ranked=sorted(entities,key=lambda e:math.hypot(e.get("pos",{}).get("x",1e9)-pos["x"],e.get("pos",{}).get("y",1e9)-pos["y"]))
            keys=("instance_id","id","type","variant","pos","vel","collision_radius","hp","owner_type","timer","timer_known","explosion_radius")
            return [{k:e[k] for k in keys if k in e} for e in ranked[:limit]]
        entities=snap["entities"]
        fact("local_context_"+label,{"player":snap["player"],"enemies":nearest(entities["enemies"]),
            "enemy_projectiles":nearest(entities["projectiles"].get("enemy_projectiles",[])),
            "bombs":nearest(entities["bombs"]),"hazards":nearest(entities["hazards"]),
            "recent_deaths":snap.get("recent_deaths"),"entity_capture_truncated":snap.get("entity_capture_truncated")},snap)
    if before and after:
        p,q=before["player"]["pos"],after["player"]["pos"]
        fact("observed_player_displacement",{"frames":[before["frame"],after["frame"]],"dx":q["x"]-p["x"],"dy":q["y"]-p["y"]},after)
    if episode.get("run_outcome"):fact("completed_run_outcome",episode["run_outcome"])
    attempts=[];seen=set()
    for row in rows:
        if row["kind"]!="damage_attempt" or not start<=row["frame"]<=end:continue
        key=row["data"].get("event_id",row["sequence"])
        if key not in seen:attempts.append(row);seen.add(key)
    source={"kind":"unknown","evidence_level":"unknown","entity_id":None,"reported_source_types":[],"evidence_refs":[]}
    for row in attempts:
        data=row["data"];ref=fact("damage_attempt",data,row);source["evidence_refs"].append(ref)
        source["reported_source_types"].append(data.get("source_type"))
    if len(attempts)==1:
        data=attempts[0]["data"];entity=data.get("source_entity") or {};typ=data.get("source_type")
        flags=data.get("flags",0)
        kind="explosion" if type(flags) is int and flags&4 else "projectile" if typ==9 else "laser" if typ==7 else "enemy_source" if type(typ) is int and 10<=typ<1000 else "terrain_or_effect" if typ==1000 else "unknown"
        source.update(kind=kind,entity_id=entity.get("instance_id"),evidence_level="observed" if kind!="unknown" else "unknown")
        if not entity.get("instance_id"):missing.append("EXACT_SOURCE_ENTITY_NOT_RECORDED")
        if before and entity.get("instance_id"):
            all_entities=before["entities"]["enemies"]+before["entities"]["bombs"]+before["entities"]["hazards"]
            all_entities+=sum(before["entities"]["projectiles"].values(),[])
            match=next((e for e in all_entities if e.get("instance_id")==entity["instance_id"]),None)
            fact("source_visible_before_loss",{"entity_id":entity["instance_id"],"recorded":match is not None,
                "capture_truncated":before.get("entity_capture_truncated",False)},before)
    elif len(attempts)>1:missing.append("MULTIPLE_ATTEMPTS_UNIQUE_DAMAGE_SOURCE_UNRESOLVED")
    else:missing.append("DAMAGE_ATTEMPT_NOT_RECORDED_OR_OUTSIDE_HEALTH_INTERVAL")
    if end-start>1:missing.append("HEALTH_OBSERVATION_GAP")
    if before and before["room_visit_id"]!=episode["room_visit_id"]:missing.append("DAMAGE_SPANS_ROOM_TRANSITION")
    actions=[r for r in rows if r["kind"]=="action" and r["frame"]<=end and r["sequence"]<=episode["trace_sequence"] and r["data"].get("sent")]
    observed_id=after.get("applied_action_id") if after else None
    matched=next((r for r in reversed(actions) if r["data"].get("action_id")==observed_id),None) if observed_id is not None else None
    action=matched or (actions[-1] if actions else None)
    action_ref=None;selected=None;alternatives=[]
    if action:
        data=action["data"];meta=data.get("metadata") or {};diag=meta.get("diagnostics") or {}
        action_ref=fact("action_timeline",{"action_id":data.get("action_id"),"observed_action_id":observed_id,
            "sent_frame":action["frame"],"move":data.get("move"),"shoot":data.get("shoot"),
            "profile":meta.get("profile"),"score":meta.get("score"),"diagnostics":diag,
            "matched_observed_action_id":matched is not None},action)
        receipts=[r for r in rows if r["kind"]=="receipt" and r["data"].get("action_id")==data.get("action_id")]
        fact("action_receipts",[r["data"] for r in receipts],action)
        if matched is None:missing.append("SELECTED_ACTION_APPLICATION_UNCONFIRMED")
        missing.append("ACTION_ID_IS_NOT_PROOF_OF_MOVEMENT_CALLBACK_EXECUTION")
        elapsed=diag.get("decision_wall_ms")
        if isinstance(elapsed,(float,int)) and elapsed>30:
            hypotheses.append({"code":"decision_latency_may_contribute","evidence_level":"supported_hypothesis","evidence_refs":[action_ref],"detail":"Decision exceeded 30 ms; this alone does not establish injury causation."})
        candidates=[r for r in rows if r["kind"]=="candidates" and r["frame"]==action["frame"]]
        if candidates:
            pool=candidates[-1]["candidates"]
            selected=next((c for c in pool if c.get("move")==data.get("move") and c.get("shoot")==data.get("shoot")),None)
            if selected:
                alternatives=[c for c in pool if c.get("safe") and (c.get("risk",0)<selected.get("risk",0) or c.get("clearance",0)>selected.get("clearance",0)+8)]
                chosen_keys=("move","shoot","safe","risk","clearance","escape_slack","utility","hit","fire_reason","predicted_next_position","prediction_origin_snapshot_id","prediction_frame_offset")
                ref=fact("recorded_candidate_comparison",{"selected":{k:selected.get(k) for k in chosen_keys},
                    "lower_predicted_risk_alternatives":[{k:c.get(k) for k in chosen_keys} for c in alternatives[:3]],
                    "count":len(pool),"counterfactual_status":"old_model_prediction_not_proof_of_avoidability"},candidates[-1])
                if alternatives:hypotheses.append({"code":"candidate_tradeoff_worth_testing","evidence_level":"supported_hypothesis","evidence_refs":[ref],"detail":"An alternative had a larger predicted margin; inspect attack utility and other risks before changing selection."})
                if selected.get("safe"):
                    hypotheses.append({"code":"predicted_safe_action_preceded_damage","evidence_level":"supported_hypothesis","evidence_refs":[ref,loss_ref],"detail":"Check prediction, unmodelled source and application timing; this is not proof that selection code is wrong."})
                prediction=selected.get("predicted_next_position")
                following=next((r for r in snapshots if r["frame"]==action["frame"]+1),None)
                if prediction and following and following.get("applied_action_id")==data.get("action_id"):
                    pos=following["player"]["pos"]
                    fact("one_update_motion_residual_px",math.hypot(pos["x"]-prediction[0],pos["y"]-prediction[1]),following)
                elif not prediction:missing.append("PREDICTED_NEXT_POSITION_NOT_RECORDED")
            else:missing.append("SELECTED_CANDIDATE_NOT_MATCHED")
        else:missing.append("CANDIDATE_WINDOW_MISSING_FOR_ACTION")
        components={"plan":action.get("plan"),"mechanism":diag.get("mechanism"),"component_failures":diag.get("component_failures",[])}
    else:
        missing.append("ACTION_NOT_RECORDED");components={}
    for row in rows:
        if row["kind"] in ("component_fallback","stale_decision_discarded") and start-3<=row["frame"]<=end:
            ref=fact(row["kind"],row["data"],row)
            hypotheses.append({"code":row["kind"]+"_may_contribute","evidence_level":"supported_hypothesis","evidence_refs":[ref],"detail":"Observed execution problem near damage; confirm temporal ordering before assigning cause."})
    frame_set=sorted({r["frame"] for r in snapshots})
    window_ref=fact("capture_coverage",{"first_frame":frame_set[0] if frame_set else None,"last_frame":frame_set[-1] if frame_set else None,
        "missing_update_count":sum(max(0,b-a-1) for a,b in zip(frame_set,frame_set[1:])),"closure":episode["closure"]})
    if episode["closure"]!="post_window_collected":missing.append("POST_WINDOW_ENDED_EARLY")
    context=after or before or {}
    result={"schema_version":1,"analyzer_version":ANALYZER_VERSION,"episode_id":episode["episode_id"],
        "run_id":episode["run_id"],"room_visit_id":episode["room_visit_id"],"interval":[start,end],"loss":episode["loss"],
        "direct_source":source,"facts":facts,"hypotheses":hypotheses,"missing_evidence":sorted(set(missing)),
        "evidence":evidence,"build":{"stats":context.get("stats"),"inventory":context.get("inventory"),
            "weapons":context.get("weapons"),"effects":context.get("effects"),"control_mode":context.get("mode")},
        "components":components,"source_manifest":episode.get("source_manifest"),
        "constraints":["source_type_is_not_unique_entity","damage_attempt_is_not_confirmed_loss",
            "recorded_candidates_are_predictions_not_causal_interventions","unknown_is_valid",
            "evaluate_clearance_damage_output_and_time_together"]}
    result["analysis_id"]=digest(result)
    return result
