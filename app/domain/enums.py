"""
app/domain/enums.py

All enumerations for the pipeline.  Using IntEnum / str-based Enum
so values serialise naturally to JSON and are sortable where needed.
"""
from __future__ import annotations

from enum import Enum, IntEnum


class DocType(str, Enum):
    """Document type assigned by the router (Step 2)."""
    DISCHARGE_SUMMARY = "DISCHARGE_SUMMARY"
    LAB_REPORT        = "LAB_REPORT"
    ENDOSCOPY         = "ENDOSCOPY"
    ELASTOGRAPHY      = "ELASTOGRAPHY"    # umbrella for fibroscan + shear_wave
    IMAGING           = "IMAGING"
    MICROBIOLOGY      = "MICROBIOLOGY"
    VIROLOGY          = "VIROLOGY"
    HISTOLOGY         = "HISTOLOGY"
    AMBULANSLAP       = "AMBULANSLAP"     # recognised but has no routed fields
    UNKNOWN           = "UNKNOWN"


class FieldSource(IntEnum):
    """
    Origin of an extracted value.

    Stored as IntEnum so priority comparison is just  >  :
        FieldSource.HARD > FieldSource.SOFT  →  True
    """
    SOFT       = 1   # LLM-assisted narrative extraction
    HEURISTIC  = 2   # rule-based / block-parser (events, dates)
    HARD       = 3   # fully deterministic (regex, table parser)


class Timepoint(str, Enum):
    """
    Registry timepoint suffixes.
    Maps to the 'timepoint' column in Registry_base.xlsx.
    """
    ANAM   = "anam"    # anamnesis (no suffix in column name)
    ADM    = "adm"     # admission  (_adm)
    INHOSP = "inhosp"  # in-hospital course  (_inhosp)
    DIS    = "dis"     # discharge  (_dis)


class JobStatus(str, Enum):
    """RQ job lifecycle states, stored in Redis."""
    QUEUED     = "QUEUED"
    INGESTING  = "INGESTING"
    ROUTING    = "ROUTING"
    ANCHORING  = "ANCHORING"
    EXTRACTING = "EXTRACTING"
    MERGING    = "MERGING"
    EXPORTING  = "EXPORTING"
    DONE       = "DONE"
    FAILED     = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Return True if this status is a final, non-recoverable state (DONE or FAILED)."""
        return self in (JobStatus.DONE, JobStatus.FAILED)

    @property
    def progress_pct(self) -> int:
        """Approximate progress percentage for the UI progress bar."""
        _pct = {
            JobStatus.QUEUED:     0,
            JobStatus.INGESTING:  10,
            JobStatus.ROUTING:    20,
            JobStatus.ANCHORING:  30,
            JobStatus.EXTRACTING: 60,
            JobStatus.MERGING:    80,
            JobStatus.EXPORTING:  90,
            JobStatus.DONE:       100,
            JobStatus.FAILED:     100,
        }
        return _pct[self]


class WindowType(str, Enum):
    """
    Classifies an event date relative to the admission window.
    Used by Step 4 (normalise) and Step 8 (export history/followup).
    """
    ADMISSION      = "ADMISSION"       # within ±48 h of admission_start
    IN_HOSPITAL    = "IN_HOSPITAL"     # after admission, before discharge
    PRE_ADMISSION  = "PRE_ADMISSION"   # before admission (> 48 h prior)
    POST_DISCHARGE = "POST_DISCHARGE"  # after discharge_end
