from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
import pandas as pd
from pydantic import BaseModel


@dataclass
class _AnalysisState:
    """
    Mutable accumulator for a single run_analysis() call.
    Created fresh each call — never stored on the engine instance.
    """
    flags: set = field(default_factory=set)
    alerts: List["Alert"] = field(default_factory=list)
    insights: List["Alert"] = field(default_factory=list)
    diagnosis: List["Alert"] = field(default_factory=list)
    sorted_pulls: List[pd.DataFrame] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)
    charge_air_peak_f: Optional[float] = None


class AlertSeverity(str, Enum):
    """
    Severity tiers that drive the scoring model.

    CRITICAL — instant zero (knock, dangerous lean)
    MAJOR    — large score deduction (HPFP crash, cascading fuel failure)
    MINOR    — small deduction (timing pull, throttle closure)
    INFO     — no deduction (performance note, metric reading)

    Using str as base class so Pydantic serializes these as plain strings,
    which Lambda JSON output and DynamoDB can store without a custom encoder.
    """
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


# Score deductions by severity. None = instant zero regardless of other alerts.
SEVERITY_DEDUCTIONS: dict = {
    AlertSeverity.CRITICAL: None,
    AlertSeverity.MAJOR: 25,
    AlertSeverity.MINOR: 10,
    AlertSeverity.INFO: 0,
}


class Alert(BaseModel):
    """A single diagnostic finding."""
    flag: str
    severity: AlertSeverity
    message: str
    # Plain-English explanation for non-technical users; None = omit from beginner view
    beginner_message: Optional[str] = None


class PullSummary(BaseModel):
    """Per-pull metrics computed by the multi-pull comparison pass."""
    pull_number: int
    mean_timing_correction: Optional[float] = None
    max_boost_psi: Optional[float] = None
    iat_end_f: Optional[float] = None


class DiagnosticReport(BaseModel):
    """Complete output of a B58DiagnosticEngine.run_analysis() call."""
    score: int
    status: str
    alerts: List[Alert]
    performance_insights: List[Alert]
    diagnosis: List["Alert"]
    pull_count: int
    pull_comparison: Optional[List[PullSummary]] = None
