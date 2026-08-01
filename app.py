"""FirmAware — deployment risk predictions page.

Read-only view over scores that the batch pipeline already produced. It never
trains, never scores, and never writes: the CLI owns those paths, so what this
page shows is exactly what shipped.

Run:
    streamlit run app.py

Sources follow the same environment contract as the pipeline:
    FIRMAWARE_SCORES_URI     default outputs/scores.csv, or gs://.../scores
    FIRMAWARE_DATA_URI       default data/
    FIRMAWARE_ARTIFACTS_URI  default artifacts/, or gs://.../artifacts
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from firmaware.features import derive_features
from firmaware.io import (
    is_gcs_uri,
    join_uri,
    list_scores_uris,
    materialize_artifacts,
    read_csv,
)
from firmaware.schema import validate

SCORES_URI = os.getenv("FIRMAWARE_SCORES_URI", str(Path("outputs") / "scores.csv"))
DATA_URI = os.getenv("FIRMAWARE_DATA_URI", "data")
ARTIFACTS_URI = os.getenv("FIRMAWARE_ARTIFACTS_URI", "artifacts")
UPCOMING_URI = os.getenv("FIRMAWARE_UPCOMING_URI") or join_uri(
    DATA_URI, "upcoming_deployments.csv"
)

st.set_page_config(
    page_title="FirmAware",
    page_icon="🎛",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BG_VOID = "#0A1628"
BG_PANEL = "#0F2138"
LINE = "#2C4A68"
INK = "#EAF2FA"
INK_MUTED = "#6E8CAA"

GREEN = "#3ED598"
AMBER = "#FFB020"
RED = "#FF5470"

BAND_COLOR = {"HIGH": RED, "MEDIUM": AMBER, "LOW": GREEN}
PRED_COLOR = {"GO": GREEN, "NO_GO": RED}

MODEL_FLAG_LABELS = {
    "major_version_changed": "Major Version Change",
    "core_system_touched": "Core System Touched",
    "emergency_no_maintenance": "Emergency / No Window",
}
CONTEXT_FLAG_LABELS = {
    "high_cvss": "High CVSS (>= 7.0)",
    "protocol_mismatch": "Protocol Mismatch",
    "high_network_stress": "High Network Stress (>= 0.70)",
    "repeat_failure_device": "Repeat Failure Device",
    "tier_1_device": "Tier 1 Device",
    "high_criticality_site": "High Criticality Site",
}
FLAG_LABELS = {**MODEL_FLAG_LABELS, **CONTEXT_FLAG_LABELS}

st.markdown(
    f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap');

html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}

.stApp {{
    background-color: {BG_VOID};
    background-image:
        linear-gradient(rgba(44,74,104,0.14) 1px, transparent 1px),
        linear-gradient(90deg, rgba(44,74,104,0.14) 1px, transparent 1px);
    background-size: 32px 32px;
    color: {INK};
}}

h1, h2, h3, h4 {{ font-family: 'Space Grotesk', sans-serif; color: {INK}; }}
hr {{ border-color: {LINE}; }}

.fa-titleblock {{
    border: 1px solid {LINE};
    border-radius: 4px;
    background: {BG_PANEL};
    padding: 22px 28px;
    margin-bottom: 22px;
    position: relative;
}}
.fa-titleblock::before, .fa-titleblock::after {{
    content: "";
    position: absolute;
    width: 10px; height: 10px;
    border-color: {INK_MUTED};
}}
.fa-titleblock::before {{ top: 6px; left: 6px; border-top: 1px solid; border-left: 1px solid; }}
.fa-titleblock::after  {{ bottom: 6px; right: 6px; border-bottom: 1px solid; border-right: 1px solid; }}
.fa-wordmark {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 30px;
    letter-spacing: 3px;
    text-transform: uppercase;
    margin: 0;
}}
.fa-tagline {{ font-size: 13px; color: {INK_MUTED}; margin-top: 4px; letter-spacing: 0.4px; }}
.fa-rev {{
    position: absolute;
    top: 22px; right: 28px;
    text-align: right;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: {INK_MUTED};
    line-height: 1.6;
}}
.fa-rev b {{ color: {INK}; }}

.fa-readout-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
.fa-readout {{
    flex: 1;
    min-width: 110px;
    border: 1px solid {LINE};
    border-radius: 4px;
    background: {BG_PANEL};
    padding: 14px 16px;
    text-align: left;
}}
.fa-readout-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: {INK_MUTED};
}}
.fa-readout-value {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 28px;
    font-weight: 700;
    margin-top: 2px;
    color: {INK};
}}

.fa-panel {{
    border: 1px solid {LINE};
    border-radius: 4px;
    background: {BG_PANEL};
    padding: 18px 22px;
    margin-bottom: 16px;
}}
.fa-panel-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: {INK_MUTED};
    margin-bottom: 10px;
}}
.fa-field {{ font-size: 13.5px; margin: 3px 0; color: {INK}; }}
.fa-field b {{ color: {INK_MUTED}; font-weight: 500; }}
.fa-field code {{
    font-family: 'JetBrains Mono', monospace;
    background: rgba(44,74,104,0.35);
    padding: 1px 6px;
    border-radius: 3px;
    color: {INK};
}}

.fa-stamp {{
    display: inline-block;
    border: 3px solid var(--stamp-color);
    color: var(--stamp-color);
    padding: 8px 22px;
    border-radius: 6px;
    transform: rotate(-4deg);
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 18px;
    letter-spacing: 2px;
    text-transform: uppercase;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--stamp-color) 18%, transparent) inset;
}}
.fa-stamp-eyebrow {{
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.8px;
    color: {AMBER};
    background: rgba(255,176,32,0.12);
    border: 1px solid rgba(255,176,32,0.45);
    border-radius: 4px;
    padding: 5px 10px;
    margin-bottom: 12px;
    text-transform: uppercase;
}}

.fa-switch-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
.fa-switch {{
    border: 1px solid {LINE};
    border-radius: 4px;
    background: rgba(44,74,104,0.10);
    padding: 10px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
}}
.fa-switch.active {{ border-color: {RED}; background: rgba(255,84,112,0.10); }}
.fa-switch-label {{ color: {INK_MUTED}; }}
.fa-switch.active .fa-switch-label {{ color: {INK}; }}
.fa-switch-dot {{
    width: 9px; height: 9px; border-radius: 50%;
    background: {LINE};
    flex-shrink: 0;
    margin-left: 8px;
}}
.fa-switch.active .fa-switch-dot {{ background: {RED}; box-shadow: 0 0 6px {RED}; }}

.fa-section {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: {INK_MUTED};
    margin: 20px 0 10px 0;
    border-bottom: 1px solid {LINE};
    padding-bottom: 6px;
}}

[data-testid="stMetric"] {{ background: {BG_PANEL}; border: 1px solid {LINE}; border-radius: 4px; padding: 10px; }}
div[data-baseweb="select"] > div {{ background: {BG_PANEL}; border-color: {LINE}; }}

.stButton > button {{
    background: {BG_PANEL};
    border: 1px solid {LINE};
    color: {INK};
    border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    padding: 10px 18px;
    transition: all 0.15s ease;
}}
.stButton > button:hover {{
    border-color: {AMBER};
    color: {AMBER};
    background: rgba(255,176,32,0.08);
}}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60, show_spinner=False)
def load_score_runs(scores_uri: str) -> list[str]:
    return list_scores_uris(scores_uri)


@st.cache_data(ttl=60, show_spinner=False)
def load_scores(score_uri: str) -> pd.DataFrame:
    return read_csv(score_uri)


@st.cache_data(ttl=60, show_spinner=False)
def load_champion(artifacts_uri: str) -> dict:
    """Read the champion threshold so bands are never hardcoded in the UI."""
    with materialize_artifacts(artifacts_uri) as local_dir:
        metadata_path = Path(local_dir) / "metadata.json"
        if not metadata_path.is_file():
            return {}
        return json.loads(metadata_path.read_text(encoding="utf-8"))


@st.cache_data(ttl=60, show_spinner=False)
def load_features(upcoming_uri: str) -> pd.DataFrame:
    """Reuse the pipeline's own derivation so the page cannot drift from the model."""
    raw = read_csv(upcoming_uri)
    validated = validate(raw, mode="scoring")
    featured = derive_features(validated, mode="scoring").reset_index()

    context = raw.copy()
    derived = featured[
        [
            "deployment_id",
            "major_version_changed",
            "core_system_touched",
            "emergency_no_maintenance",
        ]
    ]
    merged = context.merge(derived, on="deployment_id", how="left")

    merged["high_cvss"] = (merged["max_cvss_score"] >= 7.0).astype(int)
    merged["protocol_mismatch"] = (
        merged["protocol_mismatch_flag"].fillna(0).ne(0).astype(int)
    )
    merged["high_network_stress"] = (
        merged["network_stress_score"] >= 0.70
    ).astype(int)
    merged["repeat_failure_device"] = (
        merged["past_failure_count"].fillna(0) >= 1
    ).astype(int)
    merged["tier_1_device"] = merged["fleet_tier"].eq("TIER_1").astype(int)
    merged["high_criticality_site"] = (
        merged["site_criticality"].eq("HIGH").astype(int)
    )
    return merged


def active_flags(row: pd.Series) -> list[str]:
    return [key for key in FLAG_LABELS if float(row.get(key) or 0) > 0]


def render_gauge(value_0_to_1: float, label: str, threshold: float) -> go.Figure:
    """Zones mirror model.score_dataframe: HIGH >= threshold, MEDIUM >= threshold/2."""
    pct = round(value_0_to_1 * 100, 1)
    high = round(threshold * 100, 2)
    medium = round(threshold * 50, 2)
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=pct,
            number={
                "suffix": "%",
                "font": {"size": 34, "family": "JetBrains Mono", "color": INK},
            },
            gauge={
                "axis": {
                    "range": [0, 100],
                    "tickwidth": 1,
                    "tickcolor": LINE,
                    "tickfont": {
                        "family": "JetBrains Mono",
                        "size": 10,
                        "color": INK_MUTED,
                    },
                },
                "bar": {"color": INK, "thickness": 0.22},
                "bgcolor": "rgba(0,0,0,0)",
                "borderwidth": 1,
                "bordercolor": LINE,
                "steps": [
                    {"range": [0, medium], "color": "rgba(62,213,152,0.20)"},
                    {"range": [medium, high], "color": "rgba(255,176,32,0.20)"},
                    {"range": [high, 100], "color": "rgba(255,84,112,0.20)"},
                ],
                "threshold": {
                    "line": {"color": RED, "width": 3},
                    "thickness": 0.85,
                    "value": high,
                },
            },
        )
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=10, b=38),
        height=225,
        annotations=[
            dict(
                text=label,
                x=0.5,
                y=-0.08,
                showarrow=False,
                font=dict(family="Inter", size=12, color=INK_MUTED),
            )
        ],
    )
    return fig


def render_bar(series: pd.Series, color=None, colors: dict | None = None) -> go.Figure:
    labels = series.index.tolist()
    values = series.values.tolist()
    bar_colors = (
        [colors.get(label, INK_MUTED) for label in labels] if colors else (color or AMBER)
    )
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=bar_colors))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=10),
        height=220,
        font=dict(family="Inter", size=11, color=INK_MUTED),
        xaxis=dict(showgrid=False, color=INK_MUTED),
        yaxis=dict(showgrid=True, gridcolor=LINE, color=INK_MUTED, zeroline=False),
    )
    return fig


def render_switch_grid(flag_list: list[str]) -> str:
    html = '<div class="fa-switch-grid">'
    for key, label in FLAG_LABELS.items():
        active = key in flag_list
        css_class = "fa-switch active" if active else "fa-switch"
        html += (
            f'<div class="{css_class}"><span class="fa-switch-label">{label}</span>'
            '<span class="fa-switch-dot"></span></div>'
        )
    return html + "</div>"


def stop_with_message(message: str) -> None:
    st.markdown(
        '<div class="fa-titleblock"><div class="fa-wordmark">FirmAware</div>'
        '<div class="fa-tagline">Firmware Deployment Risk — Predictions</div></div>',
        unsafe_allow_html=True,
    )
    st.error(message)
    st.stop()


try:
    score_runs = load_score_runs(SCORES_URI)
except Exception as error:  # noqa: BLE001 - surfaced verbatim to the operator
    stop_with_message(f"Could not list score runs at {SCORES_URI}: {error}")
    raise

if not score_runs:
    stop_with_message(
        f"No scores found at {SCORES_URI}. Run `python -m firmaware predict` first."
    )

selected_run = score_runs[-1]
if len(score_runs) > 1:
    selected_run = st.sidebar.selectbox(
        "Scoring run",
        options=list(reversed(score_runs)),
        format_func=lambda uri: uri.rsplit("/", 1)[-1],
    )

try:
    scores = load_scores(selected_run)
except Exception as error:  # noqa: BLE001
    stop_with_message(f"Could not read {selected_run}: {error}")
    raise

if scores.empty:
    stop_with_message(f"{selected_run} contains no rows.")

try:
    features = load_features(UPCOMING_URI)
except Exception as error:  # noqa: BLE001
    st.sidebar.warning(f"Equipment context unavailable: {error}")
    features = pd.DataFrame(columns=["deployment_id"])

df = scores.merge(features, on="deployment_id", how="left", suffixes=("", "_input"))

metadata = {}
try:
    metadata = load_champion(ARTIFACTS_URI)
except Exception as error:  # noqa: BLE001
    st.sidebar.warning(f"Champion metadata unavailable: {error}")

threshold = float(metadata.get("threshold", 0.5))
model_name = metadata.get("model_name", "unknown")
model_run = str(df["model_run"].iloc[0]) if "model_run" in df.columns else "N/A"
scored_at = str(df["scored_at"].iloc[0])[:19] if "scored_at" in df.columns else "N/A"

st.markdown(
    f"""
<div class="fa-titleblock">
    <div class="fa-wordmark">FirmAware</div>
    <div class="fa-tagline">Firmware Deployment Risk — Predictions</div>
    <div class="fa-rev">
        MODEL <b>{model_name}</b><br>
        RUN <b>{model_run[:19]}</b><br>
        SCORED <b>{scored_at}</b><br>
        THRESHOLD <b>{threshold:.4f}</b>
    </div>
</div>
""",
    unsafe_allow_html=True,
)

st.sidebar.markdown("**Source**")
st.sidebar.code(selected_run, language=None)
st.sidebar.caption(
    "Read-only view. Scores are produced by the batch pipeline; this page never "
    "trains or re-scores."
)
if is_gcs_uri(selected_run):
    st.sidebar.caption("Reading published cloud scores.")

view = st.radio(
    "View",
    options=["Deployment Inspector", "Fleet Overview"],
    horizontal=True,
    label_visibility="collapsed",
)

st.markdown("<br>", unsafe_allow_html=True)

if view == "Fleet Overview":
    total = len(df)
    go_count = int((df["risk_prediction"] == "GO").sum())
    nogo_count = int((df["risk_prediction"] == "NO_GO").sum())
    high_c = int((df["risk_band"] == "HIGH").sum())
    med_c = int((df["risk_band"] == "MEDIUM").sum())
    low_c = int((df["risk_band"] == "LOW").sum())
    avg_risk = float(df["risk_probability"].mean())

    col_gauge, col_readouts = st.columns([1, 2.4])
    with col_gauge:
        st.plotly_chart(
            render_gauge(avg_risk, "FLEET AVERAGE RISK", threshold),
            width="stretch",
        )

    with col_readouts:
        st.markdown(
            f"""
        <div class="fa-readout-row">
            <div class="fa-readout"><div class="fa-readout-label">Total</div><div class="fa-readout-value">{total}</div></div>
            <div class="fa-readout"><div class="fa-readout-label">Go</div><div class="fa-readout-value" style="color:{GREEN}">{go_count}</div></div>
            <div class="fa-readout"><div class="fa-readout-label">No-Go</div><div class="fa-readout-value" style="color:{RED}">{nogo_count}</div></div>
            <div class="fa-readout"><div class="fa-readout-label">High</div><div class="fa-readout-value" style="color:{RED}">{high_c}</div></div>
            <div class="fa-readout"><div class="fa-readout-label">Medium</div><div class="fa-readout-value" style="color:{AMBER}">{med_c}</div></div>
            <div class="fa-readout"><div class="fa-readout-label">Low</div><div class="fa-readout-value" style="color:{GREEN}">{low_c}</div></div>
        </div>
        """,
            unsafe_allow_html=True,
        )

        has_vendor = "vendor_name" in df.columns and df["vendor_name"].notna().any()
        f1, f2, f3 = st.columns(3)
        with f1:
            band_filter = st.multiselect(
                "Risk band", ["HIGH", "MEDIUM", "LOW"], default=["HIGH", "MEDIUM", "LOW"]
            )
        with f2:
            pred_filter = st.multiselect(
                "Prediction", ["GO", "NO_GO"], default=["GO", "NO_GO"]
            )
        with f3:
            vendors = (
                sorted(df["vendor_name"].dropna().unique().tolist()) if has_vendor else []
            )
            vendor_filter = st.multiselect("Vendor", vendors, default=vendors)

    mask = df["risk_band"].isin(band_filter) & df["risk_prediction"].isin(pred_filter)
    if has_vendor:
        mask &= df["vendor_name"].isin(vendor_filter)
    filtered = df[mask].copy()

    st.markdown(
        f'<div class="fa-section">Deployment Register — {len(filtered)} shown</div>',
        unsafe_allow_html=True,
    )

    register_columns = [
        column
        for column in [
            "deployment_id",
            "vendor_name",
            "device_type",
            "hardware_series",
            "current_firmware",
            "target_firmware",
            "risk_probability",
            "risk_prediction",
            "risk_band",
        ]
        if column in filtered.columns
    ]
    display = filtered[register_columns].copy()
    display["risk_probability"] = display["risk_probability"].map(lambda x: f"{x:.1%}")
    display = display.rename(
        columns={
            "deployment_id": "Deployment",
            "vendor_name": "Vendor",
            "device_type": "Type",
            "hardware_series": "Hardware",
            "current_firmware": "Current FW",
            "target_firmware": "Target FW",
            "risk_probability": "P(Failure)",
            "risk_prediction": "Prediction",
            "risk_band": "Band",
        }
    )
    st.dataframe(display, width="stretch", hide_index=True, height=360)
    st.download_button(
        "Download shown rows (CSV)",
        filtered[register_columns].to_csv(index=False).encode("utf-8"),
        file_name="firmaware_predictions.csv",
        mime="text/csv",
    )

    st.markdown('<div class="fa-section">Distribution</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.caption("By risk band")
        band_counts = (
            df["risk_band"].value_counts().reindex(["HIGH", "MEDIUM", "LOW"]).fillna(0)
        )
        st.plotly_chart(
            render_bar(band_counts, colors=BAND_COLOR), width="stretch"
        )
    with c2:
        st.caption("No-Go by vendor")
        if has_vendor:
            vendor_counts = df[df["risk_prediction"] == "NO_GO"][
                "vendor_name"
            ].value_counts()
            st.plotly_chart(
                render_bar(vendor_counts, color=AMBER), width="stretch"
            )
        else:
            st.caption("Vendor context unavailable.")
    with c3:
        st.caption("By deployment type")
        if "deployment_type" in df.columns and df["deployment_type"].notna().any():
            type_counts = df["deployment_type"].value_counts()
            st.plotly_chart(
                render_bar(type_counts, color=INK_MUTED), width="stretch"
            )
        else:
            st.caption("Deployment type context unavailable.")

else:
    selected_id = st.selectbox(
        "Select a deployment",
        options=["— Select —"] + sorted(df["deployment_id"].astype(str).tolist()),
        label_visibility="collapsed",
    )

    if selected_id == "— Select —":
        st.info("Select a deployment ID above to inspect it.")
        st.stop()

    row = df[df["deployment_id"].astype(str) == selected_id].iloc[0]
    band = row["risk_band"]
    pred = row["risk_prediction"]
    prob = float(row["risk_probability"])
    flags = active_flags(row)
    stamp_color = PRED_COLOR.get(pred, INK_MUTED)

    col_stamp, col_gauge = st.columns([1, 1])
    with col_stamp:
        st.markdown(
            f"""
        <div class="fa-panel" style="display:flex; flex-direction:column; justify-content:center; align-items:flex-start; height:225px;">
            <div class="fa-panel-label">{selected_id}</div>
            <div class="fa-stamp-eyebrow">ML Signal — prediction only, not a final decision</div>
            <div class="fa-stamp" style="--stamp-color:{stamp_color};">{str(pred).replace('_', ' ')}</div>
            <div class="fa-field" style="margin-top:14px;">Risk band: <b style="color:{BAND_COLOR.get(band, INK_MUTED)}">{band}</b></div>
        </div>
        """,
            unsafe_allow_html=True,
        )
    with col_gauge:
        st.plotly_chart(
            render_gauge(prob, "FAILURE PROBABILITY", threshold),
            width="stretch",
        )

    unseen = str(row.get("unseen_categories", "{}") or "{}")
    if unseen not in {"{}", "nan"}:
        st.warning(
            f"Unseen categories for {selected_id}: {unseen}. "
            "The preprocessor treated these as unknown, so treat this score with care."
        )

    def field(label: str, value: object, code: bool = False) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            value = "N/A"
        rendered = f"<code>{value}</code>" if code else value
        return f'<div class="fa-field"><b>{label}</b> {rendered}</div>'

    st.markdown(
        '<div class="fa-section">Equipment Record</div>', unsafe_allow_html=True
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            '<div class="fa-panel"><div class="fa-panel-label">Device</div>'
            + field("ID", row.get("device_id"), code=True)
            + field("Vendor", row.get("vendor_name"))
            + field("Type", row.get("device_type"))
            + field("Hardware", row.get("hardware_series"))
            + field("Fleet tier", row.get("fleet_tier"))
            + "</div>",
            unsafe_allow_html=True,
        )
    with c2:
        window = row.get("maintenance_window")
        st.markdown(
            '<div class="fa-panel"><div class="fa-panel-label">Site &amp; Deployment</div>'
            + field("Site", row.get("site_id"), code=True)
            + field("Criticality", row.get("site_criticality"))
            + field("Type", row.get("deployment_type"))
            + field(
                "Maintenance window",
                "N/A" if pd.isna(window) else ("Yes" if float(window) else "No"),
            )
            + field("Past failures", row.get("past_failure_count"))
            + "</div>",
            unsafe_allow_html=True,
        )
    with c3:
        magnitude = row.get("version_jump_magnitude")
        st.markdown(
            '<div class="fa-panel"><div class="fa-panel-label">Firmware</div>'
            + field("Current", row.get("current_firmware"), code=True)
            + field("Target", row.get("target_firmware"), code=True)
            + field(
                "Major version change",
                "Yes" if float(row.get("major_version_changed") or 0) else "No",
            )
            + field(
                "Jump magnitude",
                "N/A" if pd.isna(magnitude) else f"{float(magnitude):.2f}",
            )
            + field(
                "Core system",
                "Touched"
                if float(row.get("core_system_touched") or 0)
                else "Untouched",
            )
            + "</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        '<div class="fa-section">Cross-Vendor &amp; Vulnerability Context</div>',
        unsafe_allow_html=True,
    )
    c4, c5, c6, c7 = st.columns(4)
    for column, label, value in [
        (c4, "Cross-vendor dependencies", row.get("cross_vendor_dependency_count")),
        (c5, "Dependent devices", row.get("dependent_device_count")),
        (c6, "CVE count", row.get("cve_count")),
        (c7, "Max CVSS score", row.get("max_cvss_score")),
    ]:
        shown = "N/A" if value is None or pd.isna(value) else value
        with column:
            st.markdown(
                f"""
            <div class="fa-readout">
                <div class="fa-readout-label">{label}</div>
                <div class="fa-readout-value" style="font-size:22px;">{shown}</div>
            </div>
            """,
                unsafe_allow_html=True,
            )

    st.markdown(
        '<div class="fa-section">Risk Flag Panel'
        f' — {len(flags)} of {len(FLAG_LABELS)} raised</div>',
        unsafe_allow_html=True,
    )
    st.markdown(render_switch_grid(flags), unsafe_allow_html=True)
    if not flags:
        st.caption(
            f"No risk flags raised for {selected_id}. Every switch above is "
            "genuinely off — this deployment tripped none of the conditions."
        )
    st.caption(
        "Model-derived features: "
        + ", ".join(MODEL_FLAG_LABELS.values())
        + ". The remaining flags are operator context read from the scoring input; "
        "they are not separate model inputs."
    )
