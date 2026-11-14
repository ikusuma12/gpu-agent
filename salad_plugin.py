import asyncio
import glob
import os
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field

import grpc
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel


# =========================================================
# 1. ノード設定とプリセット
# =========================================================
@dataclass
class NodeConfig:
    node_name: str = field(default_factory=socket.gethostname)
    target_gpu_index: int = 0
    isolated_gpu_indices: list[int] = field(default_factory=list)
    system_base_watt: float = 45.0
    electricity_cost_jpy: float = 31.0
    usd_jpy_rate: float = 155.0
    profit_margin: float = 1.05


PRESETS = {
    "north": NodeConfig(
        node_name="north",
        target_gpu_index=0,
        isolated_gpu_indices=[1, 2],  # GPU 0 (RTX 5080) のみ使用し、他GPUを隔離
        system_base_watt=60.0,
    ),
    "torrent": NodeConfig(
        node_name="torrent",
        target_gpu_index=0,
        isolated_gpu_indices=[1],     # GPU 0 (4070 Ti SUPER) のみ使用し、GPU 1 (5060 Ti) を隔離
        system_base_watt=100.0,
    ),
    "terra": NodeConfig(
        node_name="terra",
        target_gpu_index=0,
        isolated_gpu_indices=[],
        system_base_watt=35.0,
    ),
}


# =========================================================
# 2. Salad プラグイン本体
# =========================================================
class SaladPlugin:
    GRPC_SERVICE = "/salad.grpc.salad_bowl_widget.v1alpha.SaladBowlWidgetService"

    def __init__(self, config: NodeConfig | None = None):
        if config is None:
            host = socket.gethostname().lower()
            config = PRESETS.get(host, NodeConfig(node_name=host))
        self.cfg = config
        self.deficit_streak = 0
        self.auto_stop_enabled = False  # 赤字検知時の自動一時停止（デフォルトOFF）

    def _run_cmd(self, cmd: list[str], ignore_error: bool = False, env: dict | None = None) -> str:
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, check=not ignore_error, env=env
            )
            return res.stdout.strip()
        except subprocess.CalledProcessError:
            return ""

    # ---------------------------------------------------------
    # A. agent.py 排他制御向けの抽象化インターフェース
    # ---------------------------------------------------------
    def start(self, auto_launch_app: bool = True) -> dict:
        """
        Chopping（ジョブ稼働）を開始する。
        auto_launch_app=True の場合、Saladサービス自体が落ちていれば先に起動する。
        """
        if not self.is_app_running():
            if not auto_launch_app:
                raise RuntimeError("Salad.Bowl.Service.exe が起動していません")
            self.start_app(reset_wsl=False)

        return self.set_chopping(True)

    def pause(self) -> dict:
        """Chopping（ジョブ稼働）を軽量一時停止する（サービスは生かしたまま）"""
        if not self.is_app_running():
            return {"action": "StopActiveWorkloads", "enabled": False, "detail": "app_not_running"}
        return self.set_chopping(False)

    def is_active(self) -> bool:
        """現在 Chopping フラグが ON（稼働中）かどうかを返す"""
        if not self.is_app_running():
            return False
        grpc_state = self._fetch_grpc_state()
        if grpc_state is not None:
            return grpc_state["is_active"]
        # gRPC一時取得失敗時のフォールバック判定
        return self._get_gpu_metrics(self.cfg.target_gpu_index)["util_pct"] >= 15

    # ---------------------------------------------------------
    # B. アプリ・サービスの起動と完全停止
    # ---------------------------------------------------------
    def is_app_running(self) -> bool:
        out = self._run_cmd(["tasklist", "/FI", "IMAGENAME eq Salad.Bowl.Service.exe"], ignore_error=True)
        return "Salad.Bowl.Service.exe" in out

    def start_app(self, reset_wsl: bool = False) -> dict:
        """SaladBowlサービスとUIを起動する（マルチGPU隔離設定を含む）"""
        self.stop_app(shutdown_wsl=reset_wsl)

        if self.cfg.isolated_gpu_indices:
            target_str = str(self.cfg.target_gpu_index)
            for key in ["CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"]:
                self._run_cmd([
                    "wsl", "-u", "root", "sh", "-c",
                    f"grep -q '{key}' /etc/environment || echo '{key}={target_str}' >> /etc/environment"
                ], ignore_error=True)

        self._run_cmd(["net", "start", "SaladBowl"], ignore_error=True)
        time.sleep(5)

        proc_env = os.environ.copy()
        if self.cfg.isolated_gpu_indices:
            proc_env["CUDA_VISIBLE_DEVICES"] = str(self.cfg.target_gpu_index)
            proc_env["NVIDIA_VISIBLE_DEVICES"] = str(self.cfg.target_gpu_index)

        local_app = os.environ.get("LOCALAPPDATA", "")
        ui_launched = False
        for path in [
            os.path.join(local_app, r"Programs\Salad\Salad.exe"),
            r"C:\Program Files\Salad\Salad.exe",
        ]:
            if os.path.exists(path):
                subprocess.Popen([path], env=proc_env, close_fds=True)
                ui_launched = True
                break

        self.deficit_streak = 0
        return {
            "action": "start_app",
            "node": self.cfg.node_name,
            "ui_launched": ui_launched,
            "reset_wsl": reset_wsl,
        }

    def stop_app(self, shutdown_wsl: bool = False) -> dict:
        """SaladBowlサービスとUIを完全停止する（自動再起動を防止）"""
        for proc in ["Salad.exe", "Salad.Bootstrapper.exe"]:
            self._run_cmd(["taskkill", "/F", "/IM", proc], ignore_error=True)

        self._run_cmd(["sc", "failure", "SaladBowl", "reset=", "0", "actions=", "//"], ignore_error=True)
        self._run_cmd(["net", "stop", "SaladBowl"], ignore_error=True)
        time.sleep(2)
        self._run_cmd(["taskkill", "/F", "/T", "/IM", "Salad.Bowl.Service.exe"], ignore_error=True)

        if shutdown_wsl:
            self._run_cmd(["wsl", "--shutdown"], ignore_error=True)

        self.deficit_streak = 0
        return {
            "action": "stop_app",
            "node": self.cfg.node_name,
            "shutdown_wsl": shutdown_wsl,
        }

    # ---------------------------------------------------------
    # C. ネイティブ gRPC 通信 & Protobuf スナップショット解析
    # ---------------------------------------------------------
    def _get_dynamic_grpc_port(self) -> int:
        log_dir = os.path.join(os.environ.get("APPDATA", ""), "Salad", "logs")
        log_files = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)
        pattern = re.compile(r"Found Salad Bowl port numbers (\d+) \(gRPC\)")

        for log_file in log_files[:3]:
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in reversed(f.readlines()[-500:]):
                        m = pattern.search(line)
                        if m:
                            return int(m.group(1))
            except Exception:
                pass
        raise RuntimeError("SaladのログからgRPCポート番号が見つかりません")

    def set_chopping(self, enable: bool) -> dict:
        if not self.is_app_running():
            raise RuntimeError("Salad.Bowl.Service.exe が起動していません")

        port = self._get_dynamic_grpc_port()
        method = "StartActiveWorkloads" if enable else "StopActiveWorkloads"
        method_path = f"{self.GRPC_SERVICE}/{method}"
        target = f"127.0.0.1:{port}"

        with grpc.insecure_channel(target) as channel:
            rpc = channel.unary_unary(
                method_path,
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
            rpc(b"", timeout=5)

        if not enable:
            self.deficit_streak = 0

        return {
            "action": method,
            "enabled": enable,
            "grpc_target": target,
            "node": self.cfg.node_name,
        }

    def _parse_monitor_snapshot(self, raw_bytes: bytes) -> dict:
        def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
            val, shift = 0, 0
            while pos < len(buf):
                b = buf[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            return val, pos

        def parse_fields(buf: bytes) -> dict[int, list]:
            fields: dict[int, list] = {}
            pos = 0
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                fnum, wtype = tag >> 3, tag & 0x07
                if wtype == 0:
                    val, pos = read_varint(buf, pos)
                    fields.setdefault(fnum, []).append(val)
                elif wtype == 1:
                    val = struct.unpack("<d", buf[pos:pos+8])[0]
                    pos += 8
                    fields.setdefault(fnum, []).append(val)
                elif wtype == 2:
                    length, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos+length]
                    pos += length
                    fields.setdefault(fnum, []).append(chunk)
                elif wtype == 5:
                    val = struct.unpack("<f", buf[pos:pos+4])[0]
                    pos += 4
                    fields.setdefault(fnum, []).append(val)
                else:
                    break
            return fields

        root = parse_fields(raw_bytes)
        snap_bytes = root.get(100, [b""])[0]
        snap = parse_fields(snap_bytes)

        workload_flag = snap.get(11, [1])[0]
        is_active = (workload_flag == 2)

        balance_usd = 0.0
        last_24h_usd = 0.0
        est_hourly_usd = None
        if 5 in snap:
            earn = parse_fields(snap[5][0])
            balance_usd = round(earn.get(1, [0.0])[0], 4)
            last_24h_usd = round(earn.get(2, [0.0])[0], 4)
            raw_rate = earn.get(3, [-1.0])[0]
            if raw_rate >= 0.0:
                est_hourly_usd = round(raw_rate, 4)

        return {
            "is_active": is_active,
            "workload_flag": workload_flag,
            "balance_usd": balance_usd,
            "last_24h_usd": last_24h_usd,
            "est_hourly_usd": est_hourly_usd,
        }

    def _fetch_grpc_state(self) -> dict | None:
        try:
            port = self._get_dynamic_grpc_port()
            target = f"127.0.0.1:{port}"
            method_path = f"{self.GRPC_SERVICE}/MonitorState"
            with grpc.insecure_channel(target) as channel:
                rpc = channel.unary_stream(
                    method_path,
                    request_serializer=lambda x: x,
                    response_deserializer=lambda x: x,
                )
                stream = rpc(b"", timeout=3)
                first_msg = next(stream)
                return self._parse_monitor_snapshot(first_msg)
        except Exception:
            return None

    # ---------------------------------------------------------
    # D. 詳細メトリクス・損益分岐点・ウォッチドッグ
    # ---------------------------------------------------------
    def _get_gpu_metrics(self, gpu_idx: int) -> dict:
        out = self._run_cmd([
            "nvidia-smi", f"-i={gpu_idx}",
            "--query-gpu=power.draw,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits"
        ])
        if not out:
            return {"power_w": 0.0, "temp_c": 0, "util_pct": 0}
        p = [x.strip() for x in out.split(",")]
        return {"power_w": float(p[0]), "temp_c": int(p[1]), "util_pct": int(p[2])}

    def get_status(self) -> dict:
        is_running = self.is_app_running()
        m_target = self._get_gpu_metrics(self.cfg.target_gpu_index)

        extra_watt = 0.0
        isolated_metrics = {}
        for iso_idx in self.cfg.isolated_gpu_indices:
            m_iso = self._get_gpu_metrics(iso_idx)
            isolated_metrics[f"gpu_{iso_idx}"] = m_iso
            extra_watt += max(0.0, m_iso["power_w"] - 15.0)

        total_watt = m_target["power_w"] + self.cfg.system_base_watt + extra_watt
        breakeven_usd_h = ((total_watt / 1000.0) * self.cfg.electricity_cost_jpy) / self.cfg.usd_jpy_rate
        required_usd_h = breakeven_usd_h * self.cfg.profit_margin

        grpc_state = self._fetch_grpc_state() if is_running else None

        if not is_running:
            state = "STOPPED"
            is_active = False
        elif grpc_state is not None:
            is_active = grpc_state["is_active"]
            if not is_active:
                state = "PAUSED"
            elif m_target["util_pct"] >= 15:
                state = "CHOPPING"
            else:
                state = "PREPPING_OR_FINDING_JOB"
        else:
            is_active = m_target["util_pct"] >= 15
            state = "CHOPPING" if is_active else "UNKNOWN"

        current_rate = grpc_state["est_hourly_usd"] if grpc_state else None
        is_profitable = None
        net_profit_usd_h = None
        if current_rate is not None:
            net_profit_usd_h = round(current_rate - breakeven_usd_h, 4)
            is_profitable = current_rate >= required_usd_h

        return {
            "node": self.cfg.node_name,
            "state": state,
            "process_running": is_running,
            "chopping_flag_active": is_active,
            "auto_stop_enabled": self.auto_stop_enabled,
            "deficit_streak": self.deficit_streak,
            "target_gpu": m_target,
            "isolated_gpus": isolated_metrics,
            "power": {
                "total_estimated_w": round(total_watt, 1),
                "breakeven_usd_h": round(breakeven_usd_h, 4),
                "required_usd_h": round(required_usd_h, 4),
            },
            "earnings": {
                "balance_usd": grpc_state["balance_usd"] if grpc_state else None,
                "last_24h_usd": grpc_state["last_24h_usd"] if grpc_state else None,
                "current_rate_usd_h": current_rate,
                "net_profit_usd_h": net_profit_usd_h,
                "is_profitable": is_profitable,
            },
        }

    async def profit_watchdog_loop(self):
        """60秒ごとに収支を監視し、auto_stop_enabled=True の場合のみ連続赤字で軽量停止"""
        while True:
            await asyncio.sleep(60)
            status = self.get_status()
            if status["state"] != "CHOPPING":
                self.deficit_streak = 0
                continue

            is_prof = status["earnings"]["is_profitable"]
            if is_prof is False:
                self.deficit_streak += 1
                if self.auto_stop_enabled and self.deficit_streak >= 3:
                    try:
                        self.pause()
                    except Exception:
                        self.stop_app(shutdown_wsl=bool(self.cfg.isolated_gpu_indices))
            elif is_prof is True:
                self.deficit_streak = 0


# =========================================================
# 3. シングルトンインスタンス & FastAPI Router
# =========================================================
salad = SaladPlugin()
salad_router = APIRouter(prefix="/salad", tags=["Salad"])


class AutoStopConfig(BaseModel):
    enabled: bool


@salad_router.get("/status")
def api_salad_status():
    """現在の稼働状態・フラグ・GPU負荷・残高・レート・損益分岐点を取得"""
    return salad.get_status()


@salad_router.post("/app/start")
def api_salad_app_start(reset_wsl: bool = Query(default=False, description="起動前にWSL2をリセットするか")):
    """Saladアプリおよびバックエンドサービス本体を起動"""
    return salad.start_app(reset_wsl=reset_wsl)


@salad_router.post("/app/stop")
def api_salad_app_stop(shutdown_wsl: bool = Query(default=False, description="停止時にWSL2も落とすか")):
    """Saladアプリおよびバックエンドサービス本体を完全停止"""
    return salad.stop_app(shutdown_wsl=shutdown_wsl)


@salad_router.post("/watchdog")
def api_salad_watchdog(body: AutoStopConfig):
    """赤字検知時の自動一時停止の ON/OFF を切り替え"""
    salad.auto_stop_enabled = body.enabled
    salad.deficit_streak = 0
    return {"auto_stop_enabled": salad.auto_stop_enabled}