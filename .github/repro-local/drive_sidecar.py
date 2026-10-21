"""Temporary: run a released translator-sidecar.exe and drive the one-click installs."""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

exe = sys.argv[1]
components = sys.argv[2:] or ["pdf2docx"]
parent = os.environ.get("REPRO_DATA_PARENT")
if parent:
    os.makedirs(parent, exist_ok=True)
data_dir = tempfile.mkdtemp(prefix="sidecar-data-", dir=parent or None)
print("data dir:", data_dir)
env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": data_dir}
proc = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                        encoding="utf-8", errors="replace")
line = ""
deadline = time.time() + 120
while time.time() < deadline:
    line = proc.stdout.readline()
    print("sidecar:", line.rstrip())
    if line.startswith("PORT="):
        break
port, token = (part.split("=", 1)[1] for part in line.split())
base = f"http://127.0.0.1:{port}"


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"X-Translator-Token": token, "Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode("utf-8", "replace")}


failed = False
for name in components:
    print(f"=== {name}: status before", json.dumps(call("GET", f"/api/plugins/{name}"), ensure_ascii=False)[:600])
    print(f"=== {name}: start", call("POST", f"/api/plugins/{name}/one-click-install"))
    started = time.time()
    last = None
    while time.time() - started < 1500:
        state = call("GET", f"/api/plugins/{name}/install-progress")
        if state != last:
            print(f"  {int(time.time() - started)}s", json.dumps(state, ensure_ascii=False))
            last = state
        if not state.get("running"):
            break
        time.sleep(3)
    if state.get("phase") != "done":
        failed = True
    print(f"=== {name}: status after", json.dumps(call("GET", f"/api/plugins/{name}"), ensure_ascii=False)[:1500])

proc.terminate()
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
for log in glob.glob(os.path.join(data_dir, "**", "*.log"), recursive=True) + glob.glob(os.path.join(data_dir, "**", "*.log.*"), recursive=True):
    print(f"===== {log}")
    with open(log, encoding="utf-8", errors="replace") as handle:
        print(handle.read()[-20000:])
for root, dirs, files in os.walk(os.path.join(data_dir, "plugins")):
    depth = root[len(data_dir):].count(os.sep)
    if depth <= 4:
        print("  dir:", root, len(files), "files")
sys.exit(1 if failed else 0)
