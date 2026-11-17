import re


def safe_name(value):
    value = str(value or "")
    if re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
        return value
    return None


def diagnostic_commands(labels):
    alertname = str(labels.get("alertname", ""))
    namespace = safe_name(labels.get("namespace"))
    pod = safe_name(labels.get("pod"))
    deployment = safe_name(labels.get("deployment"))

    commands = []

    if namespace:
        commands.append({
            "command": f"kubectl get pods -n {namespace} -o wide",
            "purpose": "Check pod status, readiness, and node placement."
        })
        commands.append({
            "command": (
                f"kubectl get events -n {namespace} "
                "--sort-by=.lastTimestamp"
            ),
            "purpose": "Check recent Kubernetes warning events."
        })

    if namespace and pod:
        commands.append({
            "command": f"kubectl describe pod {pod} -n {namespace}",
            "purpose": "Inspect container states, image pulls, and pod events."
        })
        commands.append({
            "command": f"kubectl logs {pod} -n {namespace} --tail=100",
            "purpose": "Read application logs if the container started."
        })

    if namespace and deployment:
        commands.append({
            "command": (
                f"kubectl describe deployment {deployment} "
                f"-n {namespace}"
            ),
            "purpose": "Inspect deployment conditions and replica availability."
        })
        commands.append({
            "command": (
                f"kubectl rollout status deployment/{deployment} "
                f"-n {namespace} --timeout=10s"
            ),
            "purpose": "Check whether the deployment rollout is progressing."
        })

    if alertname.startswith("Kubernetes") and not namespace:
        commands.append({
            "command": "kubectl get pods -A -o wide",
            "purpose": "Identify unhealthy pods across namespaces."
        })

    if not commands:
        commands.append({
            "command": "kubectl get nodes -o wide",
            "purpose": "Check cluster node readiness."
        })

    return commands
