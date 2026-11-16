"""Isolated local API process crash/restart and persisted-account recovery probe."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.serve_resilient import stop


def run(args):
    import httpx
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Isolated port occupied")
    env = dict(os.environ, MYSQL_URL="sqlite+pysqlite:///"+(output/"accounts.sqlite").as_posix(),
        WENCE_AUTH_SECRET="isolated-resilience-"+uuid4().hex,
        WENCE_SESSION_STORE_PATH=str(output/"sessions.sqlite"),
        WENCE_RESEARCH_CACHE_PATH=str(output/"research.sqlite"), PYTHONIOENCODING="utf-8")
    log_path = output/"supervisor.log"
    process = subprocess.Popen([sys.executable,"-B",str(ROOT/"scripts/serve_resilient.py"),
        "--port",str(args.port),"--startup-grace","2","--interval","1","--log",str(log_path)],
        cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    base = f"http://127.0.0.1:{args.port}"
    report = {"scope":"isolated_same_host_process_recovery_with_SQLite_no_market_or_model_calls",
              "production_sla_verified":False,"passed":False,"samples":[]}
    try:
        with httpx.Client(timeout=3,trust_env=False) as client:
            deadline = time.monotonic()+40
            while True:
                try:
                    if client.get(base+"/api/v1/readyz").status_code==200: break
                except httpx.HTTPError: pass
                if time.monotonic()>deadline: raise RuntimeError("Test service not ready")
                time.sleep(.25)
            username = "resilience_"+uuid4().hex[:8]
            credentials = {"username":username,"password":"Isolated-resilience-test-2026!"}
            registration=client.post(base+"/api/v1/auth/register",json=credentials)
            registration.raise_for_status()
            token=registration.json()["access_token"]
            events=[json.loads(line[len("SUPERVISOR "):]) for line in log_path.read_text(encoding="utf-8").splitlines() if line.startswith("SUPERVISOR ")]
            # Only kill the PID created by this isolated supervisor, never an existing service.
            owned_pid=events[-1]["pid"]
            if os.name != "nt": raise RuntimeError("This recovery harness currently targets Windows")
            crash=time.monotonic()
            killed=subprocess.run(["taskkill","/PID",str(owned_pid),"/T","/F"],capture_output=True)
            if killed.returncode: raise RuntimeError("Failed to inject isolated process crash")
            recovered=None
            while time.monotonic()-crash<args.duration:
                elapsed=time.monotonic()-crash
                try: ok=client.get(base+"/api/v1/readyz").status_code==200
                except httpx.HTTPError: ok=False
                report["samples"].append({"elapsed_seconds":round(elapsed,3),"ready":ok})
                if ok and recovered is None: recovered=time.monotonic()-crash
                time.sleep(1)
            login=client.post(base+"/api/v1/auth/login",json=credentials)
            login.raise_for_status()
            new_token=login.json()["access_token"]
            # Same-host shared login leases survive a worker/process restart.
            old_session=client.get(base+"/api/v1/history",headers={"Authorization":"Bearer "+token})
            report.update(recovery_seconds=round(recovered,3) if recovered else None,
                observation_seconds=round(time.monotonic()-crash,3),persisted_account_recovered=True,
                original_session_recovered=old_session.status_code==200,
                observed_readiness_success_rate=sum(s["ready"] for s in report["samples"])/len(report["samples"]))
            for t in (token,new_token): client.post(base+"/api/v1/auth/logout",headers={"Authorization":"Bearer "+t})
            report["passed"]=bool(recovered is not None and recovered<30 and report["original_session_recovered"])
    finally:
        stop(process)
        report["service_stopped"]=True
        (output/"resilience.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="samples"},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port",type=int,default=8037)
    p.add_argument("--duration",type=float,default=60)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    if args.duration<10: p.error("duration must be at least 10 seconds")
    raise SystemExit(run(args))
