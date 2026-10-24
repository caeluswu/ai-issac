"""Minimal live control probe for SocketBridge / Repentance.

Run from SocketBridge/python with ``python -m apps.control_probe``.  The
game-side mod remains the authority for releasing input on a lost connection.
"""

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from facade import SocketBridge


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "artifacts" / "control-mvp"
STOP_SPEED_AT_TEN_MAX = 1.0
STOP_FINAL_SPEED_MAX = 0.1
PHASES = (
    ("move_right", (1, 0), (0, 0), 1.0),
    ("stop_after_move", (0, 0), (0, 0), 0.7),
    ("shoot_up", (0, 0), (0, -1), 1.0),
    ("stop_after_shoot", (0, 0), (0, 0), 0.7),
    ("move_left_and_shoot", (-1, 0), (0, -1), 1.0),
    ("stop_after_both", (0, 0), (0, 0), 0.7),
)


class ControlProbe:
    def __init__(self, bridge: SocketBridge, output_dir: Path, *,
                 cycles: int = 3, tick_seconds: float = 0.05,
                 phase_scale: float = 1.0):
        self.bridge = bridge
        self.cycles = cycles
        self.tick_seconds = tick_seconds
        self.phase_scale = phase_scale
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.trace_path = self.output_dir / f"probe-{session}.jsonl"
        self.summary_path = self.output_dir / f"probe-{session}.summary.json"
        self._trace = self.trace_path.open("w", encoding="utf-8")
        self.samples: list[dict[str, Any]] = []
        self.latest_room = -1
        self.room_transition_method = "manual"
        self.doors: dict[str, dict[str, Any]] = {}
        self.room_info_seen: set[int] = set()
        self.tear_ids: set[int] = set()
        self._mode_future: asyncio.Future | None = None
        self._pending_mode: str | None = None
        self.results: list[dict[str, Any]] = []
        self.watchdog_result: dict[str, Any] | None = None
        self.errors: list[str] = []
        bridge.on_message(self._on_message)

    def record(self, kind: str, **data: Any) -> None:
        self._trace.write(json.dumps({"kind": kind, "utc": datetime.now(timezone.utc).isoformat(),
                                      **data}, ensure_ascii=False) + "\n")
        self._trace.flush()

    def _on_message(self, raw: dict[str, Any]) -> None:
        kind = raw.get("type")
        if kind in ("CMD", "COMMAND"):
            result = raw.get("result", {})
            self.record("command_result", frame=raw.get("frame"), result=result)
            if self._mode_future and not self._mode_future.done() and isinstance(result, dict):
                if result.get("mode") == self._pending_mode or result.get("success") is False:
                    self._mode_future.set_result(result)
            return
        if kind not in ("DATA", "FULL"):
            if kind == "EVENT":
                self.record("event", frame=raw.get("frame"), event=raw.get("event"))
            return

        frame = raw.get("frame")
        room = raw.get("room_index", -1)
        payload = raw.get("payload") or {}
        if isinstance(room, int) and room >= 0 and room != self.latest_room:
            self.record("room_change", frame=frame, old_room=self.latest_room, room=room)
            self.latest_room = room
            self.doors = {}
        if "ROOM_INFO" in payload and isinstance(room, int) and room >= 0:
            self.room_info_seen.add(room)
        layout = payload.get("ROOM_LAYOUT")
        if isinstance(layout, dict) and isinstance(layout.get("doors"), dict):
            self.doors = layout["doors"]
            self.record("doors", frame=frame, room=room, doors=self.doors)

        players = payload.get("PLAYER_POSITION")
        player = (players[0] if players else None) if isinstance(players, list) else None
        if isinstance(players, dict):
            player = players.get("1", players.get(1))
        if isinstance(player, dict):
            try:
                pos, vel = player["pos"], player["vel"]
                sample = {
                    "frame": int(frame), "room": int(room), "at": time.monotonic(),
                    "x": float(pos["x"]), "y": float(pos["y"]),
                    "vx": float(vel["x"]), "vy": float(vel["y"]),
                }
                self.samples.append(sample)
                self.record("position", **{k: v for k, v in sample.items() if k != "at"})
            except (KeyError, TypeError, ValueError) as exc:
                self.record("invalid_position", frame=frame, reason=str(exc))

        projectiles = payload.get("PROJECTILES")
        if isinstance(projectiles, dict):
            tears = projectiles.get("player_tears") or []
            current = {int(tear["id"]) for tear in tears
                       if isinstance(tear, dict) and tear.get("id") is not None}
            self.tear_ids.update(current)
            self.record("tears", frame=frame, room=room, ids=sorted(current))

    def fresh_position(self, max_age: float = 0.5) -> bool:
        return bool(self.bridge.connected and self.samples and
                    time.monotonic() - self.samples[-1]["at"] <= max_age and
                    self.samples[-1]["room"] >= 0)

    async def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.fresh_position():
                self.record("ready", frame=self.samples[-1]["frame"], room=self.latest_room)
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("60 秒内未收到新鲜的玩家位置数据")

    async def set_mode(self, mode: str, timeout: float = 3.0) -> None:
        if self._mode_future and not self._mode_future.done():
            raise RuntimeError("已有控制模式命令正在等待确认")
        self._pending_mode = mode
        self._mode_future = asyncio.get_running_loop().create_future()
        try:
            sent = await self.bridge.set_control_mode(mode)
            self.record("mode_send", mode=mode, sent=sent)
            if not sent:
                raise ConnectionError(f"无法发送控制模式 {mode}")
            result = await asyncio.wait_for(self._mode_future, timeout)
            if result.get("success") is not True or result.get("mode") != mode:
                raise RuntimeError(f"游戏拒绝控制模式 {mode}: {result}")
        finally:
            self._mode_future = None
            self._pending_mode = None

    async def run_phase(self, cycle: int, name: str, move: tuple[int, int],
                        shoot: tuple[int, int], duration: float) -> dict[str, Any]:
        if not self.fresh_position():
            raise TimeoutError("动作开始前玩家位置已过期")
        start = self.samples[-1]
        start_index = len(self.samples)
        start_tears = self.tear_ids.copy()
        sent_count = 0
        ticks = max(1, math.ceil(duration * self.phase_scale / self.tick_seconds))
        for _ in range(ticks):
            if not self.fresh_position():
                raise TimeoutError(f"{name}: 玩家位置流中断")
            if self.latest_room != start["room"]:
                raise RuntimeError(f"{name}: 动作途中换房；Lua 已退出自动控制")
            sent = await self.bridge.send_input(move=move, shoot=shoot)
            self.record("input_send", cycle=cycle, phase=name, frame=self.samples[-1]["frame"],
                        room=self.latest_room, move=list(move), shoot=list(shoot), sent=sent)
            if not sent:
                raise ConnectionError(f"{name}: 动作发送失败")
            sent_count += 1
            await asyncio.sleep(self.tick_seconds)

        observed = self.samples[start_index:]
        end = observed[-1] if observed else start
        new_tears = sorted(self.tear_ids - start_tears)
        dx, dy = end["x"] - start["x"], end["y"] - start["y"]
        fresh_frames = sorted({sample["frame"] for sample in observed})
        passed = bool(fresh_frames and self.fresh_position() and end["room"] == start["room"])
        detail = ""
        if name == "move_right":
            passed = passed and dx > 20
            detail = "右移位移需超过 20 px"
        elif name == "shoot_up":
            passed = passed and bool(new_tears)
            detail = "射击阶段需出现新泪弹 ID"
        elif name == "move_left_and_shoot":
            passed = passed and dx < -20 and bool(new_tears)
            detail = "左移超过 20 px 且出现新泪弹 ID"
        else:
            after_ten = next((sample for sample in observed
                              if sample["frame"] >= start["frame"] + 10), None)
            last_five = observed[-5:]
            late_speed = math.hypot(after_ten["vx"], after_ten["vy"]) if after_ten else None
            final_speed = math.hypot(end["vx"], end["vy"])
            tail_drift = (math.hypot(last_five[-1]["x"] - last_five[0]["x"],
                                     last_five[-1]["y"] - last_five[0]["y"])
                          if len(last_five) == 5 else None)
            passed = passed and late_speed is not None and late_speed <= STOP_SPEED_AT_TEN_MAX
            passed = passed and final_speed <= STOP_FINAL_SPEED_MAX
            passed = passed and tail_drift is not None and tail_drift <= 5
            detail = "停止后第 10 帧速度 <=1.0，末帧速度 <=0.1，末 5 次采样位移 <=5 px"
        result = {"cycle": cycle, "phase": name, "passed": passed, "criterion": detail,
                  "room": start["room"], "start_frame": start["frame"],
                  "end_frame": end["frame"], "dx": dx, "dy": dy,
                  "new_tear_ids": new_tears, "sent_count": sent_count,
                  "position_samples": len(observed)}
        self.results.append(result)
        self.record("phase_result", **result)
        return result

    async def run_motion_sequence(self) -> None:
        for cycle in range(1, self.cycles + 1):
            for name, move, shoot, duration in PHASES:
                result = await self.run_phase(cycle, name, move, shoot, duration)
                print(f"[{cycle}/{self.cycles}] {name}: {'PASS' if result['passed'] else 'FAIL'}")

    async def run_watchdog(self) -> dict[str, Any]:
        """Hold movement briefly, then send nothing and check the Lua input TTL."""
        if not self.fresh_position():
            raise TimeoutError("看门狗测试前玩家位置已过期")
        first = self.samples[-1]
        for _ in range(max(1, math.ceil(0.3 * self.phase_scale / self.tick_seconds))):
            sent = await self.bridge.send_input(move=(1, 0), shoot=(0, 0))
            self.record("watchdog_input_send", sent=sent, frame=self.samples[-1]["frame"])
            if not sent:
                raise ConnectionError("看门狗测试动作发送失败")
            await asyncio.sleep(self.tick_seconds)
        release = self.samples[-1]
        silent_start_index = len(self.samples)
        await asyncio.sleep(0.8 * self.phase_scale)
        silent = self.samples[silent_start_index:]
        after_ten = next((sample for sample in silent
                          if sample["frame"] >= release["frame"] + 10), None)
        last_five = silent[-5:]
        tail_drift = (math.hypot(last_five[-1]["x"] - last_five[0]["x"],
                                 last_five[-1]["y"] - last_five[0]["y"])
                      if len(last_five) == 5 else None)
        late_speed = math.hypot(after_ten["vx"], after_ten["vy"]) if after_ten else None
        final_speed = (math.hypot(silent[-1]["vx"], silent[-1]["vy"]) if silent else None)
        moved = release["x"] - first["x"]
        passed = (self.fresh_position() and self.latest_room == first["room"]
                  and moved > 5 and late_speed is not None
                  and late_speed <= STOP_SPEED_AT_TEN_MAX
                  and final_speed is not None and final_speed <= STOP_FINAL_SPEED_MAX
                  and tail_drift is not None and tail_drift <= 5)
        self.watchdog_result = {"passed": passed, "initial_dx": moved,
                                "late_speed": late_speed, "final_speed": final_speed,
                                "tail_drift": tail_drift,
                                "silent_samples": len(silent),
                                "release_frame": release["frame"]}
        self.record("watchdog_result", **self.watchdog_result)
        return self.watchdog_result

    async def wait_room_change(self, old_room: int, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.bridge.connected:
                return False
            if self.latest_room >= 0 and self.latest_room != old_room and \
                    self.latest_room in self.room_info_seen:
                self.record("room_transition_verified", old_room=old_room,
                            room=self.latest_room)
                return True
            await asyncio.sleep(0.05)
        return False

    async def trigger_room_change(self, command: str, timeout: float = 15.0) -> bool:
        """Development-only transition through the game's console command."""
        self.room_transition_method = "console"
        old_room = self.latest_room
        sent = await self.bridge.send_console(command)
        self.record("room_command_send", command=command, sent=sent, old_room=old_room)
        return bool(sent and await self.wait_room_change(old_room, timeout))

    async def enter_right_door(self, timeout: float = 15.0) -> bool:
        """Cross an open right-side door using ordinary movement input."""
        self.room_transition_method = "door"
        if not self.fresh_position():
            raise TimeoutError("经门换房前玩家位置已过期")
        start_room = self.latest_room
        player_x = self.samples[-1]["x"]
        candidates = [door for door in self.doors.values()
                      if isinstance(door, dict) and door.get("is_open") is True
                      and not door.get("is_locked")
                      and float(door.get("x", 0)) > player_x + 50]
        if not candidates:
            raise RuntimeError("当前房间没有可用的右侧开门；请在起始空房运行")
        door = max(candidates, key=lambda item: float(item["x"]))
        target_y = float(door["y"])
        self.record("door_selected", room=start_room, door=door)
        deadline = time.monotonic() + timeout

        # Align vertically first.  Release before exact alignment so the
        # player's inertia settles near the doorway rather than past it.
        while time.monotonic() < deadline:
            if not self.fresh_position() or self.latest_room != start_room:
                raise RuntimeError("对准门时观测中断或意外换房")
            sample = self.samples[-1]
            error = target_y - sample["y"]
            if abs(error) <= 20 and abs(sample["vy"]) <= 0.2:
                break
            vertical = (1 if error > 0 else -1) if abs(error) > 20 else 0
            sent = await self.bridge.send_input(move=(0, vertical), shoot=(0, 0))
            self.record("door_align", frame=sample["frame"], error_y=error,
                        move_y=vertical, sent=sent)
            if not sent:
                raise ConnectionError("对准门时动作发送失败")
            await asyncio.sleep(self.tick_seconds)
        else:
            raise TimeoutError("未能在时限内对准右侧门")

        while time.monotonic() < deadline:
            if self.latest_room != start_room:
                return await self.wait_room_change(start_room, timeout=2.0)
            if not self.fresh_position():
                raise TimeoutError("穿门途中玩家位置流中断")
            sent = await self.bridge.send_input(move=(1, 0), shoot=(0, 0))
            self.record("door_approach", frame=self.samples[-1]["frame"],
                        x=self.samples[-1]["x"], room=self.latest_room, sent=sent)
            if not sent:
                raise ConnectionError("穿门时动作发送失败")
            await asyncio.sleep(self.tick_seconds)
        return False

    async def cleanup(self) -> None:
        if self.bridge.connected:
            try:
                await self.bridge.send_input(move=(0, 0), shoot=(0, 0))
                await self.set_mode("MANUAL", timeout=1.5)
            except Exception as exc:
                self.record("cleanup_error", reason=str(exc))
        await self.bridge.stop()

    def finish(self, room_transition: bool, keyboard_recovered: bool | None) -> dict[str, Any]:
        chain_passed = (not self.errors and len(self.results) == self.cycles * len(PHASES)
                        and all(result["passed"] for result in self.results)
                        and self.watchdog_result is not None
                        and self.watchdog_result["passed"]
                        and room_transition and keyboard_recovered is True)
        summary = {
            "passed": chain_passed and self.room_transition_method in ("manual", "door"),
            "control_chain_passed": chain_passed,
            "motion_passed": (len(self.results) == self.cycles * len(PHASES)
                              and all(result["passed"] for result in self.results)),
            "room_transition": room_transition,
            "room_transition_method": self.room_transition_method,
            "watchdog": self.watchdog_result,
            "keyboard_recovered": keyboard_recovered,
            "errors": self.errors,
            "phases": self.results,
            "trace": str(self.trace_path),
        }
        self.record("summary", passed=summary["passed"])
        self._trace.close()
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        return summary


async def run_probe(args: argparse.Namespace) -> int:
    bridge = SocketBridge(host=args.host, port=args.port)
    probe = ControlProbe(bridge, args.output, cycles=args.cycles)
    room_transition = False
    keyboard_recovered = None
    try:
        await bridge.start()
        print("等待游戏连接和玩家位置数据。请在安全的空房间中央开始。")
        await probe.wait_ready(args.connect_timeout)
        await probe.set_mode("FORCE_AI")
        await probe.run_motion_sequence()
        watchdog = await probe.run_watchdog()
        print(f"停发动作看门狗：{'PASS' if watchdog['passed'] else 'FAIL'}")
        if args.auto_door:
            await probe.set_mode("MANUAL")
            await probe.set_mode("FORCE_AI")
            print("动作序列完成。通过正常移动进入右侧房间。")
            room_transition = await probe.enter_right_door(args.room_timeout)
        elif args.room_command:
            await probe.set_mode("MANUAL")
            print(f"动作序列完成。用控制台切换房间：{args.room_command}")
            room_transition = await probe.trigger_room_change(args.room_command,
                                                               args.room_timeout)
        else:
            await probe.set_mode("MANUAL")
            old_room = probe.latest_room
            print("动作序列完成。请在 60 秒内用键盘进入相邻房间。")
            room_transition = await probe.wait_room_change(old_room, args.room_timeout)
    except Exception as exc:
        probe.errors.append(f"{type(exc).__name__}: {exc}")
        probe.record("error", reason=probe.errors[-1])
    finally:
        try:
            await probe.cleanup()
        except Exception as exc:
            probe.errors.append(f"cleanup {type(exc).__name__}: {exc}")
            probe.record("cleanup_error", reason=probe.errors[-1])
        if not args.non_interactive and probe.samples:
            try:
                answer = input("服务已断开。游戏中键盘控制是否恢复？输入 y 或 n：").strip().lower()
                keyboard_recovered = answer in ("y", "yes", "是")
            except EOFError:
                pass
        summary = probe.finish(room_transition, keyboard_recovered)
        print(f"汇总：{probe.summary_path}")
        if summary["passed"]:
            print("结果：PASS")
        elif summary["control_chain_passed"]:
            print("结果：控制链路通过；经门换房仍待验收")
        else:
            print("结果：FAIL / 待人工确认")
    return 0 if summary["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SocketBridge 控制可行性探针")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9527)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--connect-timeout", type=float, default=60.0)
    parser.add_argument("--room-timeout", type=float, default=60.0)
    parser.add_argument("--room-command", default=None,
                        help="开发验证用控制台换房命令；默认等待手动进入相邻房间")
    parser.add_argument("--auto-door", action="store_true",
                        help="在有右侧开门的空房间通过正常移动自动换房")
    parser.add_argument("--non-interactive", action="store_true",
                        help="跳过断线后键盘确认，报告保留待确认状态")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles 必须大于 0")
    if args.auto_door and args.room_command:
        parser.error("--auto-door 与 --room-command 不能同时使用")
    try:
        return asyncio.run(run_probe(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
