"""Start the installed Gazebo world and PX4 binary for a web-owned run.

The web runs use PX4's native navigation and never start the separate Offboard
mission node. That node remains available through the original launcher.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree

WORLD = "sea_mission"
CAMERA_TOPIC = "/camera"
VEHICLE = "standard_vtol_gimbal_0"
GIMBAL_COMMAND = f"/model/{VEHICLE}/command"


def camera_resources(log_dir, project):
    # The virtual machine renders in software. Keep the navigation sensors and the
    # original world intact, and use a lower-rate patrol camera for this run.
    original = project / "models/gimbal"
    if not (original / "model.sdf").is_file():
        original = Path(os.environ["PX4_GZ_MODELS"]) / "gimbal"
    resources = log_dir / "resources"
    gimbal = resources / "gimbal"
    gimbal.mkdir(parents=True)
    model = ElementTree.parse(original / "model.sdf")
    sensor = model.find(".//sensor[@name='camera']")
    for name, value in {
        "update_rate": "8", "camera/image/width": "640", "camera/image/height": "360",
        "camera/horizontal_fov": "0.72", "visualize": "false",
    }.items():
        sensor.find(name).text = value
    model.write(gimbal / "model.sdf", encoding="utf-8", xml_declaration=True)
    shutil.copyfile(original / "model.config", gimbal / "model.config")
    for source in original.iterdir():
        if source.is_dir():
            (gimbal / source.name).symlink_to(source, target_is_directory=True)
    return resources


def run(log_dir):
    home = Path.home()
    project = Path(__file__).resolve().parent.parent
    build = home / "PX4-Autopilot/build/px4_sitl_default"
    state = log_dir / "px4"
    state.mkdir()
    binary = build / "bin/px4"
    world = Path(os.environ["PX4_GZ_WORLDS"]) / f"{WORLD}.sdf"
    if not world.is_file() or not binary.is_file():
        raise RuntimeError("설치된 PX4 또는 Gazebo 해안 월드를 찾을 수 없어요.")
    env = os.environ.copy()
    resources = camera_resources(log_dir, project)
    env["GZ_SIM_RESOURCE_PATH"] = str(resources) + os.pathsep + env.get("GZ_SIM_RESOURCE_PATH", "")
    children = {}

    def start(name, args, **options):
        with (log_dir / f"{name}.log").open("ab") as log:
            children[name] = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **options
            )

    try:
        start("agent", ["MicroXRCEAgent", "udp4", "-p", "8888"])
        start("gazebo", ["gz", "sim", "--verbose=1", "-r", "-s", str(world)], env=env)
        start("sitl", [str(binary), "-d", "-w", str(state), str(build / "etc")], cwd=state, env={
            **env, "PX4_GZ_STANDALONE": "1", "PX4_GZ_WORLD": WORLD,
            "PX4_SIM_MODEL": "gz_standard_vtol_gimbal",
        })
        start("bridge", ["ros2", "run", "ros_gz_bridge", "parameter_bridge",
                         f"{CAMERA_TOPIC}@sensor_msgs/msg/Image[gz.msgs.Image"])
        while True:
            for name, process in children.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} 종료 · {name}.log 확인")
            time.sleep(0.5)
    finally:
        for process in reversed(list(children.values())):
            if process.poll() is None:
                process.terminate()
        for process in children.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    run(Path(sys.argv[1]))
