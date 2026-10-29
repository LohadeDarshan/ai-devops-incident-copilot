
import re


FAILURE_TERMS = (
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "OOMKilled",
    "CreateContainerConfigError",
    "FailedMount",
)

UNSUPPORTED_PATTERNS = (
    r"\b(?:the\s+)?root cause is\b",
    r"\b(?:the\s+)?application is causing\b",
    r"\b(?:the\s+)?application caused\b",
    r"\b(?:the\s+)?pod is failing\b",
    r"\bCPU usage is high\b",
    r"\bmemory usage is high\b",
    r"\bdisk usage is high\b",
)


def _term_is_observed(term: str, evidence: str) -> bool:
    """Return True only when evidence mentions the failure positively."""

    escaped = re.escape(term)

    pattern = (
        rf"\b{escaped}\b"
    )

    for match in re.finditer(pattern, evidence, re.IGNORECASE):
        # Inspect the text immediately before the term.
        prefix = evidence[max(0, match.start() - 50):match.start()]

        # Ignore explicit negations such as:
        # "No CrashLoopBackOff observed"
        # "CrashLoopBackOff: false"
        if re.search(
            r"\b(?:no|not|never|without|absent|false|"
            r"not\s+observed|not\s+detected)\b"
            r"[^.!?\n]{0,40}$",
            prefix,
            re.IGNORECASE,
        ):
            continue

        # Ignore explicit negative wording immediately after the term.
        suffix = evidence[match.end():match.end() + 40]

        if re.match(
            r"\s*(?::\s*false\b|"
            r"(?:was\s+)?not\s+(?:observed|detected|found)\b)",
            suffix,
            re.IGNORECASE,
        ):
            continue

        return True

    return False


def validate_analysis(analysis: str, evidence: str) -> dict:
    """
    Perform limited text-pattern consistency checks.

    This is not a complete factual verification system.
    """

    unsupported = []

    for term in FAILURE_TERMS:
        mentioned = re.search(
            rf"\b{re.escape(term)}\b",
            analysis,
            re.IGNORECASE,
        )

        if not mentioned:
            continue

        # Do not flag explicit negations in the AI analysis.
        prefix = analysis[max(0, mentioned.start() - 50):mentioned.start()]

        is_negated = bool(
            re.search(
                r"\b(?:not|no|never|without|isn't|wasn't|"
                r"hasn't|is\s+not|was\s+not|has\s+not)\b"
                r"[^.!?\n]{0,40}$",
                prefix,
                re.IGNORECASE,
            )
        )

        if not _term_is_observed(term, evidence) and not is_negated:
            unsupported.append(
                f"Failure condition not found in evidence: {term}"
            )

    for pattern in UNSUPPORTED_PATTERNS:
        if re.search(pattern, analysis, re.IGNORECASE):
            unsupported.append(
                f"Potentially unsupported assertion: {pattern}"
            )

    return {
        "passed": not unsupported,
        "unsupported_claims": unsupported,
    }
