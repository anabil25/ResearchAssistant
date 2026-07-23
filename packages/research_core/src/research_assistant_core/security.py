from __future__ import annotations

import re
from dataclasses import dataclass

_INDIRECT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore (all |any )?(previous|prior|system) instructions?\b", re.I),
    re.compile(r"\breveal (the )?(system|developer) prompt\b", re.I),
    re.compile(r"\b(call|invoke|execute|run) (the )?(tool|shell|command)\b", re.I),
    re.compile(r"\bdisable (safety|guardrails?|approval)\b", re.I),
    re.compile(r"\bexfiltrat(e|ion)\b", re.I),
)


@dataclass(frozen=True, slots=True)
class ContentSafetyFinding:
    code: str
    message: str
    excerpt: str


class UnsafeSourceContentError(ValueError):
    def __init__(self, findings: list[ContentSafetyFinding]) -> None:
        self.findings = findings
        super().__init__("Untrusted source content contains instruction-like text")


def scan_untrusted_content(content: str) -> list[ContentSafetyFinding]:
    findings: list[ContentSafetyFinding] = []
    for pattern in _INDIRECT_INJECTION_PATTERNS:
        match = pattern.search(content)
        if match:
            start = max(0, match.start() - 40)
            end = min(len(content), match.end() + 80)
            findings.append(
                ContentSafetyFinding(
                    code="indirect_prompt_injection",
                    message="Source text attempts to influence agent instructions or tools.",
                    excerpt=content[start:end],
                )
            )
    return findings


def enforce_safe_source(content: str) -> None:
    findings = scan_untrusted_content(content)
    if findings:
        raise UnsafeSourceContentError(findings)
