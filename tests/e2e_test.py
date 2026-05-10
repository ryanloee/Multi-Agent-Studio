"""End-to-end test: create workflow → trigger run → verify events via WebSocket.

Usage:
    cd apps/orchestrator
    python ../../tests/e2e_test.py

Requires the backend NOT be running (this script starts it in-process).
"""

import asyncio
import json
import sys
import os
import time
import uuid

# Add project paths
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH_DIR = os.path.join(REPO_ROOT, "apps", "orchestrator")
AGENT_DIR = os.path.join(REPO_ROOT, "apps", "agent")

sys.path.insert(0, ORCH_DIR)
sys.path.insert(0, AGENT_DIR)

# Set env vars before importing app
os.environ.setdefault("MAS_DATABASE_URL", "sqlite+aiosqlite:///./test_e2e.db")
os.environ.setdefault("MAS_SANDBOX_ROOT", ".test_sandboxes")
os.environ.setdefault("MIMO_API_KEY", "tp-c9xrl6ymx1xg5o1uxkk8vcnac36b7w2gajozg770h8a71u5y")

import httpx
from pathlib import Path

BASE_URL = "http://127.0.0.1:18799"
TIMEOUT = 120  # seconds to wait for run completion

# Color helpers for terminal
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def log_ok(msg: str):
    print(f"  {GREEN}OK{RESET} {msg}")


def log_fail(msg: str):
    print(f"  {RED}FAIL{RESET} {msg}")


def log_info(msg: str):
    print(f"  {CYAN}>>{RESET} {msg}")


def log_warn(msg: str):
    print(f"  {YELLOW}!!{RESET} {msg}")


async def run_test():
    """Main test coroutine."""
    from app.main import app
    import uvicorn
    from threading import Thread

    # Start server in background thread
    print(f"\n{CYAN}[1/7]{RESET} Starting backend server on port 18799...")
    config = uvicorn.Config(app, host="127.0.0.1", port=18799, log_level="warning")
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    await asyncio.sleep(2)

    if not server.started:
        log_fail("Server failed to start")
        return False
    log_ok("Server started")

    client = httpx.AsyncClient(base_url=BASE_URL, timeout=30)

    try:
        # --- Health check ---
        print(f"\n{CYAN}[2/7]{RESET} Health check...")
        resp = await client.get("/health")
        assert resp.status_code == 200, f"Health check failed: {resp.status_code}"
        log_ok(f"Health: {resp.json()}")

        # --- List models ---
        print(f"\n{CYAN}[3/7]{RESET} List available models...")
        resp = await client.get("/api/models")
        assert resp.status_code == 200
        models = resp.json().get("models", [])
        log_ok(f"Models: {[m['full_id'] for m in models]}")
        mimo_models = [m for m in models if m["provider"] == "mimo"]
        assert len(mimo_models) > 0, "No mimo models found!"
        log_ok(f"MiMo model found: {mimo_models[0]['full_id']}")

        # --- Create workflow ---
        print(f"\n{CYAN}[4/7]{RESET} Create workflow with a Planner node...")
        resp = await client.post("/api/workflows", json={
            "name": "E2E Test - Planner",
            "description": "Auto-generated test workflow",
        })
        assert resp.status_code == 200, f"Create failed: {resp.text}"
        wf = resp.json()
        wf_id = wf["id"]
        log_ok(f"Workflow created: {wf_id}")

        # Update with a Planner node
        node_id = f"planner_{uuid.uuid4().hex[:8]}"
        resp = await client.put(f"/api/workflows/{wf_id}", json={
            "name": "E2E Test - Planner",
            "nodes": [{
                "id": node_id,
                "type": "plan",
                "position": {"x": 100, "y": 100},
                "data": {
                    "label": "Test Planner",
                    "agentType": "plan",
                    "modelProvider": "mimo",
                    "modelId": "mimo-v2.5",
                    "prompt": "Analyze the current directory and list the files. Create a plan with 2 subtasks: 1) Read README.md if it exists 2) List Python files in the project.",
                    "permissions": {},
                    "command": "",
                    "description": "",
                },
            }],
            "edges": [],
        })
        assert resp.status_code == 200, f"Update failed: {resp.text}"
        log_ok(f"Workflow updated with planner node: {node_id}")

        # --- Trigger run ---
        print(f"\n{CYAN}[5/7]{RESET} Trigger run...")
        resp = await client.post(f"/api/runs/{wf_id}/run")
        assert resp.status_code == 201, f"Run trigger failed: {resp.text}"
        run_data = resp.json()
        run_id = run_data["id"]
        log_ok(f"Run triggered: {run_id}")
        log_info(f"Run status: {run_data['status']}")

        # --- Collect events via WebSocket ---
        print(f"\n{CYAN}[6/7]{RESET} Collecting events via WebSocket (timeout {TIMEOUT}s)...")
        import websockets

        events_received = []
        event_types_seen = set()
        ws_url = f"ws://127.0.0.1:18799/ws/runs/{run_id}/stream"

        start_time = time.time()
        run_ended = False

        async with websockets.connect(ws_url) as ws:
            while time.time() - start_time < TIMEOUT:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    event_type = data.get("type", "")
                    events_received.append(data)
                    event_types_seen.add(event_type)

                    # Print event summary
                    node_id_short = data.get("node_id", "")[:20]
                    content_preview = str(data.get("content", ""))[:80].replace("\n", " ")
                    tool_name = data.get("tool_name", "")

                    if event_type == "llm_token":
                        # Don't spam, just count
                        pass
                    elif event_type in ("run_started", "run_completed", "run_failed"):
                        log_info(f"[{event_type}] {content_preview}")
                    elif event_type in ("node_started", "node_completed", "node_failed"):
                        log_info(f"[{event_type}] node={data.get('node_id', '')[:25]} {content_preview}")
                    elif event_type == "child_created":
                        log_info(f"[child_created] parent={data.get('node_id', '')[:20]} child={data.get('child_node_id', '')[:20]} type={data.get('child_type', '')}")
                    elif event_type == "status":
                        log_info(f"[status] node={node_id_short} → {content_preview}")
                    elif event_type == "tool_call":
                        log_info(f"[tool_call] tool={tool_name} node={node_id_short}")
                    elif event_type == "tool_result":
                        log_info(f"[tool_result] tool={tool_name} node={node_id_short} len={len(content_preview)}")
                    elif event_type == "error":
                        log_warn(f"[error] {content_preview}")
                    elif event_type == "ping":
                        pass  # heartbeat
                    else:
                        log_info(f"[{event_type}] {content_preview[:60]}")

                    if event_type in ("run_completed", "run_failed"):
                        run_ended = True
                        break

                except asyncio.TimeoutError:
                    # Check run status via REST
                    resp = await client.get(f"/api/runs/{run_id}")
                    if resp.status_code == 200:
                        status = resp.json().get("status", "")
                        if status in ("completed", "failed"):
                            log_info(f"Run ended with status: {status}")
                            run_ended = True
                            break
                    continue

        # --- Verify results ---
        print(f"\n{CYAN}[7/7]{RESET} Verifying results...")
        print(f"\n  Total events received: {len(events_received)}")
        print(f"  Event types seen: {sorted(event_types_seen)}")

        success = True

        # Check 1: Run started and ended
        if "run_started" in event_types_seen:
            log_ok("run_started event received")
        else:
            log_fail("Missing run_started event")
            success = False

        if run_ended:
            log_ok("Run reached terminal state")
        else:
            log_fail(f"Run did not complete within {TIMEOUT}s")
            success = False

        # Check 2: Node lifecycle
        if "node_started" in event_types_seen:
            log_ok("node_started event received")
        else:
            log_fail("Missing node_started event")
            success = False

        # Check 3: LLM tokens (thinking process)
        llm_events = [e for e in events_received if e.get("type") in ("llm_token", "llm_chunk")]
        total_llm_chars = sum(len(e.get("content", "")) for e in llm_events)
        if llm_events:
            log_ok(f"LLM output: {len(llm_events)} tokens, {total_llm_chars} chars total")
        else:
            log_fail("No LLM tokens received - thinking process not visible")
            success = False

        # Check 4: Tool calls
        tool_calls = [e for e in events_received if e.get("type") == "tool_call"]
        tool_results = [e for e in events_received if e.get("type") == "tool_result"]
        if tool_calls:
            log_ok(f"Tool calls: {len(tool_calls)} calls, {len(tool_results)} results")
            for tc in tool_calls:
                log_info(f"  Tool: {tc.get('tool_name', '?')}")
        else:
            log_warn("No tool calls received (may be model-dependent)")

        # Check 5: Child nodes from planner
        child_created = [e for e in events_received if e.get("type") == "child_created"]
        if child_created:
            log_ok(f"Child nodes created by planner: {len(child_created)}")
            for ch in child_created:
                log_info(f"  Child: {ch.get('child_node_id', '?')} type={ch.get('child_type', '?')}")
        else:
            log_warn("No child nodes created (planner may not have produced subtasks)")

        # Check 6: Node completion
        node_completed = [e for e in events_received if e.get("type") == "node_completed"]
        node_failed = [e for e in events_received if e.get("type") == "node_failed"]
        if node_completed:
            log_ok(f"Nodes completed: {len(node_completed)}")
        if node_failed:
            log_warn(f"Nodes failed: {len(node_failed)}")
            for nf in node_failed:
                log_warn(f"  Failed: {nf.get('node_id', '?')} {nf.get('content', '')}")

        # Check 7: Shell output
        shell_events = [e for e in events_received if e.get("type") == "shell_stdout"]
        if shell_events:
            log_ok(f"Shell output events: {len(shell_events)}")

        # Final verdict
        print(f"\n{'='*60}")
        if success:
            print(f"{GREEN}TEST PASSED{RESET} -- Full pipeline working!")
        else:
            print(f"{RED}TEST FAILED{RESET} -- Some checks did not pass")
        print(f"{'='*60}\n")

        return success

    except Exception as e:
        log_fail(f"Test error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await client.aclose()
        server.should_exit = True
        # Cleanup test artifacts
        for p in [Path("test_e2e.db"), Path("test_e2e.db-wal"), Path("test_e2e.db-shm")]:
            p.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(".test_sandboxes", ignore_errors=True)


if __name__ == "__main__":
    result = asyncio.run(run_test())
    sys.exit(0 if result else 1)
