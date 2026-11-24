import logging
import os
from pathlib import Path
import subprocess
import time

from .sim_runtime import VEHICLE


LOG = logging.getLogger(__name__)
SESSION_VARIABLES = {
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
}


def desktop_environment():
    env = os.environ.copy()
    try:
        session = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                                 text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        for line in session.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in SESSION_VARIABLES and value:
                env[key] = value
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    return None


def open_ground_control(log_dir, env):
    launcher = Path.home() / ".local/bin/qgroundcontrol"
    if not launcher.is_file():
        LOG.info("QGroundControl launcher not found at %s", launcher)
        return None

    try:
        running = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "QGroundControl"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        if running.returncode == 0:
            return None

        env = env.copy()
        env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
        with (Path(log_dir) / "qgroundcontrol.log").open("ab") as log:
            return subprocess.Popen([str(launcher)], env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        LOG.warning("Could not open QGroundControl for %s", log_dir, exc_info=True)
        return None


def follow_vehicle(launcher, env, log):
    deadline = time.monotonic() + 24
    while time.monotonic() < deadline:
        result = subprocess.run(
            [str(launcher), "exec", "gz", "service", "-s", "/gui/follow",
             "--reqtype", "gz.msgs.StringMsg", "--reptype", "gz.msgs.Boolean", "--timeout", "1600",
             "--req", f'data: "{VEHICLE}"'],
            env=env, stdout=subprocess.PIPE, stderr=log, timeout=5,
        )
        if result.returncode == 0 and b"data: true" in result.stdout:
            break
        time.sleep(0.5)
    else:
        return
    subprocess.run(
        [str(launcher), "exec", "gz", "topic", "-t", "/gui/track", "-m", "gz.msgs.CameraTrack", "-p",
         f'track_mode: FOLLOW follow_target {{name: "{VEHICLE}"}} '
         'follow_offset {x:-15 y:-8 z:7} follow_pgain:0.45'],
        env=env, stdout=log, stderr=subprocess.STDOUT, timeout=5, check=True,
    )


def open_desktop_views(launcher, log_dir):
    env = desktop_environment()
    if env is None:
        return None

    ground_control = open_ground_control(log_dir, env)
    try:
        with (Path(log_dir) / "gazebo-launch.log").open("ab") as log:
            subprocess.run([str(launcher), "gui"], env=env, stdin=subprocess.DEVNULL,
                           stdout=log, stderr=subprocess.STDOUT, timeout=10, check=True)
            follow_vehicle(launcher, env, log)
    except (OSError, subprocess.SubprocessError):
        LOG.warning("Could not open the Gazebo view for %s", log_dir, exc_info=True)
    return ground_control
