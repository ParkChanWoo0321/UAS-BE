"""ROS 2 side of the local console. Run inside the Maritime Docker runtime."""

import argparse
import json
import math
import os
import select
import subprocess
import sys
import time

import rclpy
from geometry_msgs.msg import Point
from px4_msgs.msg import FailsafeFlags, VehicleCommand, VehicleCommandAck
from px4_msgs.msg import VehicleLocalPosition, VehicleStatus
from rclpy.qos import qos_profile_sensor_data

CONTROL_SERVICE = "/world/sea_mission/control"
TARGET_TOPIC = "/mission/target_ned"
STATUS_TOPIC = "/fmu/out/vehicle_status"
POSITION_TOPIC = "/fmu/out/vehicle_local_position"
COMMAND_TOPIC = "/fmu/in/vehicle_command"
RTL = VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH
NAV_RTL = VehicleStatus.NAVIGATION_STATE_AUTO_RTL
NAV_OFFBOARD = VehicleStatus.NAVIGATION_STATE_OFFBOARD
POSITION_TIMEOUT = 6
# PX4 publishes status less frequently than position, in simulation time.
STATUS_TIMEOUT = 60


def emit(kind, **fields):
    print(json.dumps({"type": kind, **fields}, ensure_ascii=True, allow_nan=False), flush=True)


def start_world():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            services = subprocess.run(["gz", "service", "-l"], capture_output=True, text=True, timeout=6)
            if CONTROL_SERVICE in services.stdout.splitlines():
                started = subprocess.run(
                    ["gz", "service", "-s", CONTROL_SERVICE, "--reqtype", "gz.msgs.WorldControl",
                     "--reptype", "gz.msgs.Boolean", "--timeout", "3000", "--req", "pause: false"],
                    capture_output=True, text=True, timeout=6,
                )
                if started.returncode == 0 and "data: true" in started.stdout:
                    return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    raise RuntimeError("Gazebo 해안 월드 응답을 받지 못했어요.")


class Flight:
    def __init__(self, node, args):
        self.node = node
        self.args = args
        self.data = {}
        self.home = (0.0, 0.0, 0.0) if args.observe else None
        self.dispatched = args.observe
        self.armed_once = False
        self.rtl_requested = False
        self.rtl_entered = False
        self.rtl_sent_at = 0.0
        self.rtl_ack = None
        self.message = None
        self.return_response = False
        self.target = node.create_publisher(Point, TARGET_TOPIC, 10)
        self.command = node.create_publisher(VehicleCommand, COMMAND_TOPIC, 10)
        for kind, topic, callback in (
            (VehicleStatus, STATUS_TOPIC, self._save("status")),
            (VehicleLocalPosition, POSITION_TOPIC, self._save("position")),
            (FailsafeFlags, "/fmu/out/failsafe_flags", self._save("flags")),
            (VehicleCommandAck, "/fmu/out/vehicle_command_ack", self._ack),
        ):
            node.create_subscription(kind, topic, callback, qos_profile_sensor_data)

    def _save(self, key):
        def receive(message):
            self.data[key] = (message, time.monotonic())
        return receive

    def _read(self, key, timeout):
        sample = self.data.get(key)
        if sample and time.monotonic() - sample[1] < timeout:
            return sample[0]
        return None

    def _isolated(self):
        return (
            self.node.count_publishers(STATUS_TOPIC) == 1
            and self.node.count_publishers(POSITION_TOPIC) == 1
            and self.command.get_subscription_count() == 1
        )

    def _ready_to_dispatch(self, status):
        endpoints = self.node.get_subscriptions_info_by_topic(TARGET_TOPIC)
        return (
            not self.dispatched and status.pre_flight_checks_pass
            and status.arming_state == VehicleStatus.ARMING_STATE_DISARMED
            and len(endpoints) == 1 and endpoints[0].node_name == "vtol_offboard_mission"
            and self.target.get_subscription_count() == 1 and self._isolated()
        )

    def _can_return(self, status, valid):
        return bool(
            status and valid and self.dispatched and not self.rtl_requested and not self.rtl_entered
            and status.nav_state != NAV_RTL and status.arming_state == VehicleStatus.ARMING_STATE_ARMED
            and self._isolated()
        )

    def _valid_position(self, position):
        return position if (
            position and position.xy_valid and position.z_valid
            and all(math.isfinite(x) for x in (position.x, position.y, position.z))
        ) else None

    def _ack(self, ack):
        if self.rtl_requested and ack.command == RTL:
            self.rtl_ack = int(ack.result)
            if self.rtl_ack not in (0, 5):
                self.rtl_requested = False
                self.message = f"PX4가 귀환 요청을 거절했어요. 응답 코드 {self.rtl_ack}."

    def request_return(self):
        status = self._read("status", STATUS_TIMEOUT)
        position = self._valid_position(self._read("position", POSITION_TIMEOUT))
        self.return_response = True
        if not self._can_return(status, position is not None):
            self.message = "현재 PX4 위치와 시동 상태로 귀환을 요청할 수 없어요."
            return
        self.command.publish(VehicleCommand(
            command=RTL, from_external=True, target_system=status.system_id,
            target_component=status.component_id, source_system=1, source_component=191,
            timestamp=self.node.get_clock().now().nanoseconds // 1000,
        ))
        self.rtl_requested = True
        self.rtl_sent_at = time.monotonic()
        self.rtl_ack = None
        self.message = None

    def sample(self):
        position_sample = self._read("position", POSITION_TIMEOUT)
        position = self._valid_position(position_sample)
        status = self._read("status", STATUS_TIMEOUT)
        flags = self._read("flags", STATUS_TIMEOUT)
        fresh = position_sample is not None
        armed = bool(status and status.arming_state == VehicleStatus.ARMING_STATE_ARMED)
        self.armed_once |= armed
        if status and position and self._ready_to_dispatch(status):
            self.home = (position.x, position.y, position.z)
            self.target.publish(Point(x=self.args.north, y=self.args.east, z=self.args.altitude))
            self.dispatched = True
        if status and status.nav_state == NAV_RTL:
            self.rtl_entered = True
            self.message = None
        if self.rtl_requested and not self.rtl_entered and time.monotonic() - self.rtl_sent_at > 15:
            self.rtl_requested = False
            self.message = "PX4가 귀환 모드 진입을 확인하지 않았어요."
        altitude = self.home[2] - position.z if position and self.home else None
        home_distance = (
            math.hypot(position.x - self.home[0], position.y - self.home[1]) if position and self.home else None
        )
        target_distance = (
            math.hypot(position.x - self.args.north, position.y - self.args.east) if position else None
        )
        fixed = bool(status and status.vehicle_type == VehicleStatus.VEHICLE_TYPE_FIXED_WING)
        transitioning = bool(status and status.in_transition_mode)
        if self.rtl_entered and status and not armed and altitude is not None and abs(altitude) <= 3 and home_distance <= 10:
            phase = "landed"
        elif not fresh:
            phase = "signal_lost" if self.dispatched else "waiting"
        elif status is None:
            phase = "status_delayed" if self.dispatched else "waiting"
        elif status.failsafe:
            phase = "failsafe"
        elif armed and status.nav_state == NAV_RTL:
            phase = "returning"
        elif not armed:
            phase = "disarmed" if self.armed_once else ("arming" if self.dispatched else "waiting")
        elif transitioning:
            phase = "transition"
        elif fixed and target_distance is not None:
            phase = "patrol" if target_distance <= 100 else "cruise"
        else:
            phase = "takeoff"
        message = self.message
        if phase == "status_delayed":
            message = "PX4 위치 신호는 수신 중이에요. 비행 상태를 갱신하고 있어요."
        if phase == "waiting" and status and not status.pre_flight_checks_pass:
            message = "PX4 시동 전 점검이 완료되면 입력한 임무를 전송해요."
        if phase == "failsafe":
            if flags and flags.local_position_invalid:
                message = "PX4 페일세이프 · 위치 추정 무효"
            elif flags and flags.offboard_control_signal_lost:
                message = "PX4 페일세이프 · Offboard 신호 끊김"
            else:
                message = "PX4가 페일세이프에 진입했어요."
        event = {
            "phase": phase, "fresh": fresh, "armed": armed,
            "nav": status.nav_state if status else None,
            "offboard": bool(status and status.nav_state == NAV_OFFBOARD),
            "fixed_wing": fixed, "transitioning": transitioning,
            "position_valid": position is not None,
            "north": round(position.x, 2) if position else None,
            "east": round(position.y, 2) if position else None,
            "altitude": round(altitude, 2) if altitude is not None else None,
            "target_distance": round(target_distance, 2) if target_distance is not None else None,
            "home_distance": round(home_distance, 2) if home_distance is not None else None,
            "can_return": self._can_return(status, position is not None),
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
    args = parser.parse_args()
    start_world()
    rclpy.init()
    node = rclpy.create_node("maritime_web_console")
    flight = Flight(node, args)
    next_sample = 0
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
            if now >= next_sample:
                emit("telemetry", data=flight.sample())
                next_sample = now + 0.7
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        emit("error", message=str(error))
        raise
