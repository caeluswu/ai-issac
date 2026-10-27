function clamp(v, a, b) { if (typeof v !== 'number' || !isFinite(v)) return a; return v < a ? a : v > b ? b : v; }
function dist(ax, ay, bx, by) { var dx = ax - bx, dy = ay - by; return Math.sqrt(dx * dx + dy * dy); }
function norm(x, y) { var l = Math.sqrt(x * x + y * y); if (!isFinite(l) || l < 1e-9) return { x: 0, y: 0, len: 0 }; return { x: x / l, y: y / l, len: l }; }
function finiteNum(v, d) { return (typeof v === 'number' && isFinite(v)) ? v : d; }

function pickCalibration(ctx) {
  var fc = ctx.calibration;
  if (!fc || typeof fc !== 'object') return null;
  var cycles = fc.cycles;
  if (!cycles || !cycles.length) return null;
  var best = null;
  for (var i = 0; i < cycles.length; i++) {
    var c = cycles[i];
    if (!c || !c.flying_observed) continue;
    var hu = finiteNum(c.hold_updates, 0);
    var md = finiteNum(c.max_distance, 0);
    var ms = finiteNum(c.max_speed, 0);
    if (!best || (hu >= (best.hold_updates || 0) && md >= (best.max_distance || 0))) {
      best = { hold_updates: hu, max_distance: md, max_speed: ms };
    }
  }
  if (!best) return null;
  return best;
}

function measureKnives(ctx) {
  var knives = ctx.knives || [];
  var out = { any: false, flying: [], held: [], returning: [] };
  for (var i = 0; i < knives.length; i++) {
    var k = knives[i];
    if (!k || !k.pos) continue;
    var ph = k.phase || 'unknown';
    if (ph === 'held') out.held.push(k);
    else if (ph === 'returning' || ph === 'turning') out.returning.push(k);
    else if (ph === 'outbound' || k.is_flying) out.flying.push(k);
    out.any = true;
  }
  return out;
}

function primaryThreat(ctx, px, py) {
  var enemies = ctx.enemies || [];
  var best = null, bestScore = -1e9;
  for (var i = 0; i < enemies.length; i++) {
    var e = enemies[i];
    if (!e || e.dead || e.vulnerable === false || !e.pos) continue;
    var d = dist(px, py, e.pos.x, e.pos.y);
    var r = finiteNum(e.radius, 8);
    var hp = finiteNum(e.hp, 0);
    if (hp <= 0) continue;
    var score = -d + r * 0.5;
    if (score > bestScore) { bestScore = score; best = e; }
  }
  return best;
}

function leadTarget(e, px, py, projSpeed, fallbackDist) {
  var ex = finiteNum(e.pos.x, px), ey = finiteNum(e.pos.y, py);
  var evx = finiteNum(e.vel && e.vel.x, 0), evy = finiteNum(e.vel && e.vel.y, 0);
  var sp = projSpeed > 0.1 ? projSpeed : 8;
  var d = dist(px, py, ex, ey);
  var t = d / sp;
  if (fallbackDist && t > fallbackDist / sp) t = fallbackDist / sp;
  t = clamp(t, 0, 3);
  return { x: ex + evx * t, y: ey + evy * t, t: t };
}

function chooseShot(tx, ty, px, py) {
  var dx = tx - px, dy = ty - py;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0 ? [1, 0] : [-1, 0];
  }
  return dy >= 0 ? [0, 1] : [0, -1];
}

function inferHoldProgress(knives, calib) {
  var held = knives.held;
  if (!held.length) return null;
  var k = held[0];
  var maxD = finiteNum(k.max_distance, calib ? calib.max_distance : 0);
  var curCharge = finiteNum(k.charge, null);
  if (curCharge === null || maxD <= 0) return null;
  return clamp(curCharge, 0, 1);
}

function nearestTargetDistance(ctx, px, py) {
  var enemies = ctx.enemies || [];
  var best = 1e9;
  for (var i = 0; i < enemies.length; i++) {
    var e = enemies[i];
    if (!e || e.dead || e.vulnerable === false || !e.pos) continue;
    var d = dist(px, py, e.pos.x, e.pos.y) - finiteNum(e.radius, 8);
    if (d < best) best = d;
  }
  return best;
}

function init(context) {
  var calib = pickCalibration(context);
  return {
    version: 1,
    calib: calib,
    lastMode: 'idle',
    lastShoot: [0, 0],
    lastFrame: null,
    feedbackCount: 0,
    consistentCount: 0,
    releaseLatch: 0,
    holdProgress: null
  };
}

function observe(context, executedFeedback, state) {
  var st = state;
  if (!st || typeof st !== 'object') st = init(context);
  var wf = context.weapon_feedback || [];
  var latest = null;
  if (wf.length) latest = wf[wf.length - 1];
  if (latest) {
    if (finiteNum(latest.frame, null) !== null) st.lastFrame = latest.frame;
    if (latest.consistent) st.consistentCount = finiteNum(st.consistentCount, 0) + 1;
    st.feedbackCount = finiteNum(st.feedbackCount, 0) + 1;
  }
  var knives = measureKnives(context);
  if (knives.held.length && st.lastMode === 'release') {
    st.releaseLatch = finiteNum(st.releaseLatch, 0) + 1;
  } else if (knives.held.length) {
    st.releaseLatch = 0;
  }
  if (st.calib) {
    st.holdProgress = inferHoldProgress(knives, st.calib);
  }
  return st;
}

function proposeBatch(context, movementCandidates, state) {
  var st = state || {};
  var props = [];
  var cands = movementCandidates || [];
  var player = context.player || { pos: { x: 0, y: 0 }, vel: { x: 0, y: 0 }, radius: 10 };
  var px = finiteNum(player.pos && player.pos.x, 0);
  var py = finiteNum(player.pos && player.pos.y, 0);
  var calib = st.calib || pickCalibration(context);
  var knives = measureKnives(context);
  var dmgPerHit = finiteNum(context.damage_per_hit, 3.5);
  var dmgBase = dmgPerHit * 2;

  var target = primaryThreat(context, px, py);
  var targetIds = [];
  if (target) targetIds.push(target.id);

  var projSpeed = calib ? finiteNum(calib.max_speed, 10) : 10;
  var maxRange = calib ? finiteNum(calib.max_distance, 80) : 80;
  var lead = target ? leadTarget(target, px, py, projSpeed, maxRange) : null;
  var shot = [0, 0];
  if (lead) shot = chooseShot(lead.x, lead.y, px, py);

  var hadHeld = knives.held.length > 0;
  var hadFlying = knives.flying.length > 0;
  var hadReturning = knives.returning.length > 0;
  var ntDist = nearestTargetDistance(context, px, py);
  var holdProgress = finiteNum(st.holdProgress, null);
  var targetD = target ? dist(px, py, target.pos.x, target.pos.y) : 1e9;
  var rangeTarget = target ? clamp(ntDist + 20, 20, maxRange) : maxRange;

  var mode = 'idle';

  if (hadFlying || hadReturning) {
    mode = 'idle';
  } else if (hadHeld && target) {
    var chargeGood = true;
    if (holdProgress !== null && maxRange > 0) {
      var approxChargeNeeded = clamp(targetD / maxRange, 0, 1);
      chargeGood = holdProgress >= approxChargeNeeded * 0.8;
    }
    mode = chargeGood ? 'release' : 'hold';
    if (mode === 'release') st.releaseLatch = 0;
  } else if (hadHeld && !target) {
    mode = 'idle';
  } else if (!knives.any && target) {
    mode = 'hold';
  }

  var outShoot = (mode === 'release' || mode === 'idle') ? [0, 0] : shot;

  for (var i = 0; i < cands.length; i++) {
    var c = cands[i];
    if (!c || !c.id) continue;
    var mx = finiteNum(c.move && c.move[0], 0);
    var my = finiteNum(c.move && c.move[1], 0);
    var n = norm(mx, my);
    var posScore = 0;
    if (target) {
      var dNow = dist(px, py, target.pos.x, target.pos.y);
      var dAfter = dist(px + n.x * 8, py + n.y * 8, target.pos.x, target.pos.y);
      if (mode === 'hold') posScore = dNow - dAfter;
      else posScore = (dNow - dAfter) * 0.3;
      posScore = clamp(posScore, -10, 10);
    }

    var expectedDamage = 0;
    var hitScore = 0;
    if (mode === 'release' && target) {
      var reachable = targetD <= maxRange * 1.1;
      var chargeFactor = 1;
      if (holdProgress !== null) chargeFactor = clamp(holdProgress * 3, 0.5, 1);
      if (reachable) {
        var hits = 1 + (targetD < rangeTarget * 0.5 ? 1 : 0);
        expectedDamage = Math.min(finiteNum(target.hp, dmgBase * 2), dmgBase * chargeFactor * hits);
        hitScore = clamp(0.4 + 0.4 * chargeFactor + 0.2, 0, 1);
      } else {
        hitScore = 0.1;
      }
    } else if (mode === 'hold' && target) {
      var hp0 = finiteNum(target.hp, dmgBase);
      expectedDamage = Math.min(hp0, dmgBase * (holdProgress !== null ? holdProgress : 0.3) * 0.5);
      hitScore = clamp(0.2 + posScore / 20, 0, 1);
    } else if ((hadFlying || hadReturning) && target) {
      expectedDamage = 0;
      hitScore = 0.05;
    }

    props.push({
      candidate_id: String(c.id),
      mode: mode,
      shoot: outShoot,
      target_ids: (mode === 'release' || mode === 'hold') && target ? targetIds : [],
      expected_damage: isFinite(expectedDamage) ? Math.max(0, expectedDamage) : 0,
      hit_score: isFinite(hitScore) ? clamp(hitScore, 0, 1) : 0,
      position_score: isFinite(posScore) ? clamp(posScore, -10, 10) : 0
    });
  }
  st.lastMode = mode;
  return props;
}
