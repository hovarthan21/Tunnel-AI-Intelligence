import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from pathlib import Path
import time

st.set_page_config(page_title="Tunnel Load Monitor", page_icon="🏗️", layout="wide")

DATA_FILE = Path(__file__).parent / "tunnel_load_monitor_all_data.csv"

# SLEEK PREMIUM DARK STYLE
st.markdown("""
<style>
.stApp {background:#050505;color:#f4f4f4}
[data-testid="stSidebar"] {background:#030303;border-right:1px solid #222}
.block-container {max-width:1500px;padding-top:1.2rem}
.card {
    background: #0b0b0b;
    border: 1px solid #242424;
    border-radius: 12px;
    padding: 18px;
    margin-bottom: 15px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    transition: all 0.3s ease;
}
.card:hover {
    border-color: #3a3a3a;
}
.badge {padding:5px 10px;border-radius:7px;font-weight:700;font-size:12px;display:inline-block}
.full {background:#073b18;color:#22c55e}
.degraded {background:#493b00;color:#facc15}
.review {background:#47220a;color:#f97316}
.low {background:#450909;color:#ef4444}
.fault {background:#292929;color:#aaa}
</style>
""", unsafe_allow_html=True)

# LOAD AND DYNAMICALLY PREPROCESS SENSOR DATA
@st.cache_data
def load_data():
    df = pd.read_csv(DATA_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(by=["timestamp", "sensor_id"]).reset_index(drop=True)
    
    # 1. Calibration workflow to calculate calibrated load
    df["calculated_load_kN"] = (
        (df["raw_value"] - df["R0"])
        * df["GF"]
        * df["BF"]
        + df["thermal_coeff"] * (df["temperature_C"] - 25.0)
    )
    
    # 2. Determine initial validity flag (exclude NaN raw or invalid tags from averages)
    df["is_valid_initial"] = (
        df["validity_flag"].eq("VALID") & 
        df["raw_value"].notna() & 
        (df["calculated_load_kN"] >= 0.0)
    )
    
    # 3. Calculate initial section averages (per timestamp & section) for deviation baseline
    group_avgs = df[df["is_valid_initial"]].groupby(["timestamp", "section_id"])["calculated_load_kN"].mean().reset_index().rename(columns={"calculated_load_kN": "section_average"})
    df = df.merge(group_avgs, on=["timestamp", "section_id"], how="left")
    
    # 4. Calculate deviation from average
    df["deviation_pct"] = ((df["calculated_load_kN"] - df["section_average"]) / df["section_average"]) * 100
    
    # 5. Classify final sensor status and validity
    statuses = []
    for _, r in df.iterrows():
        # Check if offline/invalid
        if r["validity_flag"] == "INVALID" or pd.isna(r["raw_value"]):
            statuses.append(("INVALID", "Sensor offline / signal lost", False))
        elif r["calculated_load_kN"] < 0.0:
            statuses.append(("SENSOR FAULT", "Negative sensor output", False))
        elif r["calculated_load_kN"] > 250.0:
            statuses.append(("CRITICAL", f"Load exceeds rated capacity ({r['calculated_load_kN']:.1f} kN)", False))
        else:
            dev = r["deviation_pct"]
            if pd.notna(dev):
                if abs(dev) > 25.0:
                    statuses.append(("ABNORMAL", f"Large deviation from section average ({dev:+.1f}%)", True))
                elif abs(dev) > 15.0:
                    statuses.append(("WARNING", f"Moderate deviation from section average ({dev:+.1f}%)", True))
                else:
                    statuses.append(("NORMAL", "Sensor operating normally", True))
            else:
                statuses.append(("NORMAL", "Sensor operating normally", True))
                
    df["status"] = [s[0] for s in statuses]
    df["status_reason"] = [s[1] for s in statuses]
    df["is_valid"] = [s[2] for s in statuses]
    
    # 6. Re-calculate final section averages and count valid sensors using only valid readings
    final_avgs = df[df["is_valid"]].groupby(["timestamp", "section_id"])["calculated_load_kN"].mean().reset_index().rename(columns={"calculated_load_kN": "final_section_average"})
    df = df.merge(final_avgs, on=["timestamp", "section_id"], how="left")
    
    valid_counts = df[df["is_valid"]].groupby(["timestamp", "section_id"])["sensor_id"].count().reset_index().rename(columns={"sensor_id": "valid_count"})
    df = df.merge(valid_counts, on=["timestamp", "section_id"], how="left")
    df["valid_count"] = df["valid_count"].fillna(0).astype(int)
    
    return df

df = load_data()
timestamps = sorted(df["timestamp"].unique())

# INITIALIZE STATE FOR LIVE MONITORING SIMULATION
if "sim_index" not in st.session_state:
    st.session_state.sim_index = 0
if "playing" not in st.session_state:
    st.session_state.playing = True
if "last_advance_time" not in st.session_state:
    st.session_state.last_advance_time = time.time()
if "alerts_history" not in st.session_state:
    st.session_state.alerts_history = []
if "active_alerts" not in st.session_state:
    st.session_state.active_alerts = {}
if "prev_sim_index" not in st.session_state:
    st.session_state.prev_sim_index = 0

# SIDEBAR SIMULATION PANEL CONTROLS
st.sidebar.markdown("## 🏗️ Tunnel Load Monitor")
st.sidebar.caption("12 Sensors • 4 Sections")

st.sidebar.subheader("Simulation Engine")
playing = st.sidebar.toggle("Auto-Stream Live Data", value=st.session_state.playing)
st.session_state.playing = playing

interval = st.sidebar.slider("Step Duration (seconds)", 0.5, 3.0, 1.0, step=0.5)

# Slider to scrub manually
scrubbed_index = st.sidebar.slider("Timeline Explorer", 0, len(timestamps) - 1, int(st.session_state.sim_index))
if scrubbed_index != st.session_state.sim_index:
    st.session_state.sim_index = scrubbed_index
    # Clear active alerts if we jumped manually to prevent bad transitions
    st.session_state.active_alerts = {}
    st.session_state.prev_sim_index = scrubbed_index

if st.sidebar.button("Reset Timeline to T=0"):
    st.session_state.sim_index = 0
    st.session_state.active_alerts = {}
    st.session_state.prev_sim_index = 0
    st.rerun()

# Ensure active alerts are cleared if we jumped non-sequentially
prev = st.session_state.prev_sim_index
curr = st.session_state.sim_index
if curr != prev + 1 and not (curr == 0 and prev == len(timestamps) - 1):
    st.session_state.active_alerts = {}
st.session_state.prev_sim_index = curr

# GET THE CURRENT SIMULATION TIMESTAMP SLICE
current_ts = timestamps[st.session_state.sim_index]
latest = df[df.timestamp == current_ts].copy()

# DYNAMIC ALERT ENGINE: Tracking fault events and states
def process_alerts(latest_df, ts):
    # Reset active alerts on loop-back to start fresh
    if st.session_state.sim_index == 0 and len(st.session_state.active_alerts) > 0:
        for sensor_id in list(st.session_state.active_alerts.keys()):
            active = st.session_state.active_alerts[sensor_id]
            active["resolved"] = True
            active["end_time"] = ts
            del st.session_state.active_alerts[sensor_id]

    for _, row in latest_df.iterrows():
        sensor_id = row["sensor_id"]
        status = row["status"]
        section_id = row["section_id"]
        val = row["calculated_load_kN"]
        reason = row["status_reason"]
        
        is_fault = status in ["SENSOR FAULT", "INVALID", "CRITICAL"]
        
        if is_fault:
            if sensor_id not in st.session_state.active_alerts:
                severity = "CRITICAL" if status in ["SENSOR FAULT", "INVALID"] else "WARNING"
                new_alert = {
                    "sensor_id": sensor_id,
                    "section_id": section_id,
                    "start_time": ts,
                    "end_time": ts,
                    "abnormal_value": val if pd.notna(val) else "N/A",
                    "severity": severity,
                    "status": status,
                    "reason": reason,
                    "readings_count": 1,
                    "resolved": False
                }
                st.session_state.active_alerts[sensor_id] = new_alert
                st.session_state.alerts_history.append(new_alert)
            else:
                active = st.session_state.active_alerts[sensor_id]
                active["end_time"] = ts
                active["readings_count"] += 1
                if pd.notna(val):
                    active["abnormal_value"] = val
        else:
            if sensor_id in st.session_state.active_alerts:
                active = st.session_state.active_alerts[sensor_id]
                active["end_time"] = ts
                active["resolved"] = True
                del st.session_state.active_alerts[sensor_id]

process_alerts(latest, current_ts)

# HELPER: Map confidence level to CSS class
def badge(c):
    return {"FULL": "full", "DEGRADED": "degraded", "REVIEW": "review", "LOW": "low", "FAULT": "fault"}[c]

# NAVIGATION SETUP
page = st.sidebar.radio("Navigation", [
    "Overview", "Live Streaming", "Section Averages",
    "Sensor Diagnostics", "Historical Trends", "Alerts & Events", "Settings"
])
st.sidebar.divider()
st.sidebar.success("● Simulation Active" if st.session_state.playing else "⏸ Simulation Paused")
st.sidebar.caption(f"Current Date/Time:\n{current_ts.strftime('%d %b %Y • %H:%M:%S')}")
st.sidebar.caption(f"Progress: {st.session_state.sim_index + 1} / {len(timestamps)} frames")

# SECTION AVERAGING & QUALITY CALCULATION
def calculate_section_summary(ts_df):
    result = []
    for sec in ["A", "B", "C", "D"]:
        s = ts_df[ts_df.section_id == sec]
        valid_sensors = s[s.is_valid]
        valid_count = len(valid_sensors)
        
        avg = s["final_section_average"].iloc[0] if valid_count > 0 else np.nan
        
        # Calculate spread
        if valid_count >= 2:
            val_min = valid_sensors["calculated_load_kN"].min()
            val_max = valid_sensors["calculated_load_kN"].max()
            spread = (val_max - val_min) / avg * 100 if avg else 0.0
        else:
            spread = np.nan
            
        # Determine confidence
        if valid_count == 3:
            confidence = "FULL" if (pd.isna(spread) or spread <= 15.0) else "REVIEW"
        elif valid_count == 2:
            confidence = "DEGRADED"
        elif valid_count == 1:
            confidence = "LOW"
        else:
            confidence = "FAULT"
            
        result.append({
            "Section": sec,
            "Average Load (kN)": avg,
            "Valid Sensors": valid_count,
            "Spread (%)": spread,
            "Confidence": confidence
        })
    return pd.DataFrame(result)

summary = calculate_section_summary(latest)

# ==========================================
# PAGE 1: OVERVIEW DASHBOARD
# ==========================================
if page == "Overview":
    st.title("Overview Dashboard")
    st.caption("Real-time summary of tunnel load across all sections")
    
    # Alert summary row
    sensor_status_vals = latest["status"].value_counts()
    normal_cnt = int(sensor_status_vals.get("NORMAL", 0))
    warning_cnt = int(sensor_status_vals.get("WARNING", 0) + sensor_status_vals.get("ABNORMAL", 0))
    fault_cnt = int(sensor_status_vals.get("SENSOR FAULT", 0) + sensor_status_vals.get("INVALID", 0) + sensor_status_vals.get("CRITICAL", 0))
    
    kpi_cols = st.columns(4)
    kpi_cols[0].metric("Total Sensors", "12")
    kpi_cols[1].metric("Normal", f"{normal_cnt} / 12")
    kpi_cols[2].metric("Warnings", f"{warning_cnt}")
    kpi_cols[3].metric("Faults / Offline", f"{fault_cnt}")
    
    st.divider()
    
    # Section cards
    cols = st.columns(4)
    section_colors = {"A": "#22c55e", "B": "#facc15", "C": "#f97316", "D": "#ef4444"}
    
    for col, (_, r) in zip(cols, summary.iterrows()):
        sec_id = r.Section
        value = "—" if pd.isna(r["Average Load (kN)"]) else f'{r["Average Load (kN)"]:.1f} kN'
        spread = "—" if pd.isna(r["Spread (%)"]) else f'{r["Spread (%)"]:.2f}%'
        
        # Check if there are any faulty/abnormal sensors in this section
        sec_sensors = latest[latest.section_id == sec_id]
        sec_faults = sec_sensors[~sec_sensors.is_valid]
        fault_msgs = []
        for _, s_row in sec_faults.iterrows():
            fault_msgs.append(f"<div style='color:#ef4444; font-size:12px; margin-top:2px;'>⚠️ {s_row['sensor_id']} ({s_row['status']})</div>")
        
        fault_html = "".join(fault_msgs) if fault_msgs else "<div style='color:#22c55e; font-size:12px; margin-top:2px;'>🟢 All sensors operating</div>"
        badge_class = badge(r.Confidence)
        
        with col:
            st.markdown(f"""
            <div class="card">
                <b style="color:{section_colors[sec_id]}; font-size: 16px;">Section {sec_id}</b>
                <h1 style="margin: 5px 0 10px 0; font-size: 32px;">{value}</h1>
                <span class="badge {badge_class}">{r.Confidence}</span>
                <hr style="border: 0; border-top: 1px solid #222; margin: 10px 0;">
                <div style="font-size:13px; color:#aaa; line-height: 1.5;">
                    <b>Spread:</b> {spread}<br>
                    <b>Valid Sensors:</b> {int(r["Valid Sensors"])} / 3<br>
                    <b>Status:</b> {fault_html}
                </div>
            </div>
            """, unsafe_allow_html=True)
            
    # Tunnel layout schematic
    st.subheader("Tunnel Structural Schematic — 12 Sensors")
    cols = st.columns(12)
    sensor_order = [f"{s}{n}" for s in "ABCD" for n in range(1, 4)]
    for i, sid in enumerate(sensor_order):
        r = latest[latest.sensor_id == sid].iloc[0]
        status = r["status"]
        
        status_indicators = {
            "NORMAL": "🟢",
            "WARNING": "🟡",
            "ABNORMAL": "🟠",
            "SENSOR FAULT": "🔴",
            "INVALID": "⚪",
            "CRITICAL": "🚨"
        }
        indicator = status_indicators.get(status, "🟢")
        
        with cols[i]:
            st.markdown(f"""
            <div style="text-align: center; background: #0c0c0c; border: 1px solid #222; border-radius: 8px; padding: 10px;">
                <span style="font-size: 14px; font-weight: bold; color: {section_colors[sid[0]]};">{sid}</span><br>
                <span style="font-size: 20px; margin: 5px 0; display: inline-block;">{indicator}</span><br>
                <span style="font-size: 10px; color: #888; text-transform: uppercase;">{status}</span>
            </div>
            """, unsafe_allow_html=True)
            
    # Overall summary metrics
    st.divider()
    overall_load = summary["Average Load (kN)"].mean()
    total_valid = int(summary["Valid Sensors"].sum())
    overall_conf = "FULL" if all(summary.Confidence == "FULL") else "DEGRADED" if total_valid >= 8 else "LOW"
    
    a, b, c = st.columns(3)
    a.metric("Overall Tunnel Load Average", f"{overall_load:.1f} kN" if pd.notna(overall_load) else "N/A")
    b.metric("Tunnel Health Confidence", overall_conf)
    c.metric("Active Sensors", f"{total_valid} / 12")

# ==========================================
# PAGE 2: LIVE STREAMING TAB
# ==========================================
elif page == "Live Streaming":
    st.title("● Live Streaming Data")
    st.caption(f"Continuous looping simulated sensor values. Timestamp: {current_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    
    total_valid = int(summary["Valid Sensors"].sum())
    faulty_cnt = sum(1 for status in latest["status"] if status in ["SENSOR FAULT", "INVALID", "CRITICAL"])
    warning_cnt = sum(1 for status in latest["status"] if status in ["WARNING", "ABNORMAL"])
    overall_load = summary["Average Load (kN)"].mean()
    
    # 4 KPI cards at the top
    a, b, c, d = st.columns(4)
    a.metric("Active Sensors", f"{total_valid} / 12")
    b.metric("Faulty Sensors", f"{faulty_cnt}")
    b.caption("Excluded from averages")
    c.metric("Warnings", f"{warning_cnt}")
    d.metric("Current Tunnel Load", f"{overall_load:.1f} kN" if pd.notna(overall_load) else "N/A")
    
    st.divider()
    
    # Sensor selector
    sensor_choices = ["All Sensors"] + [f"{s}{n}" for s in "ABCD" for n in range(1, 4)]
    selected_sensor = st.selectbox("Select Sensor for Wave Analysis", sensor_choices)
    
    # Calculate rolling window indices (30 points of history)
    if st.session_state.sim_index < 30:
        indices = list(range(len(timestamps) - (30 - st.session_state.sim_index), len(timestamps))) + list(range(0, st.session_state.sim_index + 1))
    else:
        indices = list(range(st.session_state.sim_index - 29, st.session_state.sim_index + 1))
        
    # Build rolling dataframe in sequence order
    rolling_frames = []
    for seq_idx, idx in enumerate(indices):
        ts = timestamps[idx]
        frame = df[df.timestamp == ts].copy()
        frame["rolling_seq"] = seq_idx
        rolling_frames.append(frame)
    rolling_df = pd.concat(rolling_frames).sort_values("rolling_seq")
    
    # If selected sensor is not All, show fault warning if currently affected
    if selected_sensor != "All Sensors":
        sensor_status = latest[latest.sensor_id == selected_sensor].iloc[0]
        if sensor_status["status"] != "NORMAL":
            st.markdown(f"""
            <div style="border-left: 5px solid #ef4444; background: #1c0909; padding: 15px; border-radius: 8px; margin-bottom: 15px;">
                <h4 style="color:#ef4444; margin:0 0 5px 0; font-family: 'Inter', sans-serif;">⚠️ SENSOR FAULT DETECTED</h4>
                <div style="font-size:14px; color:#f4f4f4; margin-top:5px;">
                    <b>Sensor ID:</b> {selected_sensor} &nbsp; | &nbsp; 
                    <b>Section:</b> {selected_sensor[0]} &nbsp; | &nbsp; 
                    <b>Value:</b> {f"{sensor_status['calculated_load_kN']:.2f} kN" if pd.notna(sensor_status['calculated_load_kN']) else 'NaN (Offline)'}<br>
                    <b>Status:</b> <span class="badge low">{sensor_status['status']}</span> &nbsp; | &nbsp; 
                    <b>Fault Reason:</b> {sensor_status['status_reason']}<br>
                    <b>Timestamp:</b> {current_ts.strftime('%H:%M:%S')}
                </div>
            </div>
            """, unsafe_allow_html=True)
            
    # Plotly rolling wave chart
    fig = go.Figure()
    colors_dict = {"A": "#22c55e", "B": "#facc15", "C": "#f97316", "D": "#ef4444"}
    
    if selected_sensor == "All Sensors":
        for sensor in [f"{s}{n}" for s in "ABCD" for n in range(1, 4)]:
            sec_id = sensor[0]
            s_data = rolling_df[rolling_df.sensor_id == sensor]
            fig.add_trace(go.Scatter(
                x=s_data["rolling_seq"],
                y=s_data["calculated_load_kN"],
                name=sensor,
                mode="lines",
                line=dict(color=colors_dict[sec_id], width=1.5),
                hoverinfo="text+name",
                hovertext=[f"Time: {t.strftime('%H:%M:%S')}<br>Load: {l:.2f} kN" if pd.notna(l) else "Offline" for t, l in zip(s_data.timestamp, s_data.calculated_load_kN)]
            ))
    else:
        sec_id = selected_sensor[0]
        s_data = rolling_df[rolling_df.sensor_id == selected_sensor]
        fig.add_trace(go.Scatter(
            x=s_data["rolling_seq"],
            y=s_data["calculated_load_kN"],
            name=selected_sensor,
            mode="lines+markers",
            line=dict(color=colors_dict[sec_id], width=3),
            marker=dict(size=6),
            hoverinfo="text+name",
            hovertext=[f"Time: {t.strftime('%H:%M:%S')}<br>Load: {l:.2f} kN" if pd.notna(l) else "Offline" for t, l in zip(s_data.timestamp, s_data.calculated_load_kN)]
        ))
        
        # Highlight live current point (always at index 29)
        if len(s_data) > 0:
            last_row = s_data.iloc[-1]
            fig.add_trace(go.Scatter(
                x=[last_row["rolling_seq"]],
                y=[last_row["calculated_load_kN"]],
                name="LIVE marker",
                mode="markers+text",
                marker=dict(color="#ef4444", size=12, symbol="circle"),
                text=["LIVE"],
                textposition="top center",
                showlegend=False
            ))
            
    # Draw vertical LIVE indicator line at the current index (29)
    fig.add_shape(
        type="line",
        x0=29, y0=rolling_df["calculated_load_kN"].min() - 20 if pd.notna(rolling_df["calculated_load_kN"].min()) else -150,
        x1=29, y1=rolling_df["calculated_load_kN"].max() + 20 if pd.notna(rolling_df["calculated_load_kN"].max()) else 400,
        line=dict(color="#ef4444", width=1, dash="dash")
    )
    
    # Custom ticks
    tick_indices = list(range(0, len(indices), 5))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#090909",
        plot_bgcolor="#090909",
        xaxis=dict(
            tickmode="array",
            tickvals=tick_indices,
            ticktext=[timestamps[indices[i]].strftime("%H:%M:%S") for i in tick_indices],
            title="Simulation Time (Rolling Window)",
            gridcolor="#1e1e1e"
        ),
        yaxis=dict(
            title="Calibrated Load (kN)",
            gridcolor="#1e1e1e"
        ),
        height=500,
        margin=dict(l=20, r=20, t=30, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    # Raw values table below graph
    st.subheader("Current Calculated Calibration Values")
    show = latest[[
        "section_id", "sensor_id", "raw_value", "R0", "GF", "BF",
        "temperature_C", "calculated_load_kN", "status"
    ]].copy()
    show.columns = [
        "Section", "Sensor", "Raw Value", "R0 (Zero Ref)", "GF (Gauge Factor)", "BF (Batch Factor)",
        "Temp (°C)", "Calibrated Load (kN)", "Health Status"
    ]
    st.dataframe(show, use_container_width=True, hide_index=True)

# ==========================================
# PAGE 3: SECTION AVERAGES
# ==========================================
elif page == "Section Averages":
    st.title("Section Averages & Confidence")
    st.caption("Averages are calculated solely from active, valid, non-faulty sensor readings.")
    
    st.dataframe(summary.round(2), use_container_width=True, hide_index=True)
    
    # 24 Hours Historical Trend Graph
    st.subheader("24-Hour Section Trend Graph")
    st.caption("Rolling history of valid section averages over the last 24 hours (96 points)")
    
    # Calculate historical indices
    if st.session_state.sim_index < 96:
        hist_indices = list(range(len(timestamps) - (96 - st.session_state.sim_index), len(timestamps))) + list(range(0, st.session_state.sim_index + 1))
    else:
        hist_indices = list(range(st.session_state.sim_index - 95, st.session_state.sim_index + 1))
        
    hist_ts = [timestamps[i] for i in hist_indices]
    hist_df = df[df.timestamp.isin(hist_ts)].copy()
    
    # Pre-calculated final_section_average grouped by timestamp and section
    trend = hist_df.groupby(["timestamp", "section_id"])["final_section_average"].first().reset_index()
    
    fig = go.Figure()
    colors = {"A": "#22c55e", "B": "#facc15", "C": "#f97316", "D": "#ef4444"}
    
    for sec in "ABCD":
        q = trend[trend.section_id == sec]
        fig.add_trace(go.Scatter(
            x=q.timestamp,
            y=q.final_section_average,
            name=f"Section {sec}",
            mode="lines",
            line=dict(color=colors[sec], width=2)
        ))
        
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#090909",
        plot_bgcolor="#090909",
        height=450,
        yaxis_title="Average Load (kN)",
        xaxis_title="Time",
        margin=dict(l=20, r=20, t=30, b=20)
    )
    st.plotly_chart(fig, use_container_width=True)

# ==========================================
# PAGE 4: SENSOR DIAGNOSTICS TAB
# ==========================================
elif page == "Sensor Diagnostics":
    st.title("Sensor Diagnostics Panel")
    st.caption("Inspect calibration values, deviation from average, and historical faults.")
    
    sec = st.selectbox("Select Section", list("ABCD"))
    st.subheader(f"Section {sec} Sensors")
    
    s_latest = latest[latest.section_id == sec]
    
    rows = []
    for _, row in s_latest.iterrows():
        sensor_id = row["sensor_id"]
        
        # Calculate fault history metrics from alerts_history
        sensor_faults = [a for a in st.session_state.alerts_history if a["sensor_id"] == sensor_id]
        fault_count = len(sensor_faults)
        if fault_count > 0:
            last_fault_time = sensor_faults[-1]["start_time"].strftime("%H:%M:%S")
            last_fault_reason = sensor_faults[-1]["reason"]
            last_fault_str = f"{last_fault_time} ({last_fault_reason})"
        else:
            last_fault_str = "None"
            
        dev = row["deviation_pct"]
        dev_str = f"{dev:+.2f}%" if pd.notna(dev) else "—"
        
        rows.append({
            "Sensor ID": sensor_id,
            "Raw Value": row["raw_value"] if pd.notna(row["raw_value"]) else "NaN",
            "Calibrated Load": f"{row['calculated_load_kN']:.2f} kN" if pd.notna(row["calculated_load_kN"]) else "N/A",
            "Validity": "VALID" if row["is_valid"] else "INVALID",
            "Deviation": dev_str,
            "Status": row["status"],
            "Last Fault Event": last_fault_str,
            "Fault Count": fault_count
        })
        
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ==========================================
# PAGE 5: HISTORICAL TRENDS
# ==========================================
elif page == "Historical Trends":
    st.title("Historical Trends Dashboard")
    st.caption("Select the history length to review long-term section load patterns.")
    
    days = st.selectbox("Historical Trend Range", ["24 Hours", "3 Days", "7 Days"])
    hours = {"24 Hours": 24, "3 Days": 72, "7 Days": 168}[days]
    
    # Calculate historical indices
    points_count = hours * 4
    if st.session_state.sim_index < points_count:
        hist_indices = list(range(len(timestamps) - (points_count - st.session_state.sim_index), len(timestamps))) + list(range(0, st.session_state.sim_index + 1))
    else:
        hist_indices = list(range(st.session_state.sim_index - (points_count - 1), st.session_state.sim_index + 1))
        
    hist_ts = [timestamps[i] for i in hist_indices]
    hist_df = df[df.timestamp.isin(hist_ts)].copy()
    
    trend = hist_df.groupby(["timestamp", "section_id"])["final_section_average"].first().reset_index()
    
    fig = go.Figure()
    colors = {"A": "#22c55e", "B": "#facc15", "C": "#f97316", "D": "#ef4444"}
    
    for sec in "ABCD":
        q = trend[trend.section_id == sec]
        fig.add_trace(go.Scatter(
            x=q.timestamp,
            y=q.final_section_average,
            name=f"Section {sec}",
            mode="lines",
            line=dict(color=colors[sec], width=2)
        ))
        
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#090909",
        plot_bgcolor="#090909",
        height=500,
        yaxis_title="Average Load (kN)",
        xaxis_title="Time",
        margin=dict(l=20, r=20, t=30, b=20)
    )
    st.plotly_chart(fig, use_container_width=True)

# ==========================================
# PAGE 6: ALERTS & EVENTS TAB
# ==========================================
elif page == "Alerts & Events":
    st.title("Alerts & Events History")
    st.caption("Active and resolved sensor issues tracked during the current session.")
    
    # Active alerts summary
    if len(st.session_state.active_alerts) > 0:
        st.subheader("🚨 Currently Active Sensor Faults")
        for sensor_id, a in st.session_state.active_alerts.items():
            st.markdown(f"""
            <div style="border-left: 5px solid #ef4444; background: #1c0909; padding: 15px; border-radius: 8px; margin-bottom: 12px;">
                <b style="color:#ef4444; font-size:16px;">CRITICAL FAULT — Sensor {sensor_id} (Section {a['section_id']})</b><br>
                <div style="font-size:14px; margin-top:5px; color:#f4f4f4;">
                    <b>Current Status:</b> {a['status']} &nbsp; | &nbsp; 
                    <b>Current Value:</b> {f"{a['abnormal_value']:.2f} kN" if isinstance(a['abnormal_value'], float) else a['abnormal_value']} &nbsp; | &nbsp; 
                    <b>Trigger Time:</b> {a['start_time'].strftime('%Y-%m-%d %H:%M:%S')}<br>
                    <b>Reason:</b> {a['reason']}
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.success("🟢 No active sensor issues detected.")
        
    st.divider()
    
    st.subheader("📋 Session Event Log")
    if len(st.session_state.alerts_history) > 0:
        alert_rows = []
        for a in st.session_state.alerts_history:
            duration_str = "Active" if not a["resolved"] else f"{int((a['end_time'] - a['start_time']).total_seconds() / 60)} min"
            alert_rows.append({
                "Severity": a["severity"],
                "Section": a["section_id"],
                "Sensor": a["sensor_id"],
                "Status": a["status"],
                "Peak Value": f"{a['abnormal_value']:.2f} kN" if isinstance(a['abnormal_value'], float) else a['abnormal_value'],
                "Start Time": a["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "End Time": a["end_time"].strftime("%Y-%m-%d %H:%M:%S") if a["resolved"] else "Ongoing",
                "Duration": duration_str,
                "Readings": a["readings_count"],
                "Fault Description": a["reason"]
            })
        st.dataframe(pd.DataFrame(alert_rows).sort_values("Start Time", ascending=False), use_container_width=True, hide_index=True)
    else:
        st.info("No alert events recorded in this session.")

# ==========================================
# PAGE 7: SETTINGS TAB
# ==========================================
elif page == "Settings":
    st.title("Settings")
    st.info("The dashboard reads calibration parameters directly from the single CSV. For production, R0, GF, BF and thermal coefficients must come from the actual calibration records.")
    st.write("Spread threshold: **15%**")
    st.write("Maximum carry-forward for all-invalid section: **2 hours**")

# ==========================================
# AUTO-ADVANCE LOOP CONTROLLER (END OF FILE)
# ==========================================
if st.session_state.playing:
    # Compute elapsed time since last advance
    elapsed = time.time() - st.session_state.last_advance_time
    sleep_time = max(0.01, interval - elapsed)
    
    # Sleep the remaining duration
    time.sleep(sleep_time)
    
    # Advance to the next timestamp
    st.session_state.sim_index = (st.session_state.sim_index + 1) % len(timestamps)
    st.session_state.last_advance_time = time.time()
    
    st.rerun()

st.caption("Demo data only — replace synthetic calibration constants and sensor readings with approved project data before production use.")
