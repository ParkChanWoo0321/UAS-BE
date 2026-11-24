"""Point the simulated gimbal toward the mission coordinate using Gazebo pose."""

from math import atan2, hypot, pi, remainder

from gz.msgs10.double_pb2 import Double
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.transport13 import Node

from sim_runtime import GIMBAL_COMMAND, VEHICLE, WORLD


class PatrolGimbal:
    def __init__(self, north, east):
        self.north, self.east = north, east
        self.pose = None
        self.yaw = 0.0
        self.node = Node()
        self.pitch_out = self.node.advertise(f"{GIMBAL_COMMAND}/gimbal_pitch", Double)
        self.yaw_out = self.node.advertise(f"{GIMBAL_COMMAND}/gimbal_yaw", Double)
        self.node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._pose)

    def _pose(self, message):
        for pose in message.pose:
            if pose.name == VEHICLE:
                self.pose = pose
                break

    def update(self):
        pose = self.pose
        if pose is None:
            return
        offset = self.east - pose.position.x, self.north - pose.position.y, 1.0 - pose.position.z
        x, y, z = self._body_vector(offset, pose.orientation)
        desired_yaw = -atan2(y, x)
        self.yaw += remainder(desired_yaw - self.yaw, 2 * pi)
        self.yaw_out.publish(Double(data=self.yaw))
        self.pitch_out.publish(Double(data=max(-2.3, min(0.75, atan2(z, hypot(x, y))))))

    @staticmethod
    def _body_vector(vector, rotation):
        # Apply the inverse of the unit quaternion to the world direction.
        u, v, w = -rotation.x, -rotation.y, -rotation.z
        x, y, z = vector
        a, b, c = 2 * (v*z - w*y), 2 * (w*x - u*z), 2 * (u*y - v*x)
        return (x + rotation.w*a + v*c - w*b,
                y + rotation.w*b + w*a - u*c,
                z + rotation.w*c + u*b - v*a)
