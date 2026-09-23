import os
import subprocess
from fastapi import FastAPI, HTTPException
import uvicorn

app = FastAPI(title="Windows GPU Node Agent")

# GPUごとの設定（パスはWindowsネイティブ形式）
TARGETS = {
    "4070tis": {
        "bat": r"F:\stablediffusion\Data\Packages\SD forge 4070tis\start.bat",
        "dir": r"F:\stablediffusion\Data\Packages\SD forge 4070tis",
        "port": 7860
    },
    "5060ti": {
        "bat": r"F:\stablediffusion\Data\Packages\SD forge 5060ti\start.bat",
        "dir": r"F:\stablediffusion\Data\Packages\SD forge 5060ti",
        "port": 7863
    }
}

# 起動中プロセスのハンドル保持用
running_processes = {}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/status/{gpu_name}")
def get_status(gpu_name: str):
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")
    
    proc = running_processes.get(gpu_name)
    if proc is not None and proc.poll() is None:
        return {"gpu": gpu_name, "running": True, "pid": proc.pid}
    return {"gpu": gpu_name, "running": False, "pid": None}

@app.post("/start/{gpu_name}")
def start_gpu(gpu_name: str):
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")

    proc = running_processes.get(gpu_name)
    if proc is not None and proc.poll() is None:
        return {"status": "already_running", "pid": proc.pid}

    cfg = TARGETS[gpu_name]

    # Windowsネイティブで新しいプロセスグループとして起動
    # CREATE_NEW_CONSOLE: 独立したウィンドウで起動（デバッグ時はコンソールが見えて便利）
    # バックグラウンド化したい場合は DETACHED_PROCESS などを利用
    new_proc = subprocess.Popen(
        ["cmd.exe", "/c", cfg["bat"]],
        cwd=cfg["dir"],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )
    running_processes[gpu_name] = new_proc

    return {"status": "started", "pid": new_proc.pid}

@app.post("/stop/{gpu_name}")
def stop_gpu(gpu_name: str):
    """バッチ配下のpythonも含めてツリーごとkill"""
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")

    proc = running_processes.get(gpu_name)
    if proc is not None and proc.poll() is None:
        # taskkill /F /T で子プロセス(python)ごと強制終了
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        running_processes.pop(gpu_name, None)
        return {"status": "stopped"}

    return {"status": "not_running"}

if __name__ == "__main__":
    # ミニPC等から叩けるよう 0.0.0.0 でリッスン
    uvicorn.run(app, host="0.0.0.0", port=8001)