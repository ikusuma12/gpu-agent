import asyncio
from contextlib import asynccontextmanager
import os
import subprocess
from fastapi import FastAPI, HTTPException
import uvicorn

from salad_plugin import salad, salad_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(salad.profit_watchdog_loop())
    yield
    task.cancel()


app = FastAPI(title="Windows GPU Node Agent", lifespan=lifespan)

# GPUごとの設定（GPU -> サービス(sd / ollama) の2階層構造）
TARGETS = {
    "4070tis": {
        "salad_managed": True,
        "sd": {
            "bat": r"F:\stablediffusion\Data\Packages\SD forge 4070tis\start.bat",
            "dir": r"F:\stablediffusion\Data\Packages\SD forge 4070tis",
            "port": 7860,
        },
        "ollama": {
            "bat": r"C:\project\AI\ollama\4070tis_server.bat",
            "dir": r"C:\project\AI\ollama",
            "port": 11434,
        },
    },
    "5060ti": {
        "salad_managed": False,
        "sd": {
            "bat": r"F:\stablediffusion\Data\Packages\SD forge 5060ti\start.bat",
            "dir": r"F:\stablediffusion\Data\Packages\SD forge 5060ti",
            "port": 7863,
        },
        "ollama": {
            "bat": r"C:\project\AI\ollama\5060ti_server.bat",
            "dir": r"C:\project\AI\ollama",
            "port": 11435,
        },
    },
}

# 起動中プロセスのハンドル保持用: running_processes[gpu_name] = {"service": service_name, "proc": Popen}
running_processes = {}


def _kill_process(proc: subprocess.Popen):
    """バッチ配下のpython/ollamaも含めてツリーごと強制終了"""
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


# def _sync_salad_state(gpu_name: str):
#     """Salad管理対象GPUの場合、gRPC側の実フラグ状態を running_processes に反映する"""
#     if not TARGETS[gpu_name].get("salad_managed"):
#         return

#     current = running_processes.get(gpu_name)
#     # すでに sd や ollama が稼働中ならそのままにする
#     if current is not None and current["service"] != "salad":
#         if current.get("proc") is not None and current["proc"].poll() is None:
#             return

#     if salad.is_active():
#         running_processes[gpu_name] = {"service": "salad", "proc": None}
#     elif current is not None and current["service"] == "salad":
#         running_processes.pop(gpu_name, None)

def _sync_salad_state(gpu_name: str):
    """Saladの実際の状態と running_processes を同期する"""
    if not TARGETS.get(gpu_name, {}).get("salad_managed"):
        return

    st = salad.get_status().get("state", "STOPPED")
    
    # 明確に停止しているステータス以外はすべて「稼働中」とみなす
    INACTIVE_STATES = {"PAUSED", "STOPPED", "UNKNOWN", "ERROR", "OFFLINE"}
    is_active = st not in INACTIVE_STATES

    current = running_processes.get(gpu_name)
    if is_active:
        if current is None or current.get("service") == "salad":
            running_processes[gpu_name] = {"service": "salad", "proc": None}
    else:
        if current and current.get("service") == "salad":
            running_processes.pop(gpu_name, None)


def _stop_current_on_gpu(gpu_name: str) -> str | None:
    """指定GPUで稼働中のサービス（プロセス または Saladフラグ）を停止"""
    _sync_salad_state(gpu_name)
    current = running_processes.get(gpu_name)
    if current is None:
        return None

    svc = current["service"]
    if svc == "salad":
        try:
            salad.pause()
        except Exception:
            pass
    elif current.get("proc") is not None and current["proc"].poll() is None:
        _kill_process(current["proc"])

    running_processes.pop(gpu_name, None)
    return svc


@app.get("/health")
def health():
    return {"status": "ok"}


# @app.get("/status/{gpu_name}")
# def get_status(gpu_name: str):
#     """指定したGPUで現在何が動いているかを取得"""
#     if gpu_name not in TARGETS:
#         raise HTTPException(status_code=404, detail="Unknown GPU")

#     _sync_salad_state(gpu_name)
#     current = running_processes.get(gpu_name)
#     if current is not None:
#         if current["service"] == "salad":
#             return {"gpu": gpu_name, "running": True, "service": "salad", "pid": None}
#         if current["proc"] is not None and current["proc"].poll() is None:
#             return {
#                 "gpu": gpu_name,
#                 "running": True,
#                 "service": current["service"],
#                 "pid": current["proc"].pid,
#             }

#     return {"gpu": gpu_name, "running": False, "service": None, "pid": None}

@app.get("/status/{gpu_name}")
def get_status(gpu_name: str):
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")

    _sync_salad_state(gpu_name)
    current = running_processes.get(gpu_name)

    if current is not None:
        # 1. Salad の場合は proc が None でも running として返す
        if current["service"] == "salad":
            return {
                "running": True,
                "status": "running",
                "service": "salad",
                "running_service": "salad",
                "gpu": gpu_name,
                "pid": None,
            }

        # 2. SD / Ollama の場合はプロセスの生存確認
        if current["proc"] is not None and current["proc"].poll() is None:
            svc = current["service"]
            return {
                "running": True,
                "status": "running",
                "service": svc,
                "running_service": svc,
                "gpu": gpu_name,
                "pid": current["proc"].pid,
            }
        else:
            running_processes.pop(gpu_name, None)

    return {
        "running": False,
        "status": "stopped",
        "service": None,
        "running_service": None,
        "gpu": gpu_name,
        "pid": None,
    }


@app.post("/start/{gpu_name}/{service_name}")
def start_service(gpu_name: str, service_name: str):
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")

    gpu_cfg = TARGETS[gpu_name]
    _sync_salad_state(gpu_name)

    # --- 1. Salad の起動リクエスト ---
    if service_name == "salad":
        if not gpu_cfg.get("salad_managed"):
            raise HTTPException(status_code=400, detail=f"Salad is not enabled on {gpu_name}")

        current = running_processes.get(gpu_name)
        if current is not None:
            if current["service"] == "salad":
                return {"status": "already_running", "gpu": gpu_name, "service": "salad", "pid": None}
            _stop_current_on_gpu(gpu_name)

        try:
            salad.start(auto_launch_app=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        running_processes[gpu_name] = {"service": "salad", "proc": None}
        return {"status": "started", "gpu": gpu_name, "service": "salad", "pid": None}

    # --- 2. 通常のバッチサービス (sd / ollama) の起動リクエスト ---
    if service_name not in gpu_cfg or not isinstance(gpu_cfg[service_name], dict):
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_name}' not configured for {gpu_name}",
        )

    current = running_processes.get(gpu_name)
    if current is not None:
        if current["service"] == service_name and current["proc"] is not None and current["proc"].poll() is None:
            return {
                "status": "already_running",
                "service": service_name,
                "pid": current["proc"].pid,
            }
        # 別サービス（または手動起動されたSalad）が動いていれば停止
        _stop_current_on_gpu(gpu_name)

    cfg = gpu_cfg[service_name]
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

    _sync_salad_state(gpu_name)
    current = running_processes.get(gpu_name)
    if current is not None:
        if current["service"] == service_name:
            _stop_current_on_gpu(gpu_name)
            return {"status": "stopped", "gpu": gpu_name, "service": service_name}
        return {
            "status": "not_running",
            "detail": f"Currently running service is '{current['service']}', not '{service_name}'",
        }

    return {"status": "not_running"}


@app.post("/stop/{gpu_name}")
def stop_gpu(gpu_name: str):
    """GPU指定のみで、現在動いているサービス（SD/Ollama/Salad）を何であれ停止・VRAM解放"""
    if gpu_name not in TARGETS:
        raise HTTPException(status_code=404, detail="Unknown GPU")

    stopped_service = _stop_current_on_gpu(gpu_name)
    if stopped_service is not None:
        return {"status": "stopped", "gpu": gpu_name, "service": stopped_service}

    return {"status": "not_running"}


# Salad専用エンドポイント (/salad/status, /salad/app/start, /salad/app/stop, /salad/watchdog) をマウント
app.include_router(salad_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)