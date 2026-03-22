"""
Integrity X - Main Entry Point
Real-Time Integrity & Drift Evaluation Engine

Architecture:
- Local system metrics (CPU, memory, disk, network) are ALWAYS collected at 1Hz
- Remote endpoints are ADDITIVE — their data merges with local metrics
- Endpoint status (ok/error/timeout) is tracked per-poll and sent to the frontend
- Stop properly cancels the monitoring asyncio task
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import Set, Optional, Dict, List
import json

from engine.baseline_engine import BaselineEngine
from engine.anomaly_detection_engine import AnomalyDetectionEngine
from engine.cause_inference_engine import CauseInferenceEngine
from engine.integrity_score_engine import IntegrityScoreEngine, IntegrityFactors
from engine.local_system_monitor import LocalSystemMonitor
from engine.baseline_persistence import save_baseline, load_baseline, list_saved_baselines, delete_baseline
from engine.intelligence_engine import IntelligenceEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Integrity X API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Models ───────────────────────────────────────────────────────────────────

class EndpointConfig(BaseModel):
    url: str
    name: str = ""
    mapping: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None


# ─── Endpoint Registry ────────────────────────────────────────────────────────

class EndpointRegistry:
    """Manages remote server endpoints polled for metrics"""

    def __init__(self):
        self.endpoints: List[Dict] = []

    def add(self, url: str, name: str = "", mapping: Dict = None, headers: Dict = None) -> int:
        idx = len(self.endpoints)
        self.endpoints.append({
            "id": idx,
            "url": url,
            "name": name or url,
            "mapping": mapping or {},
            "headers": headers or {},
            "active": True,
            "last_poll": None,
            "last_status": None,   # "ok" | "error" | "timeout"
            "last_error": None,
            "error_count": 0,
            "success_count": 0,
        })
        return idx

    def remove(self, idx: int):
        if 0 <= idx < len(self.endpoints):
            self.endpoints.pop(idx)
            for i, ep in enumerate(self.endpoints):
                ep["id"] = i

    def toggle(self, idx: int):
        if 0 <= idx < len(self.endpoints):
            self.endpoints[idx]["active"] = not self.endpoints[idx]["active"]

    def to_json(self):
        return [
            {
                "id": ep["id"],
                "name": ep["name"],
                "url": ep["url"],
                "active": ep["active"],
                "last_poll": ep["last_poll"].isoformat() if ep["last_poll"] else None,
                "last_status": ep["last_status"],
                "last_error": ep["last_error"],
                "errors": ep["error_count"],
                "successes": ep["success_count"],
                "mapping": ep["mapping"],
            }
            for ep in self.endpoints
        ]


# ─── System State ─────────────────────────────────────────────────────────────

class SystemState:
    def __init__(self):
        self.baseline_engine = BaselineEngine(window_size=100, min_samples=30)
        self.anomaly_engine = AnomalyDetectionEngine()
        self.cause_engine = CauseInferenceEngine()
        self.integrity_engine = IntegrityScoreEngine()
        self.local_monitor = LocalSystemMonitor()
        self.intelligence_engine = IntelligenceEngine(window_size=60)
        self.endpoint_registry = EndpointRegistry()

        self.ws_clients: Set[WebSocket] = set()

        self.running = False
        self.phase = "idle"   # idle | learning | monitoring
        self.learning_samples = 0
        self.learning_target = 30

        self._monitor_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None

        self.latest_metrics: Dict = {}
        self.latest_integrity = {"score": 100, "severity": "STABLE", "change": 0}
        self.latest_drift = {"drifting": False, "severity": 0, "anomaly_count": 0}
        self.latest_cause = None
        self.latest_analysis: Dict = {}
        self.events: List[Dict] = []
        
        # Server crash detection state
        self.system_state = "STABLE"  # STABLE | WARNING | CRITICAL
        self.crashed_servers: Set[str] = set()
        self.server_recovery_times: Dict[str, datetime] = {}


state = SystemState()


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

async def poll_endpoint(session: aiohttp.ClientSession, ep: Dict) -> Dict[str, float]:
    """
    Poll one remote endpoint with multi-layer crash detection
    Layer 1: Network Connectivity
    Layer 2: HTTP Status Codes  
    Layer 3: Health Status Validation
    """
    server_id = ep["name"] or ep["url"]
    
    try:
        # Layer 1: Network Connectivity Detection
        async with session.get(
            ep["url"],
            headers=ep["headers"],
            timeout=aiohttp.ClientTimeout(total=2)  # Reduced to 2 seconds for faster detection
        ) as resp:
            
            # Layer 2: HTTP Status Code Detection - require multiple 500 errors
            if resp.status >= 500:
                ep["error_count"] += 1
                ep["last_error"] = f"Server error HTTP {resp.status}"
                
                if ep["error_count"] >= 3:
                    ep["last_status"] = "crashed"
                    await trigger_crash_alert(ep, f"HTTP {resp.status} server error (after {ep['error_count']} attempts)")
                else:
                    logger.warning(f"[HTTP ERROR] Server {server_id} returned {resp.status} (attempt {ep['error_count']}/3)")
                return {}
            
            if resp.status != 200:
                ep["error_count"] += 1
                ep["last_error"] = f"HTTP {resp.status}"
                
                # Don't crash on 4xx errors, just log them
                if ep["error_count"] >= 5:
                    logger.warning(f"[HTTP WARNING] Server {server_id} consistently returning {resp.status}")
                return {}

            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except:
                    ep["last_status"] = "error"
                    ep["last_error"] = "Invalid JSON response"
                    ep["error_count"] += 1
                    return {}

            # Layer 3: Health Status Validation
            if isinstance(data, dict):
                status = str(data.get("status", "")).lower()
                health = str(data.get("health", "")).lower()
                
                # Check for explicit crash indicators - require multiple detections
                crash_indicators = ["down", "unhealthy", "crashed", "failed", "offline"]
                if any(indicator in status for indicator in crash_indicators) or \
                   any(indicator in health for indicator in crash_indicators):
                    ep["error_count"] += 1
                    ep["last_error"] = f"Server reports status: {status or health}"
                    
                    if ep["error_count"] >= 2:  # Lower threshold for explicit crash indicators
                        ep["last_status"] = "crashed"
                        await trigger_crash_alert(ep, f"Server explicitly reports unhealthy status: {status or health}")
                    else:
                        logger.warning(f"[HEALTH WARNING] Server {server_id} reports unhealthy status (attempt {ep['error_count']}/2): {status or health}")
                    return {}

            # Success - reset error count and update status
            ep["last_poll"] = datetime.now()
            ep["last_status"] = "ok"
            ep["last_error"] = None
            ep["success_count"] += 1
            ep["error_count"] = 0
            
            # Check if this server was previously crashed and is now recovered
            if server_id in state.crashed_servers:
                logger.info(f"[RECOVERY CHECK] Server {server_id} was crashed, now responding - triggering recovery")
                await handle_server_recovery(ep)
            else:
                logger.debug(f"[POLL SUCCESS] Server {server_id} responding normally (was not crashed)")

            if ep["mapping"]:
                result = {}
                for internal, path in ep["mapping"].items():
                    val = _nested_get(data, path)
                    if val is not None:
                        try:
                            result[internal] = float(val)
                        except (TypeError, ValueError):
                            pass
                return result
            else:
                extracted = _extract_numeric(data)
                
                # Additional validation for impossible metric values
                if _detect_impossible_metrics(extracted):
                    ep["error_count"] += 1
                    ep["last_error"] = "Impossible metric values detected"
                    
                    # Only trigger crash after multiple impossible metric detections
                    if ep["error_count"] >= 3:
                        ep["last_status"] = "crashed"
                        await trigger_crash_alert(ep, "Impossible metric values detected (possible crash)")
                        return {}
                    else:
                        logger.warning(f"[IMPOSSIBLE METRICS] Server {server_id} returned impossible metrics (attempt {ep['error_count']}/3)")
                        return {}
                
                return extracted

    except asyncio.TimeoutError:
        # Layer 1: Timeout Detection - require multiple timeouts before crash
        ep["error_count"] += 1
        ep["last_error"] = "Request timeout (server unresponsive)"
        
        if ep["error_count"] >= 3:
            ep["last_status"] = "crashed"
            await trigger_crash_alert(ep, f"Server timeout - unresponsive for {ep['error_count']} consecutive attempts")
        else:
            logger.warning(f"[TIMEOUT] Server {server_id} timeout (attempt {ep['error_count']}/3)")
        return {}
        
    except (aiohttp.ClientConnectorError, ConnectionRefusedError, OSError) as e:
        # Layer 1: Connection Refused / Network Error - require multiple failures
        ep["error_count"] += 1
        ep["last_error"] = f"Connection failed: {str(e)[:100]}"
        
        if ep["error_count"] >= 3:
            ep["last_status"] = "crashed"
            await trigger_crash_alert(ep, f"Network connectivity failure: {type(e).__name__} (after {ep['error_count']} attempts)")
        else:
            logger.warning(f"[CONNECTION ERROR] Server {server_id} connection failed (attempt {ep['error_count']}/3): {type(e).__name__}")
        return {}
        
    except aiohttp.ClientError as e:
        # Layer 1: DNS Error / Unreachable Host - require multiple failures
        ep["error_count"] += 1
        ep["last_error"] = f"Client error: {str(e)[:100]}"
        
        if ep["error_count"] >= 3:
            ep["last_status"] = "crashed"
            await trigger_crash_alert(ep, f"DNS or host unreachable: {str(e)} (after {ep['error_count']} attempts)")
        else:
            logger.warning(f"[CLIENT ERROR] Server {server_id} client error (attempt {ep['error_count']}/3): {str(e)}")
        return {}
        
    except Exception as e:
        # Catch-all for other network issues - require multiple failures
        ep["error_count"] += 1
        ep["last_error"] = str(e)[:120]
        
        if ep["error_count"] >= 5:  # Higher threshold for unknown errors
            ep["last_status"] = "crashed"
            await trigger_crash_alert(ep, f"Repeated unknown errors: {str(e)} (after {ep['error_count']} attempts)")
        else:
            logger.warning(f"[UNKNOWN ERROR] Server {server_id} error (attempt {ep['error_count']}/5): {e}")
        return {}


def _nested_get(data, path: str):
    parts = path.split(".")
    val = data
    for p in parts:
        if isinstance(val, dict) and p in val:
            val = val[p]
        else:
            return None
    return val


def _extract_numeric(data, prefix="") -> Dict[str, float]:
    result = {}
    if not isinstance(data, dict):
        return result
    for k, v in data.items():
        key = f"{prefix}_{k}" if prefix else k
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            result[key] = float(v)
        elif isinstance(v, dict):
            result.update(_extract_numeric(v, key))
    return result


def _detect_impossible_metrics(metrics: Dict[str, float]) -> bool:
    """
    Enhanced impossible metrics detection for crash identification
    Detects patterns that indicate server malfunction or crash
    """
    if not metrics:
        return False
    
    # Pattern 1: Zero requests but high latency (impossible combination)
    request_rate = metrics.get("request_rate", metrics.get("requests_per_second", metrics.get("rps", 0)))
    latency = metrics.get("latency", metrics.get("response_time", metrics.get("avg_response_time", 0)))
    
    if request_rate == 0 and latency > 1000:
        logger.warning("Impossible metrics: Zero requests with high latency")
        return True
    
    # Pattern 2: All critical metrics exactly zero (suspicious)
    critical_metrics = ["cpu_usage", "memory_usage", "request_rate", "requests_per_second"]
    zero_count = sum(1 for key in critical_metrics if metrics.get(key, -1) == 0)
    if zero_count >= 3 and len(metrics) > 3:
        logger.warning("Impossible metrics: All critical metrics are zero")
        return True
    
    # Pattern 3: Negative values where impossible
    impossible_negative = ["cpu", "memory", "requests", "latency", "response_time", "uptime"]
    for key, value in metrics.items():
        if any(neg_key in key.lower() for neg_key in impossible_negative) and value < 0:
            logger.warning(f"Impossible metrics: Negative value for {key}: {value}")
            return True
    
    # Pattern 4: Extremely high values that indicate malfunction
    if latency > 60000:  # 60+ second response time
        logger.warning(f"Impossible metrics: Extreme latency: {latency}ms")
        return True
    
    # Pattern 5: CPU or memory usage over 100%
    cpu_usage = metrics.get("cpu_usage", metrics.get("cpu", 0))
    memory_usage = metrics.get("memory_usage", metrics.get("memory", 0))
    
    if cpu_usage > 100 or memory_usage > 100:
        logger.warning(f"Impossible metrics: Usage over 100% - CPU: {cpu_usage}%, Memory: {memory_usage}%")
        return True
    
    return False


async def trigger_crash_alert(ep: Dict, reason: str):
    """
    Central crash alert handler - triggers immediate CRITICAL alert
    This function implements the core crash detection response
    """
    server_id = ep["name"] or ep["url"]
    
    # Only trigger crash event once per server to avoid spam
    if server_id not in state.crashed_servers:
        state.crashed_servers.add(server_id)
        
        # Set system to CRITICAL state immediately
        state.system_state = "CRITICAL"
        
        # Force integrity score to zero
        state.latest_integrity = {
            "score": 0,
            "severity": "CRITICAL",
            "change": -100
        }
        
        # Create structured crash event
        crash_event = {
            "timestamp": datetime.now().isoformat(),
            "type": "SERVER_CRASH",
            "severity": "CRITICAL",
            "server_id": server_id,
            "message": f"Monitored server unreachable or crashed",
            "reason": reason,
            "url": ep["url"],
        }
        
        # Add to event history
        state.events.append(crash_event)
        if len(state.events) > 100:
            state.events.pop(0)
        
        # Log the critical event
        logger.error(f"[CRITICAL ALERT] {server_id}: {reason}")
        
        # Broadcast immediate alert to all UI clients
        await broadcast({
            "type": "CRITICAL_ALERT",
            "server": server_id,
            "message": f"⚠ CRITICAL ALERT — SERVER DOWN: {server_id}",
            "reason": reason,
            "integrity_score": 0,
            "system_state": "CRITICAL",
            "event": crash_event,
            "timestamp": datetime.now().isoformat()
        })


async def handle_server_recovery(ep: Dict):
    """
    Handle server recovery detection and system restoration
    """
    server_id = ep["name"] or ep["url"]
    
    logger.info(f"[RECOVERY HANDLER] Processing recovery for server: {server_id}")
    logger.info(f"[RECOVERY HANDLER] Current crashed servers: {list(state.crashed_servers)}")
    
    if server_id in state.crashed_servers:
        # Remove from crashed servers
        state.crashed_servers.discard(server_id)
        state.server_recovery_times[server_id] = datetime.now()
        
        logger.info(f"[RECOVERY DETECTED] Server {server_id} is back online")
        logger.info(f"[RECOVERY DETECTED] Remaining crashed servers: {list(state.crashed_servers)}")
        
        # Create recovery event
        recovery_event = {
            "timestamp": datetime.now().isoformat(),
            "type": "SERVER_RECOVERY",
            "severity": "INFO",
            "server_id": server_id,
            "message": f"Server restored — monitoring resumed",
            "url": ep["url"],
        }
        
        state.events.append(recovery_event)
        if len(state.events) > 100:
            state.events.pop(0)
        
        # Check if all servers have recovered
        if not state.crashed_servers:
            # All servers recovered - restore system state
            state.system_state = "STABLE"
            logger.info("[SYSTEM RECOVERY] All servers restored - system state back to STABLE")
            
            await broadcast({
                "type": "SYSTEM_RECOVERY",
                "system_state": "STABLE",
                "message": "All servers restored — monitoring resumed",
                "recovered_server": server_id,
                "event": recovery_event
            })
        else:
            # Partial recovery - still have crashed servers
            logger.info(f"[PARTIAL RECOVERY] Server {server_id} recovered, but {len(state.crashed_servers)} servers still crashed")
            await broadcast({
                "type": "SERVER_RECOVERY",
                "server_id": server_id,
                "recovered_server": server_id,
                "remaining_crashed": list(state.crashed_servers),
                "event": recovery_event
            })
    else:
        logger.warning(f"[RECOVERY HANDLER] Server {server_id} not found in crashed servers list: {list(state.crashed_servers)}")


# ─── Core Monitoring Loop ─────────────────────────────────────────────────────

async def monitoring_loop():
    """
    Main 1Hz loop:
    1. Always collect local system metrics (CPU, memory, disk, network)
    2. Poll any configured remote endpoints and MERGE their data
    3. Run the analysis pipeline
    4. Broadcast results to all WebSocket clients
    """
    logger.info("Monitoring loop started")
    state._http_session = aiohttp.ClientSession()

    try:
        while state.running:
            tick_start = asyncio.get_event_loop().time()

            # 1. Local system metrics (always present)
            metrics: Dict[str, float] = state.local_monitor.get_metrics()

            # 2. Remote endpoints (additive — merged on top of local)
            active_eps = [ep for ep in state.endpoint_registry.endpoints if ep["active"]]
            if active_eps:
                tasks = [poll_endpoint(state._http_session, ep) for ep in active_eps]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, dict):
                        metrics.update(r)

            state.latest_metrics = metrics

            # 3. Analysis pipeline — wrapped so any crash logs but doesn't kill the loop
            try:
                await run_pipeline(metrics)
            except Exception as pipeline_err:
                logger.error(f"Pipeline error (continuing): {pipeline_err}", exc_info=True)

            # 4. Maintain 1Hz cadence
            elapsed = asyncio.get_event_loop().time() - tick_start
            await asyncio.sleep(max(0, 1.0 - elapsed))

    except asyncio.CancelledError:
        logger.info("Monitoring loop cancelled")
    except Exception as e:
        logger.error(f"Monitoring loop error: {e}", exc_info=True)
    finally:
        if state._http_session:
            await state._http_session.close()
            state._http_session = None
        logger.info("Monitoring loop stopped")


async def run_pipeline(metrics: Dict[str, float]):
    """Run the full analysis pipeline on a metrics snapshot"""

    if state.phase == "learning":
        state.baseline_engine.add_sample(metrics)
        state.learning_samples += 1

        progress = min(state.learning_samples / state.learning_target, 1.0)

        if state.baseline_engine.is_ready():
            state.baseline_engine.finalize()
            state.phase = "monitoring"
            logger.info("Baseline complete — entering monitoring phase")

            # ── Save baseline to disk for future runs ──────────────────────────
            endpoint_urls = [ep["url"] for ep in state.endpoint_registry.endpoints if ep["active"]]
            fingerprint = save_baseline(state.baseline_engine, endpoint_urls)

            await broadcast({
                "type": "phase_change",
                "phase": "monitoring",
                "baseline_saved": fingerprint is not None,
                "baseline_key": fingerprint,
                "endpoint_count": active_endpoint_count(),
                "endpoints": state.endpoint_registry.to_json(),
            })
        else:
            await broadcast({
                "type": "learning",
                "progress": progress,
                "samples": state.learning_samples,
                "target": state.learning_target,
                "metrics": metrics,
                "endpoint_count": active_endpoint_count(),
                "endpoints": state.endpoint_registry.to_json(),
            })
        return

    if state.phase == "monitoring":
        state.baseline_engine.update_rolling(metrics)
        baseline_stats = state.baseline_engine.get_stats()
        z_scores = state.baseline_engine.get_z_scores(metrics)

        drift_result = state.anomaly_engine.detect(metrics, baseline_stats, z_scores)
        state.latest_drift = drift_result

        cause = None
        if drift_result["drifting"]:
            cause = state.cause_engine.infer(metrics, baseline_stats, drift_result, z_scores)
            state.latest_cause = cause

            event = {
                "timestamp": datetime.now().isoformat(),
                "type": "drift_detected",
                "drift_type": drift_result["drift_type"],
                "severity": drift_result["severity"],
                "cause": cause["primary_cause"],
                "confidence": cause["confidence"],
                "affected_metrics": drift_result["affected_metrics"],
            }
            state.events.append(event)
            if len(state.events) > 100:
                state.events.pop(0)

        factors = IntegrityFactors(
            drift_severity=drift_result["severity"],
            anomaly_count=drift_result["anomaly_count"],
            baseline_confidence=1.0 if state.baseline_engine.is_ready() else 0.5,
            correlation_stability=state.baseline_engine.get_correlation_score(metrics),
            variance_stability=state.baseline_engine.get_variance_stability(metrics),
            response_stability=1.0,
        )

        integrity_result = state.integrity_engine.calculate(factors)
        
        # Override integrity score if system is in CRITICAL state due to server crash
        if state.system_state == "CRITICAL":
            integrity_result = {
                "score": 0,
                "severity": "CRITICAL",
                "change": integrity_result.get("change", -100)
            }
        
        state.latest_integrity = integrity_result

        # Ingest into IIE every tick
        state.intelligence_engine.ingest(
            metrics=metrics,
            z_scores=z_scores,
            drift_result=drift_result,
            integrity_result=integrity_result,
            events=state.events,
            baseline_stats=baseline_stats,
        )

        # Run IIE analysis every 5 seconds and broadcast
        if state.intelligence_engine.should_analyze(5.0):
            analysis = state.intelligence_engine.analyze(
                metrics=metrics,
                z_scores=z_scores,
                drift_result=drift_result,
                integrity_result=integrity_result,
                events=state.events,
                phase=state.phase,
            )
            state.latest_analysis = analysis
            await broadcast({"type": "intelligence", **analysis})

        await broadcast({
            "type": "data",
            "timestamp": datetime.now().isoformat(),
            "phase": state.phase,
            "metrics": metrics,
            "z_scores": z_scores,
            "drift": drift_result,
            "cause": cause,
            "integrity": integrity_result,
            "system_state": state.system_state,
            "crashed_servers": list(state.crashed_servers),
            "baseline_stats": {
                "samples": baseline_stats["samples"],
                "metrics_count": len(baseline_stats["metrics"]),
            },
            "endpoint_count": active_endpoint_count(),
            "endpoints": state.endpoint_registry.to_json(),
        })


def active_endpoint_count():
    return len([e for e in state.endpoint_registry.endpoints if e["active"]])


# ─── Broadcast ────────────────────────────────────────────────────────────────

async def broadcast(data: dict):
    if not state.ws_clients:
        logger.debug(f"Broadcast {data.get('type', '?')}: no clients")
        return
    dead = set()
    for client in state.ws_clients:
        try:
            await client.send_json(data)
        except Exception as e:
            logger.warning(f"Broadcast send failed: {e}")
            dead.add(client)
    state.ws_clients -= dead
    logger.info(f"Broadcast {data.get('type', '?')} to {len(state.ws_clients)} client(s)")


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Integrity X ready on http://localhost:8000")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"system": "Integrity X", "version": "2.0.0", "status": "operational"}


@app.get("/status")
async def get_status():
    return {
        "running": state.running,
        "phase": state.phase,
        "learning_samples": state.learning_samples,
        "learning_target": state.learning_target,
        "baseline_ready": state.baseline_engine.is_ready(),
        "integrity_score": state.latest_integrity["score"],
        "severity": state.latest_integrity["severity"],
        "system_state": state.system_state,
        "crashed_servers": list(state.crashed_servers),
        "ws_clients": len(state.ws_clients),
        "endpoints": state.endpoint_registry.to_json(),
    }


# ─── Control ──────────────────────────────────────────────────────────────────

@app.post("/control/start")
async def start_system():
    if state.running:
        return {"status": "already_running"}

    logger.info("Starting monitoring")
    state.running = True
    state.learning_samples = 0

    state.baseline_engine = BaselineEngine(window_size=100, min_samples=30)
    state.anomaly_engine.reset()
    state.integrity_engine.reset()

    if state._monitor_task and not state._monitor_task.done():
        state._monitor_task.cancel()
        try:
            await state._monitor_task
        except asyncio.CancelledError:
            pass

    # ── Try to restore a saved baseline (skip learning if found) ──────────────
    endpoint_urls = [ep["url"] for ep in state.endpoint_registry.endpoints if ep["active"]]
    fingerprint = load_baseline(state.baseline_engine, endpoint_urls)

    if fingerprint:
        # Baseline restored — jump straight to monitoring
        state.phase = "monitoring"
        logger.info(f"Baseline restored from disk ({fingerprint}) — skipping learning phase")
        await broadcast({
            "type": "baseline_loaded",
            "key": fingerprint,
            "endpoint_count": active_endpoint_count(),
            "endpoints": state.endpoint_registry.to_json(),
        })
    else:
        # No saved baseline — start learning from scratch
        state.phase = "learning"
        await broadcast({
            "type": "system_started",
            "endpoint_count": active_endpoint_count(),
            "endpoints": state.endpoint_registry.to_json(),
        })

    state._monitor_task = asyncio.create_task(monitoring_loop())
    return {"status": "started", "phase": state.phase, "endpoint_count": active_endpoint_count()}


@app.post("/control/stop")
async def stop_system():
    if not state.running:
        return {"status": "not_running"}

    logger.info("Stopping monitoring")
    state.running = False
    state.phase = "idle"

    if state._monitor_task and not state._monitor_task.done():
        state._monitor_task.cancel()
        try:
            await state._monitor_task
        except asyncio.CancelledError:
            pass
    state._monitor_task = None

    await broadcast({"type": "system_stopped"})
    return {"status": "stopped"}


# ─── Endpoint Management ──────────────────────────────────────────────────────

@app.get("/config/endpoints")
async def get_endpoints():
    return {"endpoints": state.endpoint_registry.to_json()}


@app.post("/config/endpoints")
async def add_endpoint(config: EndpointConfig):
    idx = state.endpoint_registry.add(
        url=config.url,
        name=config.name or config.url,
        mapping=config.mapping,
        headers=config.headers or {},
    )
    logger.info(f"Endpoint added [{idx}]: {config.url}")
    return {"status": "added", "id": idx, "url": config.url}


@app.delete("/config/endpoints/{endpoint_id}")
async def remove_endpoint(endpoint_id: int):
    eps = state.endpoint_registry.endpoints
    if endpoint_id < 0 or endpoint_id >= len(eps):
        return {"status": "not_found"}
    url = eps[endpoint_id]["url"]
    state.endpoint_registry.remove(endpoint_id)
    logger.info(f"Endpoint removed: {url}")
    return {"status": "removed", "url": url}


@app.patch("/config/endpoints/{endpoint_id}/toggle")
async def toggle_endpoint(endpoint_id: int):
    eps = state.endpoint_registry.endpoints
    if endpoint_id < 0 or endpoint_id >= len(eps):
        return {"status": "not_found"}
    state.endpoint_registry.toggle(endpoint_id)
    return {"status": "toggled", "active": eps[endpoint_id]["active"]}


# ─── Intelligence ─────────────────────────────────────────────────────────────

@app.get("/intelligence/analysis")
async def get_intelligence():
    """Returns the latest IIE cognitive analysis snapshot"""
    if state.latest_analysis:
        return state.latest_analysis
    # If not yet analyzed, run a quick one
    if state.phase == "monitoring" and state.latest_metrics:
        analysis = state.intelligence_engine.analyze(
            metrics=state.latest_metrics,
            z_scores=state.baseline_engine.get_z_scores(state.latest_metrics),
            drift_result=state.latest_drift,
            integrity_result=state.latest_integrity,
            events=state.events,
            phase=state.phase,
        )
        state.latest_analysis = analysis
        return analysis
    return state.intelligence_engine._idle_analysis(state.phase)


# ─── Events ───────────────────────────────────────────────────────────────────

@app.get("/events/recent")
async def get_recent_events():
    return {"events": state.events[-20:], "total": len(state.events)}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    state.ws_clients.add(websocket)
    client_id = id(websocket)
    logger.info(f"WebSocket connected (ID: {client_id})")

    try:
        # Send full current state on connect — enough to reconstruct any phase
        await websocket.send_json({
            "type": "init",
            "phase": state.phase,
            "running": state.running,
            "integrity": state.latest_integrity,
            "system_state": state.system_state,
            "crashed_servers": list(state.crashed_servers),
            "endpoints": state.endpoint_registry.to_json(),
            "learning_samples": state.learning_samples,
            "learning_target": state.learning_target,
            "learning_progress": state.learning_samples / state.learning_target if state.learning_target else 0,
            "baseline_saved": state.phase == "monitoring" and state.baseline_engine.is_ready(),
            "last_analysis": state.latest_analysis or None,
        })

        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                cmd = json.loads(message)
                action = cmd.get("action")
                if action == "start":
                    await start_system()
                elif action == "stop":
                    await stop_system()
            except asyncio.TimeoutError:
                await asyncio.sleep(0.05)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected (ID: {client_id})")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        state.ws_clients.discard(websocket)


# ─── Baseline Management ──────────────────────────────────────────────────────

@app.get("/baselines")
async def get_baselines():
    """List all saved baselines on disk"""
    return {"baselines": list_saved_baselines()}


@app.delete("/baselines/{fingerprint}")
async def delete_saved_baseline(fingerprint: str):
    """Delete a saved baseline so the system re-learns next time"""
    ok = delete_baseline(fingerprint)
    return {"deleted": ok, "fingerprint": fingerprint}


@app.delete("/baselines")
async def clear_all_baselines():
    """Delete ALL saved baselines"""
    baselines = list_saved_baselines()
    count = 0
    for b in baselines:
        if delete_baseline(b["fingerprint"]):
            count += 1
    return {"deleted": count}


# ─── Entry Point ──────────────────────────────────────────────────────────────

# ─── Simulation Endpoints (for testing) ──────────────────────────────────────

@app.post("/simulate/crash")
async def simulate_crash():
    """Simulate a server crash for testing purposes"""
    if not state.endpoint_registry.endpoints:
        return {"status": "no_endpoints", "message": "No endpoints configured to crash"}
    
    # Simulate crash on first active endpoint
    for ep in state.endpoint_registry.endpoints:
        if ep["active"]:
            await _handle_server_crash(ep, "Simulated crash for testing")
            return {
                "status": "crash_simulated",
                "server": ep["name"] or ep["url"],
                "message": "Server crash simulation triggered"
            }
    
    return {"status": "no_active_endpoints"}


async def _check_server_recovery():
    """Check all endpoints for recovery and trigger recovery events"""
    for ep in state.endpoint_registry.endpoints:
        server_id = ep["name"] or ep["url"]
        if server_id in state.crashed_servers and ep["last_status"] == "ok":
            await handle_server_recovery(ep)


@app.post("/simulate/recovery")
async def simulate_recovery():
    """Simulate server recovery for testing purposes"""
    if not state.crashed_servers:
        return {"status": "no_crashed_servers", "message": "No servers are currently crashed"}
    
    # Simulate recovery for all crashed servers
    recovered = []
    for ep in state.endpoint_registry.endpoints:
        server_id = ep["name"] or ep["url"]
        if server_id in state.crashed_servers:
            ep["last_status"] = "ok"
            ep["error_count"] = 0
            recovered.append(server_id)
    
    await _check_server_recovery()
    
    return {
        "status": "recovery_simulated",
        "recovered_servers": recovered,
        "message": f"Recovery simulation triggered for {len(recovered)} servers"
    }


@app.post("/force/recovery")
async def force_recovery():
    """Force system recovery - clears all crash states"""
    logger.info("[FORCE RECOVERY] Manual recovery triggered")
    
    # Get list of crashed servers before clearing
    crashed_list = list(state.crashed_servers)
    
    # Clear all crash states
    state.crashed_servers.clear()
    state.system_state = "STABLE"
    
    # Restore integrity score if it was zeroed
    if state.latest_integrity["score"] == 0:
        state.latest_integrity = {
            "score": 100,
            "severity": "STABLE", 
            "change": 100
        }
    
    # Reset all endpoint statuses
    for ep in state.endpoint_registry.endpoints:
        ep["last_status"] = "ok"
        ep["last_error"] = None
        ep["error_count"] = 0
    
    # Create recovery event
    recovery_event = {
        "timestamp": datetime.now().isoformat(),
        "type": "FORCE_RECOVERY",
        "severity": "INFO",
        "message": "Manual system recovery performed",
        "recovered_servers": crashed_list,
    }
    
    state.events.append(recovery_event)
    
    # Broadcast system recovery
    await broadcast({
        "type": "SYSTEM_RECOVERY",
        "system_state": "STABLE",
        "message": "Manual recovery performed - all systems restored",
        "recovered_servers": crashed_list,
        "event": recovery_event
    })
    
    return {
        "status": "recovery_forced",
        "recovered_servers": crashed_list,
        "message": f"Force recovery completed for {len(crashed_list)} servers"
    }


@app.post("/system/check-recovery")
async def check_recovery():
    """Manually check all servers for recovery and trigger recovery if they're responding"""
    logger.info("[MANUAL RECOVERY CHECK] Checking all servers for recovery")
    
    recovered_servers = []
    still_crashed = []
    
    # Check each crashed server
    for server_id in list(state.crashed_servers):
        # Find the endpoint
        ep = None
        for endpoint in state.endpoint_registry.endpoints:
            if (endpoint["name"] or endpoint["url"]) == server_id:
                ep = endpoint
                break
        
        if not ep:
            logger.warning(f"[RECOVERY CHECK] Could not find endpoint for crashed server: {server_id}")
            continue
        
        try:
            # Try to poll the server
            async with aiohttp.ClientSession() as session:
                result = await poll_endpoint(session, ep)
                
                if result:  # If we got metrics back, server is recovered
                    logger.info(f"[RECOVERY CHECK] Server {server_id} is responding - triggering recovery")
                    await handle_server_recovery(ep)
                    recovered_servers.append(server_id)
                else:
                    still_crashed.append(server_id)
                    
        except Exception as e:
            logger.warning(f"[RECOVERY CHECK] Server {server_id} still not responding: {e}")
            still_crashed.append(server_id)
    
    return {
        "status": "recovery_check_complete",
        "recovered_servers": recovered_servers,
        "still_crashed": still_crashed,
        "message": f"Recovery check complete: {len(recovered_servers)} recovered, {len(still_crashed)} still down"
    }


@app.post("/system/reset")
async def reset_system():
    """Complete system reset - clears all states and restarts fresh"""
    logger.info("[SYSTEM RESET] Complete system reset triggered")
    
    # Stop monitoring if running
    if state.running:
        await stop_system()
    
    # Clear all states
    state.crashed_servers.clear()
    state.system_state = "STABLE"
    state.latest_integrity = {"score": 100, "severity": "STABLE", "change": 0}
    state.latest_drift = {"drifting": False, "severity": 0, "anomaly_count": 0}
    state.latest_cause = None
    state.latest_analysis = {}
    state.events.clear()
    state.server_recovery_times.clear()
    
    # Reset all endpoint statuses
    for ep in state.endpoint_registry.endpoints:
        ep["last_status"] = "ok"
        ep["last_error"] = None
        ep["error_count"] = 0
        ep["success_count"] = 0
    
    # Reset engines
    state.baseline_engine = BaselineEngine()
    state.anomaly_engine = AnomalyDetectionEngine()
    state.cause_engine = CauseInferenceEngine()
    state.integrity_engine = IntegrityScoreEngine()
    state.intelligence_engine = IntelligenceEngine()
    
    # Broadcast complete reset
    await broadcast({
        "type": "SYSTEM_RESET",
        "system_state": "STABLE",
        "message": "Complete system reset performed",
        "integrity": state.latest_integrity
    })
    
    return {
        "status": "system_reset",
        "message": "Complete system reset performed - all states cleared"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
