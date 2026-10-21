"""Name a deployment's risks in the vocabulary the evidence was written in.

The corpus was generated from FirmGuard's nine risk flags, so its words carry
FirmGuard's definitions, and a query or a grounding check only lines up with
the documents if it uses the same ones. Three differ from what the Streamlit
page shows, deliberately:

    flag                    here (FirmGuard, the corpus)   app.py
    major_version_jump      major number goes up           any major change
    repeat_failure_device   >= 3 past failures             >= 1
    high_network_stress     >= 0.65                        >= 0.70

`core_system_touched` is split into the kernel_touched and bootloader_touched
the documents actually name.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from ..features import derive_features
from ..schema import validate

FLAGS = [
    "major_version_jump",
    "kernel_touched",
    "bootloader_touched",
    "protocol_mismatch_flag",
    "high_cvss",
    "high_criticality_site",
    "emergency_no_maintenance",
    "repeat_failure_device",
    "tier_1_device",
    "high_network_stress",
]
# Words that count as a text discussing a flag. Two flags have no entry in the
# corpus at all: no document mentions network stress, and only the matrices
# mention TIER_1. That is a property of the corpus, kept visible here.
FLAG_KEYWORDS = {
    "major_version_jump": ["major_version_jump", "major version", "version jump"],
    "kernel_touched": ["kernel_touched", "kernel"],
    "bootloader_touched": ["bootloader_touched", "bootloader"],
    "protocol_mismatch_flag": ["protocol_mismatch", "protocol mismatch"],
    "high_cvss": ["high_cvss", "cvss"],
    "high_criticality_site": ["high_criticality_site", "high criticality", "high-criticality"],
    "emergency_no_maintenance": ["emergency_no_maintenance", "emergency", "maintenance window"],
    "repeat_failure_device": ["repeat_failure_device", "repeat failure", "past failure"],
    "tier_1_device": ["tier_1", "tier 1"],
    "high_network_stress": ["network stress", "network_stress"],
}

# Bands of version_jump_magnitude the transition summaries cover. Not stated in
# any document, but confirmed by them: counting non-SUCCESS historical rows in
# [5, 8) and [8, inf) per vendor reproduces each summary's stated record count
# (470/470, 234/234, 58/58, 71/71, 96/96, and the other three within one).
# Distinct from the major_version_jump flag, which is about the major number.
TRANSITION_BANDS = (("extreme", 8.0), ("major", 5.0))


def transition_class(version_jump_magnitude: float | None) -> str | None:
    if version_jump_magnitude is None or pd.isna(version_jump_magnitude):
        return None
    for name, floor in TRANSITION_BANDS:
        if version_jump_magnitude >= floor:
            return name
    return None


def mentions(text: str, flag: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in FLAG_KEYWORDS[flag])


def deployment_flags(
    raw: pd.DataFrame, mode: Literal["training", "scoring"] = "scoring"
) -> pd.DataFrame:
    """Raw rows indexed by deployment_id, plus `active_flags` and `transition_class`.

    Goes through the pipeline's own validation and feature derivation, so the
    contract is enforced here exactly as it is for scoring.
    """
    derived = derive_features(validate(raw, mode=mode), mode=mode)
    # derive_features drops the version strings a query needs, so the derived
    # columns are joined back onto the raw rows, as app.py does.
    frame = raw.set_index("deployment_id").join(
        derived[["major_version_jump", "emergency_no_maintenance"]]
    )
    raised = pd.DataFrame(
        {
            "major_version_jump": frame["major_version_jump"].fillna(0).gt(0),
            "kernel_touched": frame["kernel_touched"].fillna(0).ne(0),
            "bootloader_touched": frame["bootloader_touched"].fillna(0).ne(0),
            "protocol_mismatch_flag": frame["protocol_mismatch_flag"].fillna(0).ne(0),
            "high_cvss": frame["max_cvss_score"].ge(7.0),
            "high_criticality_site": frame["site_criticality"].eq("HIGH"),
            "emergency_no_maintenance": frame["emergency_no_maintenance"].eq(1),
            "repeat_failure_device": frame["past_failure_count"].fillna(0).ge(3),
            "tier_1_device": frame["fleet_tier"].eq("TIER_1"),
            "high_network_stress": frame["network_stress_score"].fillna(0).ge(0.65),
        },
        index=frame.index,
    )
    frame["active_flags"] = [
        [flag for flag, on in zip(FLAGS, values) if on] for values in raised[FLAGS].to_numpy()
    ]
    frame["transition_class"] = frame["version_jump_magnitude"].map(transition_class)
    return frame
