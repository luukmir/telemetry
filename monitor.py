import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")

prev_cpu_idle = 0
prev_cpu_total = 0
prev_net_rx = 0
prev_net_tx = 0
prev_net_time = 0.0

# docker stats は負荷軽減のため10秒に1回だけ更新してキャッシュする
docker_cache = []
last_docker_check = 0.0

def get_uptime_and_load():
    with open("/proc/uptime", "r") as f:
        uptime_sec = float(f.read().split()[0])
    days = int(uptime_sec // 86400)
    hours = int((uptime_sec % 86400) // 3600)
    mins = int((uptime_sec % 3600) // 60)
    uptime_str = f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"

    with open("/proc/loadavg", "r") as f:
        loads = f.read().split()[:3]
    return {"uptime": uptime_str, "load": ", ".join(loads)}

def get_cpu_usage():
    global prev_cpu_idle, prev_cpu_total
    with open("/proc/stat", "r") as f:
        line = f.readline()
    fields = [float(col) for col in line.strip().split()[1:]]
    idle = fields[3] + fields[4]
    total = sum(fields)

    diff_idle = idle - prev_cpu_idle
    diff_total = total - prev_cpu_total
    prev_cpu_idle, prev_cpu_total = idle, total

    if diff_total == 0:
        return 0.0
    return round((1.0 - diff_idle / diff_total) * 100, 1)

def get_cpu_temp():
    thermal_base = "/sys/class/thermal"
    if not os.path.exists(thermal_base):
        return None
    temps = []
    # x86_pkg_temp を優先し、なければ最も高い温度ゾーンを採用
    for entry in os.listdir(thermal_base):
        if entry.startswith("thermal_zone"):
            zone_path = os.path.join(thermal_base, entry)
            try:
                with open(os.path.join(zone_path, "type"), "r") as f:
                    ztype = f.read().strip()
                with open(os.path.join(zone_path, "temp"), "r") as f:
                    val = int(f.read().strip()) / 1000.0
                if "pkg" in ztype or "acpitz" in ztype or "core" in ztype:
                    return round(val, 1)
                if 10 < val < 120:
                    temps.append(val)
            except Exception:
                pass
    return round(max(temps), 1) if temps else None

def get_mem_usage():
    meminfo = {}
    with open("/proc/meminfo", "r") as f:
        for line in f:
            parts = line.split()
            meminfo[parts[0].rstrip(":")] = int(parts[1])
    total = meminfo.get("MemTotal", 1)
    available = meminfo.get("MemAvailable", 0)
    used = total - available
    return {
        "used_gb": round(used / 1024 / 1024, 2),
        "total_gb": round(total / 1024 / 1024, 2),
        "percent": round((used / total) * 100, 1)
    }

def get_disk_usage():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    total_avail = used + free
    percent = (used / total_avail) * 100 if total_avail > 0 else 0.0
    return {
        "used_gb": round(used / (1024**3), 1),
        "total_gb": round(total / (1024**3), 1),
        "percent": round(percent, 1)
    }

def get_battery_status():
    base_path = "/sys/class/power_supply"
    if not os.path.exists(base_path):
        return None
    for entry in os.listdir(base_path):
        if entry.startswith("BAT"):
            bat_path = os.path.join(base_path, entry)
            try:
                with open(os.path.join(bat_path, "capacity"), "r") as f:
                    capacity = int(f.read().strip())
                with open(os.path.join(bat_path, "status"), "r") as f:
                    status = f.read().strip()

                health = None
                for full_name, design_name in [("energy_full", "energy_full_design"), ("charge_full", "charge_full_design")]:
                    f_path = os.path.join(bat_path, full_name)
                    d_path = os.path.join(bat_path, design_name)
                    if os.path.exists(f_path) and os.path.exists(d_path):
                        with open(f_path, "r") as ff, open(d_path, "r") as df:
                            full_val = int(ff.read().strip())
                            design_val = int(df.read().strip())
                            if design_val > 0:
                                health = round((full_val / design_val) * 100, 1)
                        break

                return {"capacity": capacity, "status": status, "health": health}
            except Exception:
                pass
    return None

def get_network_speed():
    global prev_net_rx, prev_net_tx, prev_net_time
    now = time.monotonic()
    rx_bytes = 0
    tx_bytes = 0
    with open("/proc/net/dev", "r") as f:
        for line in f.readlines()[2:]:
            parts = line.split()
            iface = parts[0].rstrip(":")
            # ループバック・Docker仮想ブリッジ・veth・Tailscale仮想IFの二重カウントを除外
            if iface == "lo" or iface.startswith(("docker", "br-", "veth", "tailscale")):
                continue
            rx_bytes += int(parts[1])
            tx_bytes += int(parts[9])

    if prev_net_time == 0.0:
        prev_net_rx, prev_net_tx, prev_net_time = rx_bytes, tx_bytes, now
        return {"rx_mbps": 0.0, "tx_mbps": 0.0}

    dt = now - prev_net_time
    if dt <= 0:
        return {"rx_mbps": 0.0, "tx_mbps": 0.0}

    rx_mbps = round(((rx_bytes - prev_net_rx) * 8) / dt / 1_000_000, 2)
    tx_mbps = round(((tx_bytes - prev_net_tx) * 8) / dt / 1_000_000, 2)
    prev_net_rx, prev_net_tx, prev_net_time = rx_bytes, tx_bytes, now
    return {"rx_mbps": max(0.0, rx_mbps), "tx_mbps": max(0.0, tx_mbps)}

def get_top_processes():
    cmd = ["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    lines = res.stdout.strip().split("\n")[1:11]
    procs = []
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs.append({"pid": parts[0], "name": parts[1], "cpu": parts[2], "mem": parts[3]})
    return procs

def get_docker_containers():
    global docker_cache, last_docker_check
    now = time.monotonic()
    if now - last_docker_check < 10.0 and docker_cache:
        return docker_cache

    try:
        # ステータス取得
        ps_cmd = ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"]
        ps_res = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=3)
        if ps_res.returncode != 0:
            return docker_cache

        status_map = {}
        for line in ps_res.stdout.strip().split("\n"):
            if line:
                name, status, image = line.split("\t")
                status_map[name] = {"status": status, "image": image}

        # リソース消費量取得
        stats_cmd = ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"]
        stats_res = subprocess.run(stats_cmd, capture_output=True, text=True, timeout=4)
        stats_map = {}
        if stats_res.returncode == 0:
            for line in stats_res.stdout.strip().split("\n"):
                if line:
                    name, cpu, mem = line.split("\t")
                    stats_map[name] = {"cpu": cpu, "mem": mem.split("/")[0].strip()}

        containers = []
        for name, info in status_map.items():
            st = stats_map.get(name, {"cpu": "--", "mem": "--"})
            containers.append({
                "name": name,
                "image": info["image"],
                "status": info["status"],
                "cpu": st["cpu"],
                "mem": st["mem"]
            })

        docker_cache = containers
        last_docker_check = now
        return containers
    except Exception:
        return docker_cache

class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(HTML_PATH, "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = {
                        "sys": get_uptime_and_load(),
                        "cpu": get_cpu_usage(),
                        "temp": get_cpu_temp(),
                        "mem": get_mem_usage(),
                        "disk": get_disk_usage(),
                        "battery": get_battery_status(),
                        "net": get_network_speed(),
                        "processes": get_top_processes(),
                        "docker": get_docker_containers(),
                    }
                    msg = f"data: {json.dumps(payload)}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8099), MonitorHandler)
    print("Monitoring server running on http://0.0.0.0:8099")
    server.serve_forever()
