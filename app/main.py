import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

app = FastAPI(title="AI DevOps Incident Copilot", version="1.0.0")

PROM_URL = os.getenv("PROM_URL", "http://127.0.0.1:9090").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.getenv("MODEL", "qwen2.5:1.5b")
INCIDENT_FILE = Path(
    os.getenv("INCIDENT_FILE", "/data/incidents.jsonl")
)

http = requests.Session()
llm_lock = threading.Lock()

SYSTEM_PROMPT = """
You are a cautious Site Reliability Engineer.

Analyze the supplied Prometheus alert and evidence.
Use only the evidence supplied. Never invent metrics or logs.
Separate observed facts from hypotheses.
Do not claim that you fixed the incident.
Never execute commands.

Return:
1. Incident summary
2. Evidence
3. Probable root cause and confidence
4. Suggested remediation for a human to review
5. Verification steps

Keep the response under 250 words.
"""


def query_prometheus(query: str) -> str:
    response = http.get(
        f"{PROM_URL}/api/v1/query",
        params={"query": query},
        timeout=15,
    )
    response.raise_for_status()

    payload = response.json()

    if payload.get("status") != "success":
        return "Prometheus returned an unsuccessful query."

    results = payload.get("data", {}).get("result", [])

    if not results:
        return "No metric data available."

    lines = []

    for item in results[:8]:
        labels = item.get("metric", {})
        value = item.get("value", [None, "unknown"])[1]
        lines.append(f"labels={labels}, value={value}")

    return "\n".join(lines)


def collect_evidence() -> str:
    queries = {
        "CPU usage percent":
            '100 * (1 - avg(rate(node_cpu_seconds_total'
            '{mode="idle"}[2m])))',

        "Memory usage percent":
            '100 * (1 - node_memory_MemAvailable_bytes'
            ' / node_memory_MemTotal_bytes)',

        "Filesystem usage percent":
            '100 * (1 - node_filesystem_avail_bytes'
            '{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
            ' / node_filesystem_size_bytes'
            '{fstype!~"tmpfs|overlay|squashfs|ramfs"})',

        "Node exporter health":
            'up{job="node"}',

        "System load":
            'node_load1',
    }

    evidence = []

    for title, query in queries.items():
        try:
            result = query_prometheus(query)
        except Exception as exc:
            result = f"Metric query failed: {type(exc).__name__}"

        evidence.append(f"{title}:\n{result}")

    return "\n\n".join(evidence)[:10000]


def ask_llm(prompt: str) -> str:
    response = http.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096,
            },
        },
        timeout=300,
    )

    response.raise_for_status()
    return response.json()["message"]["content"]


def save_incident(record: dict) -> None:
    INCIDENT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with INCIDENT_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")


def investigate(alert: dict) -> None:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    evidence = collect_evidence()

    prompt = (
        f"Alert labels:\n{json.dumps(labels)}\n\n"
        f"Alert annotations:\n{json.dumps(annotations)}\n\n"
        f"Alert status: {alert.get('status', 'unknown')}\n"
        f"Started at: {alert.get('startsAt', 'unknown')}\n\n"
        f"Evidence:\n{evidence}"
    )

    try:
        # Serialize inference for a small CPU-based model.
        with llm_lock:
            rca = ask_llm(prompt)

        status = "completed"

    except Exception as exc:
        rca = (
            f"AI analysis failed: {type(exc).__name__}. "
            "Review the service logs and collected evidence."
        )
        status = "failed"

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "alert": labels,
        "annotations": annotations,
        "evidence": evidence,
        "root_cause_analysis": rca,
    }

    try:
        save_incident(record)
    except Exception as exc:
        print(f"Failed to save incident: {exc}", flush=True)

    print(json.dumps(record), flush=True)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Expected a JSON alert payload",
        )

    alerts = payload.get("alerts", [])

    if not isinstance(alerts, list):
        raise HTTPException(
            status_code=400,
            detail="alerts must be a list",
        )

    queued = 0

    for alert in alerts[:10]:
        if not isinstance(alert, dict):
            continue

        # Ignore resolved alerts in this initial version.
        if alert.get("status") == "resolved":
            continue

        background_tasks.add_task(investigate, alert)
        queued += 1

    return {"status": "accepted", "alerts_queued": queued}


@app.get("/incidents")
def incidents(n: int = 10):
    if n < 1 or n > 100:
        raise HTTPException(
            status_code=400,
            detail="n must be between 1 and 100",
        )

    try:
        lines = INCIDENT_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
    except FileNotFoundError:
        return []

    records = []

    for line in lines[-n:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return records