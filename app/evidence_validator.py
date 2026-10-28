import re


FAILURE_TERMS = (
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "OOMKilled",
    "CreateContainerConfigError",
    "FailedMount",
)


def validate_analysis(analysis: str, evidence: str) -> dict:
    """
    Detect failure conditions mentioned by AI
    but absent from collected evidence.

    This is a limited consistency check, not a
    complete factual verification system.
    """

    unsupported = []

    for term in FAILURE_TERMS:
        mentioned = re.search(
            rf"\b{re.escape(term)}\b",
            analysis,
            re.IGNORECASE,
        )

        observed = re.search(
            rf"\b{re.escape(term)}\b",
            evidence,
            re.IGNORECASE,
        )

        if mentioned and not observed:
            unsupported.append(term)

    return {
        "passed": not unsupported,
        "unsupported_claims": unsupported,
    }
