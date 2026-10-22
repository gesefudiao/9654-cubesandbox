#!/usr/bin/env python3
"""Portable, approval-gated CubeSandbox one-click deployment and benchmark entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
ARTIFACT_RECORD = ROOT / "artifacts/artifact.json"
TEMPLATE_RECORD = ROOT / "artifacts/template.json"
OWNED = ROOT / "artifacts/owned-sandboxes.json"
PHRASES = {"deploy": "确认本次部署", "template": "确认本次模板创建", "bench": "确认本次性能测试"}
SERVICES = ["cube-sandbox-cubelet.service", "cube-sandbox-cubemaster.service",
            "cube-sandbox-cube-api.service"]


def stamp():
    return datetime.now().astimezone().isoformat()


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)


def sha(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, *, cwd=None, timeout=20, check=True):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                          timeout=timeout, check=check)


def command_log(args, logfile: Path, *, cwd=None, env=None):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("w") as log:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                log.write(line)
        finally:
            code = proc.wait()
    if code:
        raise RuntimeError(f"command failed ({code}): {args}; log={logfile}")


def config():
    if not CONFIG.exists():
        raise RuntimeError(f"copy config.example.json to {CONFIG} and edit it first")
    value = json.loads(CONFIG.read_text())
    source = Path(value["source_dir"]).expanduser().resolve()
    if not source.is_dir():
        raise RuntimeError(f"source_dir does not exist: {source}")
    value["source_dir"] = str(source)
    return value


def git_identity(source: Path):
    commit = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    dirty = run(["git", "status", "--porcelain"], cwd=source).stdout.strip()
    return {"commit": commit, "dirty": bool(dirty), "dirty_files": dirty.splitlines()[:20]}


def source_check(cfg, blockers):
    source = Path(cfg["source_dir"])
    ident = git_identity(source)
    if ident["dirty"]:
        blockers.append("source worktree is dirty")
    expected = cfg.get("expected_git_ref", "")
    if expected:
        ref_sha = run(["git", "rev-parse", expected], cwd=source).stdout.strip()
        if ref_sha != ident["commit"]:
            blockers.append(f"source HEAD differs from {expected}")
    if not (source / "deploy/one-click/build-release-bundle-builder.sh").exists():
        blockers.append("one-click source build entry missing")
    return ident


def http_json(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers or {})
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def inventory(cfg):
    key = cfg.get("api_key", "")
    headers = {"Authorization": "Bearer " + key} if key else {}
    data = http_json(cfg["api_url"].rstrip("/") + "/sandboxes", headers)
    if not isinstance(data, list):
        raise RuntimeError("GET /sandboxes did not return a list")
    return data


def sandbox_id(row):
    return next((row[k] for k in ("sandboxID", "sandboxId", "sandbox_id", "id")
                 if isinstance(row, dict) and row.get(k)), None)


def template_info(cfg):
    name = cfg.get("template_id") or cfg.get("template_alias")
    if not name:
        return None
    cli = shutil.which("cubemastercli")
    if not cli:
        return None
    result = run([cli, "--address", "127.0.0.1", "--port", "8089",
                  "template", "info", name, "--json"], timeout=15, check=False)
    if result.returncode:
        return {"name": name, "error": result.stderr[-350:]}
    try:
        value = json.loads(result.stdout)
        return {"name": name, "id": value.get("template_id"),
                "status": value.get("status"), "replicas": value.get("replicas", [])}
    except json.JSONDecodeError:
        return {"name": name, "error": "invalid template JSON"}


def node_health(cfg):
    data = http_json("http://127.0.0.1:8089/internal/node")
    nodes = data.get("data", [])
    ip = cfg.get("node_ip")
    nodes = [n for n in nodes if n.get("InstanceType") == "cubebox" and
             (not ip or n.get("IP") == ip)]
    return [{"ip": n.get("IP"), "healthy": n.get("Healthy"),
             "reason": n.get("UnhealthyReason"),
             "metric_update_at": n.get("MetricUpdateAt"),
             "reported_ready": n.get("ReportedReady")} for n in nodes]


def service_states():
    return {s: run(["systemctl", "is-active", s], check=False).stdout.strip()
            for s in SERVICES + ["cube-sandbox-cube-egress.service", "cube-perf-exporter.service"]}


def live_binaries(cfg):
    base = Path(cfg["install_root"])
    paths = {"cubelet": base / "Cubelet/bin/cubelet",
             "master": base / "CubeMaster/bin/cubemaster",
             "api": base / "CubeAPI/bin/cube-api",
             "shim": base / "cube-shim/bin/containerd-shim-cube-rs",
             "runtime": base / "cube-shim/bin/cube-runtime"}
    return {k: {"path": str(p), "sha256": sha(p) if p.is_file() else None}
            for k, p in paths.items()}


def meminfo():
    wanted = {"MemTotal", "MemAvailable", "Dirty", "Writeback", "SwapFree"}
    return {k: v.strip() for k, v in (line.split(":", 1) for line in
            Path("/proc/meminfo").read_text().splitlines()) if k in wanted}


def host_switches():
    result = {}
    for name, path in {"thp": "/sys/kernel/mm/transparent_hugepage/enabled",
                       "numa_balancing": "/proc/sys/kernel/numa_balancing"}.items():
        p = Path(path)
        result[name] = p.read_text().strip() if p.exists() else None
    return result


def disk_free(path):
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free


def snapshot(stage, cfg):
    blockers = []
    source = Path(cfg["source_dir"])
    ident = source_check(cfg, blockers)
    artifact = json.loads(ARTIFACT_RECORD.read_text()) if ARTIFACT_RECORD.exists() else None
    if artifact and (not Path(artifact["path"]).is_file() or
                     sha(Path(artifact["path"])) != artifact["sha256"]):
        blockers.append("built artifact missing or hash mismatch")
    states = service_states()
    disks = {"root": disk_free(Path("/")),
             "data": disk_free(Path(cfg["data_dir"])),
             "source": disk_free(source)}
    if disks["root"] < cfg["min_root_free_gib"] * 1024**3:
        blockers.append("root disk below threshold")
    if disks["data"] < cfg["min_data_free_gib"] * 1024**3:
        blockers.append("data disk below threshold")
    vms = run(["virsh", "-c", "qemu:///system", "list", "--state-running", "--name"],
              check=False).stdout.splitlines() if shutil.which("virsh") else []
    vms = [v.strip() for v in vms if v.strip()]
    qemu = [name for name in run(["ps", "-eo", "comm="], check=False).stdout.splitlines()
            if name.strip().startswith(("qemu-kvm", "qemu-system"))]
    if qemu and not vms:
        vms = ["unidentified QEMU process: " + name.strip() for name in qemu]
    if vms:
        blockers.append("other running libvirt VMs present")
    load = os.getloadavg()
    if load[0] >= cfg["load1_max"]:
        blockers.append("load1 above gate")
    arch = os.uname().machine
    if arch != "x86_64":
        blockers.append("target architecture is not x86_64")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        blockers.append("/dev/kvm unavailable")
    if stage in ("deploy", "template", "bench") and os.geteuid() != 0:
        blockers.append("run as root for deployment and host preflight")
    docker = run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False).stdout.strip() if shutil.which("docker") else None
    if stage == "deploy" and not docker:
        blockers.append("Docker daemon unavailable")
    if stage == "deploy":
        if not artifact:
            blockers.append("build artifact missing")
        if Path(cfg["install_root"]).exists():
            blockers.append("install_root already exists; automatic overwrite disabled")
    if stage in ("template", "bench"):
        if any(states[s] != "active" for s in SERVICES + ["cube-sandbox-cube-egress.service"]):
            blockers.append("required CubeSandbox service inactive")
        if not shutil.which("cubemastercli"):
            blockers.append("cubemastercli missing")
        deploy_record = ROOT / "artifacts/deployment.json"
        if not deploy_record.exists():
            blockers.append("deployment record missing")
        else:
            installed = json.loads(deploy_record.read_text())
            for name, recorded in installed.get("binaries", {}).items():
                current_path = Path(recorded["path"])
                if not current_path.is_file() or sha(current_path) != recorded["sha256"]:
                    blockers.append(f"deployed binary changed: {name}")
    api_rows = None
    if stage in ("template", "bench"):
        try:
            api_rows = inventory(cfg)
        except Exception as exc:
            blockers.append(f"inventory unavailable: {exc}")
    owned = set(json.loads(OWNED.read_text()).get("ids", [])) if OWNED.exists() else set()
    if api_rows is not None:
        ids = {sandbox_id(row) for row in api_rows}
        if None in ids:
            blockers.append("inventory contains rows without sandbox ID")
        if ids - owned:
            blockers.append("foreign sandbox inventory present")
        if stage == "template" and ids:
            blockers.append("inventory must be empty before template creation")
    tpl = template_info(cfg) if stage in ("template", "bench") else None
    if stage == "template" and cfg.get("template_id"):
        blockers.append("template_id is configured; skip template creation stage")
    if stage == "template" and tpl and tpl.get("id"):
        blockers.append("configured template already exists; use it or choose a new alias")
    nodes = None
    if stage in ("template", "bench"):
        try:
            nodes = node_health(cfg)
            if not nodes or not any(n["healthy"] is True for n in nodes):
                blockers.append("Master has no healthy target node")
        except Exception as exc:
            blockers.append(f"Master node health unavailable: {exc}")
    if stage == "bench":
        if not tpl or tpl.get("status") != "READY" or not any(r.get("status") == "READY" for r in tpl.get("replicas", [])):
            blockers.append("template has no READY replica")
        if states["cube-perf-exporter.service"] == "active":
            blockers.append("perf exporter active")
        if not (ROOT / ".venv/bin/python").exists():
            blockers.append("SDK virtual environment missing; run build")
        elif run([str(ROOT / ".venv/bin/python"), "-c", "import cubesandbox"], check=False).returncode:
            blockers.append("cubesandbox SDK import failed")
    pressure = {name: (Path("/proc/pressure") / name).read_text().strip()
                if (Path("/proc/pressure") / name).exists() else None
                for name in ("cpu", "memory", "io")}
    bindings = {s: run(["systemctl", "show", s, "-p", "CPUAffinity", "-p", "NUMAPolicy", "-p", "NUMAMask"], check=False).stdout.strip()
                for s in SERVICES}
    switches = host_switches()
    binaries = live_binaries(cfg)
    plan = {"deploy": "install locally built one-click bundle; no performance workload",
            "template": "create one template from configured OCI image and wait READY",
            "bench": f"clean only owned sandboxes; one functional smoke; tiers {cfg['tiers']}; {cfg['warmup_rounds']} warmup + {cfg['formal_rounds']} formal rounds per tier; no retries or sampling"}[stage]
    result = {"created_at": stamp(), "stage": stage, "status": "blocked" if blockers else "ready_for_user_confirmation",
              "blockers": blockers, "plan": plan, "config_sha256": sha(CONFIG), "source": ident,
              "artifact": artifact, "load_average": load, "arch": arch, "kvm": os.path.exists("/dev/kvm"),
              "cpu_affinity": sorted(os.sched_getaffinity(0)), "numa_online": Path("/sys/devices/system/node/online").read_text().strip() if Path("/sys/devices/system/node/online").exists() else None,
              "memory": meminfo(), "pressure": pressure, "disk_free_bytes": disks,
              "running_vms": vms, "docker": docker, "services": states,
              "service_bindings": bindings, "binaries": binaries,
              "inventory_total": len(api_rows) if api_rows is not None else None,
              "owned_inventory": sorted(owned), "template": tpl, "master_nodes": nodes,
              "host_switches": switches}
    stable = {k: result[k] for k in ("stage", "plan", "config_sha256", "source", "artifact",
              "arch", "numa_online", "services", "service_bindings", "binaries", "template", "host_switches")}
    if nodes is not None:
        stable["node_health"] = [(n["ip"], n["healthy"], n["reason"]) for n in nodes]
    result["scope_sha256"] = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    return result


def preflight(stage, cfg):
    result = snapshot(stage, cfg)
    path = ROOT / "preflight" / f"{stage}-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f')}.json"
    save(path, result)
    print(json.dumps({"preflight": str(path), "status": result["status"],
                      "blockers": result["blockers"], "plan": result["plan"],
                      "load": result["load_average"], "disk_free_gib":
                      {k: round(v / 1024**3, 2) for k, v in result["disk_free_bytes"].items()},
                      "master_nodes": result["master_nodes"]}, ensure_ascii=False, indent=2))
    return path


def approve(path: Path, phrase: str):
    if path.parent.resolve() != (ROOT / "preflight").resolve():
        raise RuntimeError("approval requires a preflight file from this kit")
    check = json.loads(path.read_text())
    if check["status"] != "ready_for_user_confirmation" or phrase != PHRASES[check["stage"]]:
        raise RuntimeError("preflight blocked or confirmation phrase does not match stage")
    out = ROOT / "approvals" / path.name
    if out.exists():
        raise RuntimeError("approval record already exists")
    save(out, {"approved_by": "user", "confirmation": phrase, "confirmed_at": stamp(),
               "preflight_path": str(path), "preflight_sha256": sha(path),
               "scope_sha256": check["scope_sha256"], "stage": check["stage"], "used_at": None})
    print(out)


def authorize(stage, path: Path, cfg):
    record = json.loads(path.read_text())
    if record["stage"] != stage or record["confirmation"] != PHRASES[stage] or record.get("used_at"):
        raise RuntimeError("approval absent, wrong scope, or already used")
    approved = Path(record["preflight_path"])
    if sha(approved) != record["preflight_sha256"]:
        raise RuntimeError("approved preflight changed")
    fresh = snapshot(stage, cfg)
    if fresh["status"] != "ready_for_user_confirmation" or fresh["scope_sha256"] != record["scope_sha256"]:
        raise RuntimeError(f"conditions changed; new preflight and confirmation required: {fresh['blockers']}")
    record["used_at"] = stamp()
    if stage == "bench":
        record["launch_nonce"] = secrets.token_hex(16)
        record["bench_started_at"] = None
    save(path, record)
    return fresh


def build(cfg):
    blockers = []
    source = Path(cfg["source_dir"])
    ident = source_check(cfg, blockers)
    if os.uname().machine != "x86_64" or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        blockers.append("x86_64 KVM host required")
    if not shutil.which("docker") or run(["docker", "info"], check=False).returncode:
        blockers.append("Docker daemon unavailable")
    if disk_free(source) < 80 * 1024**3:
        blockers.append("source filesystem needs at least 80 GiB free for build")
    if blockers:
        raise RuntimeError("build prerequisites: " + "; ".join(blockers))
    kernel = Path(cfg["kernel_file"]).expanduser() if cfg.get("kernel_file") else ROOT / "artifacts/vmlinux-amd64"
    if not kernel.exists():
        kernel.parent.mkdir(parents=True, exist_ok=True)
        url = cfg.get("kernel_url")
        if not url:
            raise RuntimeError("kernel_file or kernel_url required")
        temp = kernel.with_name(kernel.name + ".part")
        with urllib.request.urlopen(url, timeout=90) as source_stream, temp.open("wb") as dest:
            shutil.copyfileobj(source_stream, dest)
        os.replace(temp, kernel)
    if cfg.get("kernel_sha256") and sha(kernel) != cfg["kernel_sha256"]:
        raise RuntimeError("guest kernel sha256 mismatch")
    env = os.environ.copy()
    env.update(ONE_CLICK_CUBE_KERNEL_VMLINUX=str(kernel.resolve()), MIRROR=cfg.get("mirror", "cn"))
    command_log(["bash", "deploy/one-click/build-release-bundle-builder.sh"],
                ROOT / "artifacts/build.log", cwd=source, env=env)
    bundles = sorted((source / "deploy/one-click/dist").glob("cube-sandbox-one-click-*.tar.gz"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not bundles:
        raise RuntimeError("builder returned success but no one-click tarball found")
    value = {"path": str(bundles[0].resolve()), "sha256": sha(bundles[0]),
             "source_commit": ident["commit"], "kernel_sha256": sha(kernel), "built_at": stamp()}
    save(ARTIFACT_RECORD, value)
    venv = ROOT / ".venv"
    if not venv.exists():
        command_log([sys.executable, "-m", "venv", str(venv)], ROOT / "artifacts/venv.log")
    command_log([str(venv / "bin/python"), "-m", "pip", "install", "-e", str(source / "sdk/python")],
                ROOT / "artifacts/sdk-install.log")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def extract_bundle(artifact):
    dest = ROOT / "work/release"
    if dest.exists():
        raise RuntimeError("work/release already exists; no automatic overwrite")
    dest.mkdir(parents=True)
    with tarfile.open(artifact["path"], "r:gz") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise RuntimeError("tarball contains path outside release directory")
        if sys.version_info >= (3, 12):
            archive.extractall(dest, filter="data")
        else:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    link = (dest / member.linkname) if member.islnk() else (dest / member.name).parent / member.linkname
                    if not link.resolve().is_relative_to(dest.resolve()):
                        raise RuntimeError("tarball contains link outside release directory")
            archive.extractall(dest)
    installers = list(dest.rglob("install.sh"))
    installers = [p for p in installers if p.parent.name.startswith("cube-sandbox-one-click")]
    if len(installers) != 1:
        raise RuntimeError(f"expected one release install.sh, found {len(installers)}")
    return installers[0].parent


def deploy(cfg, artifact):
    release = extract_bundle(artifact)
    envfile = release / ".env"
    shutil.copy2(release / "env.example", envfile)
    with envfile.open("a") as stream:
        stream.write("\nONE_CLICK_RUN_QUICKCHECK=0\n")
        if cfg.get("node_ip"):
            if not re.fullmatch(r"[0-9.]+", cfg["node_ip"]):
                raise RuntimeError("node_ip must be a numeric IPv4 address")
            stream.write(f"CUBE_SANDBOX_NODE_IP={cfg['node_ip']}\n")
    env = os.environ.copy()
    env["MIRROR"] = cfg.get("mirror", "cn")
    command_log(["bash", "install.sh"], ROOT / "runs/deploy.log", cwd=release, env=env)
    save(ROOT / "artifacts/deployment.json", {"status": "installed", "at": stamp(),
                                            "release": str(release), "source_commit": artifact["source_commit"],
                                            "binaries": live_binaries(cfg)})


def create_template(cfg):
    cli = shutil.which("cubemastercli")
    if not cli:
        raise RuntimeError("cubemastercli not installed")
    args = [cli, "--address", "127.0.0.1", "--port", "8089", "tpl", "create-from-image",
            "--image", cfg["template_image"], "--alias", cfg["template_alias"],
            "--writable-layer-size", cfg["writable_layer_size"],
            "--expose-port", "49999", "--expose-port", "49983", "--probe", "49999"]
    command_log(args, ROOT / "runs/template-create.log")
    deadline = time.monotonic() + cfg["template_timeout_seconds"]
    while time.monotonic() < deadline:
        info = template_info(cfg)
        if info and info.get("status") == "READY" and any(r.get("status") == "READY" for r in info.get("replicas", [])):
            save(TEMPLATE_RECORD, {"created_at": stamp(), "template": info})
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return
        if info and info.get("status") in ("FAILED", "ERROR"):
            raise RuntimeError(f"template failed: {info}")
        time.sleep(10)
    raise RuntimeError("template did not become READY before timeout; no retry performed")


def run_bench(cfg, approval: Path):
    result = ROOT / "runs" / datetime.now().astimezone().strftime("pause-resume-%Y%m%dT%H%M%S")
    result.mkdir(parents=True)
    env = os.environ.copy()
    env.update(CUBE_API_URL=cfg["api_url"], CUBE_API_KEY=cfg.get("api_key", ""),
               CUBE_TEMPLATE_ID=cfg.get("template_id") or cfg["template_alias"],
               NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    if cfg.get("node_ip"):
        env.update(CUBE_PROXY_NODE_IP=cfg["node_ip"], CUBE_PROXY_PORT_HTTP=str(cfg["proxy_port"]))
    env["CUBE_BENCH_LAUNCH_NONCE"] = json.loads(approval.read_text())["launch_nonce"]
    command_log([str(ROOT / ".venv/bin/python"), str(ROOT / "bench.py"),
                 "--config", str(CONFIG), "--run-dir", str(result),
                 "--approval", str(approval)], result / "console.log", env=env)
    print("result=" + str(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("build")
    pf = sub.add_parser("preflight")
    pf.add_argument("stage", choices=PHRASES)
    ap = sub.add_parser("approve")
    ap.add_argument("preflight", type=Path)
    ap.add_argument("--phrase", required=True)
    launch = sub.add_parser("run")
    launch.add_argument("stage", choices=PHRASES)
    launch.add_argument("--approval", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "approve":
        approve(args.preflight.resolve(), args.phrase)
        return
    cfg = config()
    if args.command == "status":
        print(json.dumps({"source": git_identity(Path(cfg["source_dir"])),
                          "artifact": json.loads(ARTIFACT_RECORD.read_text()) if ARTIFACT_RECORD.exists() else None,
                          "template": template_info(cfg), "binaries": live_binaries(cfg)}, ensure_ascii=False, indent=2))
    elif args.command == "build":
        build(cfg)
    elif args.command == "preflight":
        preflight(args.stage, cfg)
    else:
        fresh = authorize(args.stage, args.approval.resolve(), cfg)
        if args.stage == "deploy":
            deploy(cfg, fresh["artifact"])
        elif args.stage == "template":
            create_template(cfg)
        else:
            run_bench(cfg, args.approval.resolve())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
