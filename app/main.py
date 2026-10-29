import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import requests
from app.diagnostics import diagnostic_commands
from app.evidence_validator import validate_analysis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

app = FastAPI(
    title="AI DevOps Incident Copilot",
    version="1.1.0",
)



# Dashboard static files
STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


PROM_URL = os.getenv(
    "PROM_URL",
    "http://127.0.0.1:9090",
).rstrip("/")

OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://127.0.0.1:11434",
).rstrip("/")

MODEL = os.getenv("MODEL", "qwen2.5:0.5b")

INCIDENT_FILE = Path(
    os.getenv("INCIDENT_FILE", "/data/incidents.jsonl")
)

http = requests.Session()
llm_lock = threading.Lock()

SYSTEM_PROMPT = """
You are an SRE incident investigation assistant.

Analyze ONLY the supplied alert and collected evidence.
The supplied evidence is the source of truth.

STRICT EVIDENCE RULES:
- Never invent Kubernetes errors, events, metrics, logs, or observations.
- Never assume a pod or application failed unless evidence supports it.
- Treat alert names and descriptions as signals, not proof of root cause.
- Never label CPU, memory, disk, or load as high without a supplied
  threshold, alert condition, or explicit evidence establishing it.
- Node-level metrics describe the node, not an individual pod or app.
- Do not claim the Copilot application caused a resource issue unless
  evidence directly links the issue to that application.
- Never claim ImagePullBackOff, CrashLoopBackOff, OOMKilled, or another
  specific failure unless the supplied evidence supports that condition.
- Distinguish confirmed observations from hypotheses.
- Label possible explanations as hypotheses, not confirmed causes.
- If evidence is insufficient, state exactly:
  "Root cause not confirmed."
- If no Kubernetes-specific evidence is available, explicitly say so.
- Do not claim an incident has been fixed or recovered without evidence.
- Do not execute commands or suggest destructive remediation.
- Recommend read-only investigation commands only.
- Use only object names supplied by the alert or evidence.
- Do not invent pod, namespace, deployment, node, or container names.
- Do not interpret missing metrics as proof that a resource is healthy
  or unhealthy.

Return EXACTLY these sections:

1. INCIDENT SUMMARY
Summarize the alert using the supplied information.

2. CONFIRMED OBSERVATIONS
List only facts explicitly supported by the supplied evidence.
Mention when relevant evidence is missing.

3. ROOT CAUSE ASSESSMENT
State the root cause only when supported by evidence.
Otherwise state: Root cause not confirmed.
Separate hypotheses from confirmed findings.

4. DIAGNOSTIC APPROACH
Recommend relevant read-only checks using actual known object names.
Do not invent command targets.

5. RECOMMENDED ACTIONS
Provide safe next steps based on the evidence.
Do not claim an action has been performed.

6. CONFIDENCE AND EVIDENCE GAPS
State High, Medium, or Low confidence and explain why.
Identify missing evidence needed to confirm the root cause.

Keep the response concise and evidence-based.
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
        return "Prometheus query was unsuccessful."

    results = payload.get("data", {}).get("result", [])

    if not results:
        return "No metric data available."

    lines = []

    for item in results[:8]:
        labels = item.get("metric", {})
        value = item.get("value", [None, "unknown"])[1]

        lines.append(
            f"labels={json.dumps(labels)}, value={value}"
        )

    return "\n".join(lines)


def promql_label(value: str) -> str:
    return json.dumps(value)


def collect_kubernetes_evidence(labels: dict) -> str:
    namespace = labels.get("namespace", "")
    pod = labels.get("pod", "")
    deployment = labels.get("deployment", "")
    node = labels.get("node", "")

    queries = {}

    if namespace and pod:
        selector = (
            f'namespace={promql_label(namespace)},'
            f'pod={promql_label(pod)}'
        )

        queries["Pod active phase"] = (
            f"kube_pod_status_phase{{{selector}}} == 1"
        )

        queries["Pod readiness (1=ready, 0=not ready)"] = (
            f'kube_pod_status_ready'
            f'{{{selector},condition="true"}}'
        )

        queries["Container waiting reason"] = (
            f"kube_pod_container_status_waiting_reason"
            f"{{{selector}}} == 1"
        )

        queries["Previous container termination reason"] = (
            f"kube_pod_container_status_last_terminated_reason"
            f"{{{selector}}} == 1"
        )

        queries["Container restart count"] = (
            f"kube_pod_container_status_restarts_total"
            f"{{{selector}}}"
        )

        queries["Container image"] = (
            f"kube_pod_container_info{{{selector}}}"
        )

    if namespace and deployment:
        selector = (
            f'namespace={promql_label(namespace)},'
            f'deployment={promql_label(deployment)}'
        )

        queries["Deployment desired replicas"] = (
            f"kube_deployment_spec_replicas{{{selector}}}"
        )

        queries["Deployment available replicas"] = (
            f"kube_deployment_status_replicas_available"
            f"{{{selector}}}"
        )

        queries["Deployment unavailable replicas"] = (
            f"kube_deployment_status_replicas_unavailable"
            f"{{{selector}}}"
        )

        # Find ReplicaSets owned by the affected deployment.
        rs_selector = (
            f'namespace={promql_label(namespace)},'
            f'owner_name={promql_label(deployment)},'
            f'owner_kind="Deployment"'
        )

        queries["Deployment ReplicaSets"] = (
            f"kube_replicaset_owner{{{rs_selector}}}"
        )

    if node:
        queries["Kubernetes node readiness"] = (
            f'kube_node_status_condition'
            f'{{node={promql_label(node)},'
            f'condition="Ready",status="true"}}'
        )

    if not queries:
        return "No Kubernetes-specific labels supplied."

    evidence = []

    for title, query in queries.items():
        try:
            result = query_prometheus(query)
        except Exception as exc:
            result = (
                f"Metric query failed: {type(exc).__name__}"
            )

        evidence.append(f"{title}:\n{result}")

    return "\n\n".join(evidence)[:12000]


def collect_linux_evidence() -> str:
    queries = {
        "CPU usage percent by server":
            '100 * (1 - avg by(instance)'
            '(rate(node_cpu_seconds_total'
            '{mode="idle"}[2m])))',

        "Memory usage percent by server":
            '100 * (1 - node_memory_MemAvailable_bytes'
            ' / node_memory_MemTotal_bytes)',

        "Root filesystem usage percent":
            '100 * (1 - node_filesystem_avail_bytes'
            '{mountpoint="/"}'
            ' / node_filesystem_size_bytes'
            '{mountpoint="/"})',

        "Node exporter health (1=up, 0=down)":
            'up{job="node"}',

        "System load by server":
            'node_load1',
    }

    evidence = []

    for title, query in queries.items():
        try:
            result = query_prometheus(query)
        except Exception as exc:
            result = (
                f"Metric query failed: {type(exc).__name__}"
            )

        evidence.append(f"{title}:\n{result}")

    return "\n\n".join(evidence)[:8000]


def collect_evidence(labels: dict) -> str:
    kubernetes = collect_kubernetes_evidence(labels)
    linux = collect_linux_evidence()

    return (
        "KUBERNETES EVIDENCE:\n"
        + kubernetes
        + "\n\nLINUX INFRASTRUCTURE EVIDENCE:\n"
        + linux
    )


def ask_llm(prompt: str) -> str:
    response = http.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
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
    INCIDENT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with INCIDENT_FILE.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(json.dumps(record) + "\n")


def investigate(alert: dict) -> None:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    evidence = collect_evidence(labels)

    prompt = (
        f"Alert labels:\n{json.dumps(labels)}\n\n"
        f"Alert annotations:\n"
        f"{json.dumps(annotations)}\n\n"
        f"Alert status: "
        f"{alert.get('status', 'unknown')}\n"
        f"Started at: "
        f"{alert.get('startsAt', 'unknown')}\n\n"
        f"Evidence:\n{evidence}"
    )

    try:
        # Only one CPU-based Ollama inference at a time.
        with llm_lock:
            rca = ask_llm(prompt)

        validation = validate_analysis(rca, evidence)

        if not validation["passed"]:
            rca += (
                "\n\nAI VALIDATION WARNING: "
                "Potentially unsupported claims were detected: "
                + "; ".join(validation["unsupported_claims"])
                + ". Treat these claims as unverified."
            )

        status = "completed"

    except Exception as exc:
        rca = (
            f"AI analysis failed: "
            f"{type(exc).__name__}. "
            "Review collected evidence and service logs."
        )
        status = "failed"

    record = {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": status,
        "alert": labels,
        "annotations": annotations,
        "evidence": evidence,
        "root_cause_analysis": rca,
    }

    try:
        save_incident(record)
    except Exception as exc:
        print(
            f"Failed to save incident: {exc}",
            flush=True,
        )

    print(json.dumps(record), flush=True)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
    }


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

        # Resolved alerts will be supported in a later step.
        if alert.get("status") == "resolved":
            continue

        background_tasks.add_task(
            investigate,
            alert,
        )

        queued += 1

    return {
        "status": "accepted",
        "alerts_queued": queued,
    }


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


@app.get("/diagnostics")
def diagnostics(n: int = 10):
    records = incidents(n=n)
    return [
        {
            "timestamp": record.get("timestamp"),
            "alert": record.get("alert", {}),
            "commands": diagnostic_commands(
                record.get("alert", {})
            ),
        }
        for record in records
    ]
