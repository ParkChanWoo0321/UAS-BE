"""Send native flight commands to the locally owned PX4 simulator."""

import argparse
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import rclpy
from px4_msgs.msg import FailsafeFlags, PositionSetpoint, PositionSetpointTriplet, VehicleCommand
from px4_msgs.msg import VehicleCommandAck, VehicleLocalPosition, VehicleStatus
from rclpy.qos import qos_profile_sensor_data

from sim_runtime import WORLD

STATUS_TOPIC = "/fmu/out/vehicle_status"
POSITION_TOPIC = "/fmu/out/vehicle_local_position"
COMMAND_TOPIC = "/fmu/in/vehicle_command"
RTL = VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH
ARM = VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM
TAKEOFF = VehicleCommand.VEHICLE_CMD_NAV_VTOL_TAKEOFF
REPOSITION = VehicleCommand.VEHICLE_CMD_DO_REPOSITION
NAV_RTL = VehicleStatus.NAVIGATION_STATE_AUTO_RTL
NAV_LOITER = VehicleStatus.NAVIGATION_STATE_AUTO_LOITER
NAV_OFFBOARD = VehicleStatus.NAVIGATION_STATE_OFFBOARD
POSITION_TIMEOUT = 7
STATUS_TIMEOUT = 25
COMMAND_TIMEOUT = 25
TAKEOFF_HEIGHT = 35
DEFAULT_LOITER_RADIUS = 80
ARM_STABILITY_SECONDS = 3


def emit(kind, **fields):
    print(json.dumps({"type": kind, **fields}, ensure_ascii=True, allow_nan=False), flush=True)


def start_world():
    service = f"/world/{WORLD}/control"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            started = subprocess.run(
                ["gz", "service", "-s", service, "--reqtype", "gz.msgs.WorldControl",
                 "--reptype", "gz.msgs.Boolean", "--timeout", "3000", "--req", "pause: false"],
                capture_output=True, text=True, timeout=6,
            )
            if started.returncode == 0 and "data: true" in started.stdout:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise RuntimeError("Gazebo 해안 월드 응답을 받지 못했어요.")


def global_target(position, north, east):
    # PX4's local frame is a tangent plane at the EKF reference.
    radius = 6_371_000.0
    latitude, longitude = map(math.radians, (position.ref_lat, position.ref_lon))
    angle = math.hypot(north, east) / radius
    bearing = math.atan2(east, north)
    target_lat = math.asin(math.sin(latitude) * math.cos(angle)
                          + math.cos(latitude) * math.sin(angle) * math.cos(bearing))
    target_lon = longitude + math.atan2(math.sin(bearing) * math.sin(angle) * math.cos(latitude),
                                       math.cos(angle) - math.sin(latitude) * math.sin(target_lat))
    return math.degrees(target_lat), math.degrees(math.remainder(target_lon, 2 * math.pi))


class Patrol:
    def __init__(self):
        self.bearing = None
        self.sweep = 0.0
        self.completed = False

    def update(self, north, east):
        heading = math.atan2(east, north)
        if self.bearing is not None:
            turn = math.remainder(heading - self.bearing, 2 * math.pi)
            if abs(turn) < math.pi / 3:
                self.sweep += turn
        self.bearing = heading
        self.completed |= abs(self.sweep) >= 2 * math.pi

    def pause(self):
        self.bearing = None


class Flight:
    def __init__(self, node, args):
        self.node = node
        self.args = args
        self.data = {}
        self.home = (0.0, 0.0, 0.0) if args.observe else None
        self.destination = None
        self.ready_since = None
        self.armed_once = False
        self.stage = "observe" if args.observe else "waiting"
        self.sent_at = 0.0
        self.failure = None
        self.patrol = Patrol()
        self.rtl_requested = False
        self.rtl_entered = False
        self.rtl_sent_at = 0.0
        self.rtl_ack = None
        self.return_response = False
        self.auto_return_sent = False
        self.message = None
        self.command = node.create_publisher(VehicleCommand, COMMAND_TOPIC, 10)
        for kind, topic, callback in (
            (VehicleStatus, STATUS_TOPIC, self._save("status")),
            (VehicleLocalPosition, POSITION_TOPIC, self._save("position")),
            (FailsafeFlags, "/fmu/out/failsafe_flags", self._save("flags")),
            (PositionSetpointTriplet, "/fmu/out/position_setpoint_triplet", self._save("setpoint")),
            (VehicleCommandAck, "/fmu/out/vehicle_command_ack", self._ack),
        ):
            node.create_subscription(kind, topic, callback, qos_profile_sensor_data)

    def _save(self, key):
        def receive(message):
            self.data[key] = (message, time.monotonic())
        return receive

    def _read(self, key, timeout):
        sample = self.data.get(key)
        return sample[0] if sample and time.monotonic() - sample[1] < timeout else None

    def _isolated(self):
        return (
            self.node.count_publishers(STATUS_TOPIC) == 1
            and self.node.count_publishers(POSITION_TOPIC) == 1
            and self.command.get_subscription_count() == 1
        )

    def _send(self, command, status, **params):
        self.command.publish(VehicleCommand(
            command=command, from_external=True, target_system=status.system_id,
            target_component=status.component_id, source_system=1, source_component=191,
            timestamp=self.node.get_clock().now().nanoseconds // 1000, **params,
        ))

    def _can_return(self, status, position):
        return bool(
            status and position and self.home and not self.rtl_requested and not self.rtl_entered
            and status.nav_state != NAV_RTL and status.arming_state == VehicleStatus.ARMING_STATE_ARMED
            and self._isolated()
        )

    @staticmethod
    def _valid_position(position):
        if position and position.xy_valid and position.z_valid and all(
            math.isfinite(x) for x in (position.x, position.y, position.z)
        ):
            return position
        return None

    def _ack(self, ack):
        if ack.target_component != 191:
            return
        result = int(ack.result)
        if self.rtl_requested and ack.command == RTL:
            self.rtl_ack = result
            if result not in (0, 5):
                self.rtl_requested = False
                self.message = f"PX4가 귀환 요청을 거절했어요. 응답 코드 {result}."
        if result not in (0, 5) and (self.stage, ack.command) in {
            ("arming", ARM), ("takeoff", TAKEOFF), ("orbit", REPOSITION),
        }:
            self.failure = f"PX4가 비행 명령을 거절했어요. 명령 {ack.command} · 응답 {result}."

    def request_return(self, *, automatic=False):
        status = self._read("status", STATUS_TIMEOUT)
        position = self._valid_position(self._read("position", POSITION_TIMEOUT))
        self.return_response = not automatic
        if not self._can_return(status, position):
            self.message = "현재 PX4 위치와 시동 상태로 귀환을 요청할 수 없어요."
            return
        self._send(RTL, status)
        self.rtl_requested = True
        self.rtl_sent_at = time.monotonic()
        self.rtl_ack = None
        self.message = None

    def _advance(self, status, position, armed, fixed, now):
        if self.rtl_requested or self.rtl_entered or self.stage == "observe" or self.failure:
            return
        if self.stage == "waiting":
            desktop_ready = not self.args.desktop_ready or self.args.desktop_ready.is_file()
            ready = (desktop_ready and status and position and not armed and status.pre_flight_checks_pass and not status.failsafe
                     and position.xy_global and position.z_global and self._isolated())
            if not ready:
                self.ready_since = None
            elif self.ready_since is None:
                self.ready_since = now
            elif now - self.ready_since >= ARM_STABILITY_SECONDS:
                self.home = (position.x, position.y, position.z)
                lat, lon = global_target(position, self.args.north, self.args.east)
                home_amsl = position.ref_alt - position.z
                self.destination = (lat, lon, home_amsl + self.args.altitude)
                self._send(ARM, status, param1=1.0)
                self.stage, self.sent_at = "arming", now
            return
        if self.stage == "arming" and now - self.sent_at > COMMAND_TIMEOUT:
            self.failure = "PX4 시동 응답을 받지 못했어요."
            return
        if not (status and position and armed and not status.failsafe and self._isolated()):
            return
        if self.stage == "arming":
            lat, lon, _ = self.destination
            self._send(TAKEOFF, status, param5=lat, param6=lon,
                       param7=position.ref_alt - self.home[2] + TAKEOFF_HEIGHT)
            self.stage, self.sent_at = "takeoff", now
        elif self.stage == "takeoff":
            if fixed and not status.in_transition_mode:
                lat, lon, altitude = self.destination
                self._send(REPOSITION, status, param1=-1.0, param2=1.0, param4=math.nan,
                           param5=lat, param6=lon, param7=altitude)
                self.stage, self.sent_at = "orbit", now
            elif status.nav_state != VehicleStatus.NAVIGATION_STATE_AUTO_VTOL_TAKEOFF and now - self.sent_at > COMMAND_TIMEOUT:
                self.failure = "PX4가 VTOL 이륙 또는 고정익 전환을 완료하지 못했어요."

    def _patrol_radius(self, status):
        # The navigator only publishes this command setpoint when it changes.
        sample = self.data.get("setpoint")
        if self.stage not in {"orbit", "observe"} or not sample or not status or status.nav_state != NAV_LOITER:
            return None
        loiter = sample[0].current
        if not (loiter.valid and loiter.type == PositionSetpoint.SETPOINT_TYPE_LOITER):
            return None
        if self.destination:
            lat, lon, alt = self.destination
            if not (abs(loiter.lat - lat) < 0.0002 and abs(loiter.lon - lon) < 0.0002 and abs(loiter.alt - alt) < 2):
                return None
        radius = abs(loiter.loiter_radius)
        return radius if math.isfinite(radius) and radius > 0 else DEFAULT_LOITER_RADIUS

    def sample(self):
        now = time.monotonic()
        position_sample = self._read("position", POSITION_TIMEOUT)
        position = self._valid_position(position_sample)
        status = self._read("status", STATUS_TIMEOUT)
        flags = self._read("flags", STATUS_TIMEOUT)
        armed = bool(status and status.arming_state == VehicleStatus.ARMING_STATE_ARMED)
        fixed = bool(status and status.vehicle_type == VehicleStatus.VEHICLE_TYPE_FIXED_WING)
        transitioning = bool(status and status.in_transition_mode)
        self.armed_once |= armed
        if status and status.nav_state == NAV_RTL:
            self.rtl_entered = True
            self.message = None
        if self.rtl_requested and not self.rtl_entered and now - self.rtl_sent_at > COMMAND_TIMEOUT:
            self.rtl_requested = False
            self.message = "PX4가 귀환 모드 진입을 확인하지 않았어요."
        self._advance(status, position, armed, fixed, now)

        altitude = self.home[2] - position.z if position and self.home else None
        home_distance = (math.hypot(position.x - self.home[0], position.y - self.home[1])
                         if position and self.home else None)
        delta = (position.x - self.args.north, position.y - self.args.east) if position else None
        target_distance = math.hypot(*delta) if delta else None
        radius = self._patrol_radius(status) if fixed and armed and not transitioning else None
        patrolling = bool(radius and delta and target_distance <= radius + 35
                          and altitude is not None and abs(altitude - self.args.altitude) <= 12
                          and not (self.rtl_requested or self.rtl_entered or status.failsafe))

        if patrolling:
            if radius / 2 <= target_distance:
                self.patrol.update(*delta)
            if self.patrol.completed and not self.args.observe and not self.auto_return_sent:
                self.auto_return_sent = True
                self.request_return(automatic=True)
        else:
            self.patrol.pause()

        message = self.failure or self.message
        if self.rtl_entered and status and not armed and altitude is not None and abs(altitude) <= 3 and home_distance <= 10:
            phase = "landed"
        elif not position_sample:
            phase = "signal_lost" if self.armed_once else "waiting"
        elif not status:
            phase = "status_delayed" if self.armed_once else "waiting"
        elif status.failsafe or status.failsafe_and_user_took_over:
            phase = "failsafe"
            if flags and flags.local_position_invalid:
                message = "PX4 페일세이프 · 위치 추정 무효"
            elif flags and flags.offboard_control_signal_lost and status.nav_state_user_intention == NAV_OFFBOARD:
                message = "PX4 페일세이프 · Offboard 신호 끊김"
            else:
                message = "PX4가 페일세이프에 진입했어요."
        elif armed and status.nav_state == NAV_RTL:
            phase = "returning"
        elif self.failure:
            phase = "failed" if not armed else "failsafe"
        elif not armed:
            phase = "disarmed" if self.armed_once else ("arming" if self.stage == "arming" else "waiting")
        elif transitioning:
            phase = "transition"
        elif fixed:
            phase = "patrol" if patrolling else "cruise"
        else:
            phase = "takeoff"
        if phase == "waiting" and status and not status.pre_flight_checks_pass:
            message = "PX4 시동 전 점검이 완료되면 입력한 임무를 전송해요."
        elif phase == "waiting" and position and self.args.desktop_ready and not self.args.desktop_ready.is_file():
            message = "Gazebo 3D 추적 화면을 준비하고 있어요."
        if phase == "status_delayed":
            message = "PX4 위치 신호는 수신 중이에요. 비행 상태를 갱신하고 있어요."

        event = {
            "phase": phase, "fresh": position_sample is not None, "armed": armed,
            "nav": status.nav_state if status else None,
            "offboard": bool(status and status.nav_state == NAV_OFFBOARD),
            "fixed_wing": fixed, "transitioning": transitioning,
            "position_valid": position is not None,
            "north": round(position.x, 2) if position else None,
            "east": round(position.y, 2) if position else None,
            "altitude": round(altitude, 2) if altitude is not None else None,
            "target_distance": round(target_distance, 2) if target_distance is not None else None,
            "home_distance": round(home_distance, 2) if home_distance is not None else None,
            "can_return": self._can_return(status, position),
            "rtl_requested": self.rtl_requested, "rtl_ack": self.rtl_ack,
            "return_response": self.return_response, "message": message,
        }
        self.return_response = False
        return event


def main():
    parser = argparse.ArgumentParser()
    for field in ("north", "east", "altitude"):
        parser.add_argument(field, type=float)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--desktop-ready", type=Path)
    args = parser.parse_args()
    start_world()
    rclpy.init()
    node = rclpy.create_node("maritime_web_console")
    flight = Flight(node, args)
    gimbal = None
    if not args.observe:
        from patrol_gimbal import PatrolGimbal
        gimbal = PatrolGimbal(args.north, args.east)
    next_sample = next_aim = 0.0
    pending = b""
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
            if select.select([sys.stdin], [], [], 0)[0]:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line == b'{"action":"return"}':
                        flight.request_return()
            now = time.monotonic()
            if gimbal and now >= next_aim:
                gimbal.update()
                next_aim = now + 0.16
            if now >= next_sample:
                emit("telemetry", data=flight.sample())
                next_sample = now + 0.5
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        emit("error", message=str(error))
        raise
