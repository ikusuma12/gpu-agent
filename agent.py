import os
import subprocess
from fastapi import FastAPI, HTTPException
import uvicorn

app = FastAPI(title="Windows GPU Node Agent")

# GPUごとの設定（GPU -> サービス(sd / ollama) の2階層構造）
TARGETS = {
    "4070tis": {
        "sd": {
            "bat": r"F:\stablediffusion\Data\Packages\SD forge 4070tis\start.bat",
            "dir": r"F:\stablediffusion\Data\Packages\SD forge 4070tis",
            "port": 7860
        },
        # 将来4070tis側でもOllamaを動かす場合はここに追加可能
    },
    "5060ti": {
        "sd": {
            "bat": r"F:\stablediffusion\Data\Packages\SD forge 5060ti\start.bat",
            "dir": r"F:\stablediffusion\Data\Packages\SD forge 5060ti",
            "port": 7863
        },
        "ollama": {
            # 実在するファイル名（5060ti_server.bat）に修正
            "bat": r"C:\project\AI\ollama\5060ti_server.bat",
            "dir": r"C:\project\AI\ollama",
            "port": 11435
        }
    }
}

# 起動中プロセスのハンドル保持用: running_processes[gpu_name] = {"service": service_name, "proc": Popen}
# ※GPU単位で1プロセスのみ保持（同一GPUでのSDとOllamaの排他管理）
running_processes = {}

def _kill_process(proc: subprocess.Popen):
    """バッチ配下のpython/ollamaも含めてツリーごと強制終了"""
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/status/{gpu_name}")
def get_status(gpu_name: str):
    """指定したGPUで現在何が動いているかを取得"""
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")
    
    current = running_processes.get(gpu_name)
    if current is not None and current["proc"].poll() is None:
        return {
            "gpu": gpu_name,
            "running": True,
            "service": current["service"],
            "pid": current["proc"].pid
        }
    return {"gpu": gpu_name, "running": False, "service": None, "pid": None}

@app.post("/start/{gpu_name}/{service_name}")
def start_service(gpu_name: str, service_name: str):
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")
    if service_name not in TARGETS[gpu_name]:
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_name}' not configured for {gpu_name}",
        )

    current = running_processes.get(gpu_name)

    # 既に同じサービスが動いている場合はスルー
    if current is not None and current["proc"].poll() is None:
        if current["service"] == service_name:
            return {
                "status": "already_running",
                "service": service_name,
                "pid": current["proc"].pid,
            }
        else:
            # 別のサービスが動いている場合は停止
            _kill_process(current["proc"])
            running_processes.pop(gpu_name, None)

    cfg = TARGETS[gpu_name][service_name]

    # もともと動いていた方式に戻す
    new_proc = subprocess.Popen(
        ["cmd.exe", "/c", cfg["bat"]],
        cwd=cfg["dir"],
        env=os.environ.copy(),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )

    running_processes[gpu_name] = {"service": service_name, "proc": new_proc}

    return {
        "status": "started",
        "gpu": gpu_name,
        "service": service_name,
        "pid": new_proc.pid,
    }

@app.post("/stop/{gpu_name}/{service_name}")
def stop_service(gpu_name: str, service_name: str):
    """指定したGPUの特定サービスを停止"""
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")
    
    current = running_processes.get(gpu_name)
    if current is not None and current["proc"].poll() is None:
        if current["service"] == service_name:
            _kill_process(current["proc"])
            running_processes.pop(gpu_name, None)
            return {"status": "stopped", "gpu": gpu_name, "service": service_name}
        else:
            return {"status": "not_running", "detail": f"Currently running service is '{current['service']}', not '{service_name}'"}

    return {"status": "not_running"}

@app.post("/stop/{gpu_name}")
def stop_gpu(gpu_name: str):
    """GPU指定のみで、現在動いているサービスを何であれ停止・VRAM解放 (freeフラグ用)"""
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")
    
    current = running_processes.get(gpu_name)
    if current is not None and current["proc"].poll() is None:
        _kill_process(current["proc"])
        stopped_service = current["service"]
        running_processes.pop(gpu_name, None)
        return {"status": "stopped", "gpu": gpu_name, "service": stopped_service}

    return {"status": "not_running"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)