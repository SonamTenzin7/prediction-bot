import streamlit as st
import os
import json
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Import core modules
from core.alert_engine import AlertEngine
from core.paper_trader import PaperTrader

st.set_page_config(
    page_title="Polymarket BTC Predict Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_dotenv()

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background: #f8fafc; color: #1e293b; }
    [data-testid="stHeader"] { background: rgba(255,255,255,0.8); backdrop-filter: blur(10px); }
    .stMetric {
        background-color: #ffffff !important;
        padding: 24px !important;
        border-radius: 12px !important;
        border: 1px solid #e2e8f0 !important;
        box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1) !important;
    }
    .stMetric [data-testid="stMetricValue"] { color: #0f172a !important; }
    .stMetric [data-testid="stMetricLabel"] { color: #64748b !important; }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px; background-color: #f1f5f9; padding: 8px; border-radius: 10px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent; border-radius: 6px;
        color: #64748b; border: none; padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #ffffff !important; color: #0f172a !important;
        box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1) !important;
    }
    .correct-row { background-color: #dcfce7 !important; }
    .wrong-row   { background-color: #fee2e2 !important; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "bot_running" not in st.session_state:
    st.session_state.bot_running = False

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("🤖 Predict Bot Control")
if st.sidebar.button("Start Bot" if not st.session_state.bot_running else "Stop Bot"):
    st.session_state.bot_running = not st.session_state.bot_running

st.sidebar.markdown("---")
trading_mode = os.getenv("TRADING_MODE", "paper")
st.sidebar.info(f"Mode: **{trading_mode.upper()}**")
st.sidebar.caption("Run `python3 bot.py` in a terminal to start the engine.")

# ── Helpers ───────────────────────────────────────────────────────────────────
SIGNAL_LOG = os.getenv("SIGNAL_LOG_FILE", "signal_log.jsonl")
BT_OFFSET  = timedelta(hours=6)   # Bhutan Time = UTC+6

def fmt_bt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + BT_OFFSET
    return dt.strftime("%b %d  %I:%M:%S %p")

def load_signal_log() -> pd.DataFrame:
    """
    Read signal_log.jsonl and join predictions with results on slug.
    Returns a DataFrame with one row per window.
    """
    if not os.path.exists(SIGNAL_LOG):
        return pd.DataFrame()

    preds, results = {}, {}
    with open(SIGNAL_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = r.get("slug", "")
            if r.get("type") == "prediction":
                preds[slug]   = r
            elif r.get("type") == "result":
                results[slug] = r

    rows = []
    for slug, p in preds.items():
        res = results.get(slug, {})
        ws  = p.get("window_start", 0)
        we  = p.get("window_end",   ws + 300)
        pred_dir   = p.get("direction", "")
        actual_dir = res.get("actual", "")
        correct    = (pred_dir == actual_dir) if actual_dir else None

        rows.append({
            "Window (BT)":   fmt_bt(ws) + " – " + (datetime.fromtimestamp(we, tz=timezone.utc) + BT_OFFSET).strftime("%I:%M %p"),
            "_window_start": ws,
            "Predicted":     pred_dir,
            "Confidence":    f"{p.get('confidence', 0):.0%}",
            "_confidence":   p.get("confidence", 0),
            "Source":        p.get("source", ""),
            "BTC Spot":      f"${p.get('btc_spot', 0):,.2f}",
            "Edge":          f"{p.get('edge', 0):+.3f}",
            "Actual":        actual_dir if actual_dir else "⏳ Pending",
            "BTC Open":      f"${res.get('btc_open', 0):,.2f}"  if actual_dir else "—",
            "BTC Close":     f"${res.get('btc_close', 0):,.2f}" if actual_dir else "—",
            "% Move":        f"{res.get('pct_change', 0):+.2f}%" if actual_dir else "—",
            "Result":        ("✅ Correct" if correct else "❌ Wrong") if correct is not None else "⏳",
            "_correct":      correct,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("_window_start", ascending=False).reset_index(drop=True)
    return df


# ── Main ─────────────────────────────────────────────────────────────────────
st.title("Polymarket BTC Predict Bot")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Monitor", "Predictions Report", "Performance", "Leaderboard", "Settings"
])

# ── Tab 1: Monitor ────────────────────────────────────────────────────────────
with tab1:
    col1, col2, col3 = st.columns(3)
    col1.metric("Bot Status", "🟢 Running" if st.session_state.bot_running else "⚫ Idle")
    col2.metric("Trading Mode", trading_mode.upper())
    col3.metric("Alert Lead", f"{os.getenv('ALERT_LEAD_SECONDS', 30)}s before open")

    st.subheader("Upcoming 5-Min Windows (BT)")
    engine  = AlertEngine()
    windows = engine.generate_windows(hours_ahead=1)
    now_utc = datetime.now(timezone.utc)
    window_data = []
    for w in windows[:8]:
        opens_in = (w - now_utc).total_seconds()
        w_bt = w + BT_OFFSET
        window_data.append({
            "Opens (BT)":  w_bt.strftime("%I:%M:%S %p"),
            "Opens in":    f"{int(opens_in//60)}m {int(opens_in%60):02d}s" if opens_in > 0 else "Now",
            "Closes (BT)": (w_bt + timedelta(minutes=5)).strftime("%I:%M %p"),
            "Market Slug": f"btc-updown-5m-{int(w.timestamp())}",
        })
    st.table(pd.DataFrame(window_data))

    st.subheader("Live Bot Logs")
    if os.path.exists("bot.log"):
        with open("bot.log") as f:
            all_log_lines = f.readlines()

        # Simple search bar
        log_search = st.text_input("🔍 Search logs", "", key="log_search", placeholder="Type to filter…")

        # Get last 30 lines, newest first
        recent = [l.strip() for l in reversed(all_log_lines) if l.strip()][:30]
        if log_search:
            recent = [l for l in recent if log_search.lower() in l.lower()]

        if recent:
            level_styles = {
                "ERROR":   ("🔴", "#fef2f2", "#dc2626"),
                "WARNING": ("🟡", "#fffbeb", "#d97706"),
                "INFO":    ("🟢", "#f0fdf4", "#16a34a"),
                "DEBUG":   ("⚪", "#f8fafc", "#94a3b8"),
            }
            rows_html = ""
            for line in recent:
                dot, bg, border = "⚪", "#f8fafc", "#cbd5e1"
                for lvl, (d, b, c) in level_styles.items():
                    if f" {lvl} " in line or f":{lvl}:" in line:
                        dot, bg, border = d, b, c
                        break
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                rows_html += (
                    f'<div style="padding:8px 14px;margin-bottom:3px;border-radius:8px;'
                    f'background:{bg};border-left:4px solid {border};'
                    f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
                    f'font-size:12px;color:#1e293b;line-height:1.6;word-break:break-word;">'
                    f'{dot} {safe}</div>'
                )

            st.markdown(
                f'<div style="max-height:450px;overflow-y:auto;padding:10px;'
                f'background:#f1f5f9;border-radius:12px;border:1px solid #e2e8f0;">'
                f'{rows_html}</div>',
                unsafe_allow_html=True,
            )
            st.caption(f"{len(recent)} lines · newest first")
        else:
            st.info("No matching log lines.")
    else:
        st.info("No bot.log yet. Run `python3 bot.py` in a terminal.")

# ── Tab 2: Predictions Report ─────────────────────────────────────────────────
with tab2:
    st.header("Predictions vs Actual Results")

    if st.button("🔄 Refresh"):
        st.rerun()

    df = load_signal_log()

    if df.empty:
        st.info("No predictions logged yet. The bot writes to `signal_log.jsonl` as it runs.")
    else:
        # ── Summary stats ─────────────────────────────────────────────────────
        resolved = df[df["_correct"].notna()]
        total_pred  = len(df)
        total_res   = len(resolved)
        n_correct   = int(resolved["_correct"].sum()) if total_res else 0
        accuracy    = n_correct / total_res if total_res else 0
        avg_conf    = df["_confidence"].mean()
        copy_count  = df["Source"].str.startswith("copy:").sum()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Predictions", total_pred)
        c2.metric("Resolved",          total_res)
        c3.metric("Correct",           f"{n_correct} / {total_res}")
        c4.metric("Accuracy",          f"{accuracy:.0%}")
        c5.metric("Avg Confidence",    f"{avg_conf:.0%}")

        st.markdown("---")

        # ── Accuracy over time chart ──────────────────────────────────────────
        if total_res >= 2:
            chart_df = resolved.copy().sort_values("_window_start")
            chart_df["cumulative_accuracy"] = (
                chart_df["_correct"].astype(float).cumsum() /
                (range(1, len(chart_df) + 1))
            )
            chart_df["Window"] = chart_df["Window (BT)"].str[:16]
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=chart_df["Window"],
                y=chart_df["cumulative_accuracy"] * 100,
                mode="lines+markers",
                line=dict(color="#3b82f6", width=2),
                marker=dict(size=6),
                name="Cumulative Accuracy %",
            ))
            fig.add_hline(y=50, line_dash="dash", line_color="#94a3b8", annotation_text="50% (coin flip)")
            fig.update_layout(
                title="Cumulative Prediction Accuracy",
                yaxis_title="Accuracy (%)",
                xaxis_title="Window (BT)",
                yaxis=dict(range=[0, 100]),
                height=320,
                margin=dict(l=0, r=0, t=40, b=0),
                plot_bgcolor="#ffffff",
                paper_bgcolor="#ffffff",
            )
            st.plotly_chart(fig, use_container_width=True)

        # ── Confidence distribution ───────────────────────────────────────────
        col_a, col_b = st.columns(2)
        with col_a:
            if total_res >= 2:
                fig2 = px.histogram(
                    resolved,
                    x="_confidence",
                    color="_correct",
                    color_discrete_map={True: "#22c55e", False: "#ef4444"},
                    nbins=10,
                    labels={"_confidence": "Confidence", "_correct": "Correct"},
                    title="Confidence Distribution (Green = Correct)",
                    range_x=[0, 1],
                )
                fig2.update_layout(height=280, margin=dict(l=0, r=0, t=40, b=0),
                                   plot_bgcolor="#ffffff", paper_bgcolor="#ffffff")
                st.plotly_chart(fig2, use_container_width=True)

        with col_b:
            if total_pred >= 1:
                src_counts = df.groupby(
                    df["Source"].apply(lambda s: "🐋 Copy" if s.startswith("copy:") else "⚙️ Model")
                ).size().reset_index(name="Count")
                src_counts.columns = ["Source", "Count"]
                fig3 = px.pie(src_counts, names="Source", values="Count",
                              color_discrete_sequence=["#3b82f6", "#f59e0b"],
                              title="Signal Source Breakdown")
                fig3.update_layout(height=280, margin=dict(l=0, r=0, t=40, b=0),
                                   paper_bgcolor="#ffffff")
                st.plotly_chart(fig3, use_container_width=True)

        # ── Full table ────────────────────────────────────────────────────────
        st.subheader("All Predictions")

        display_cols = [
            "Window (BT)", "Predicted", "Confidence", "Source",
            "BTC Spot", "Edge", "Actual", "BTC Open", "BTC Close", "% Move", "Result"
        ]

        display_df = df[display_cols + ["_correct"]].copy()

        def color_result(row):
            idx = row.name
            correct_val = display_df.loc[idx, "_correct"]
            if correct_val is True:
                return ["background-color: #dcfce7"] * len(row)
            elif correct_val is False:
                return ["background-color: #fee2e2"] * len(row)
            return [""] * len(row)

        styled = (
            display_df[display_cols]
            .style
            .apply(color_result, axis=1)
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # ── Download ──────────────────────────────────────────────────────────
        csv = df[display_cols].to_csv(index=False)
        st.download_button(
            "⬇️ Download CSV",
            data=csv,
            file_name=f"btc_predictions_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

# ── Tab 3: Performance ────────────────────────────────────────────────────────
with tab3:
    st.header("Paper Trading Performance")
    trader = PaperTrader()
    perf   = trader.get_performance()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Balance",      f"${perf['balance']:.2f} USDC")
    c2.metric("Win Rate",     f"{perf['win_rate']:.1%}")
    c3.metric("Total Trades", perf['total_trades'])
    c4.metric("Session PnL",  f"${perf.get('session_pnl', 0):.2f}")

    if os.path.exists("paper_trades.jsonl"):
        trades = []
        with open("paper_trades.jsonl") as f:
            for line in f:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass
        if trades:
            tdf = pd.DataFrame(trades[::-1])
            tdf["time"] = tdf["ts"].apply(lambda t: fmt_bt(t))
            st.dataframe(tdf, use_container_width=True)
    else:
        st.info("No paper trades yet (AUTO_TRADE_ENABLED=false).")

# ── Tab 4: Leaderboard ────────────────────────────────────────────────────────
with tab4:
    st.header("Top BTC Copy Wallets")
    if os.path.exists("wallets_cache.json"):
        with open("wallets_cache.json") as f:
            cache = json.load(f)
        lb = cache.get("leaderboard", [])
        if lb:
            df_lb = pd.DataFrame(lb)
            df_lb["win_rate"] = df_lb["win_rate"].apply(lambda x: f"{x:.1%}")
            df_lb["avg_size"] = df_lb["avg_size"].apply(lambda x: f"${x:.2f}")
            st.dataframe(df_lb, use_container_width=True, hide_index=True)
            saved = cache.get("saved_at", 0)
            st.caption(f"Last scanned: {fmt_bt(saved)} BT")
        else:
            st.warning("No wallets met the qualification threshold yet.")
    else:
        st.info("Wallet scan not yet complete. The scanner runs every 60 minutes.")

# ── Tab 5: Settings ───────────────────────────────────────────────────────────
with tab5:
    st.header("Bot Configuration")
    with st.form("settings_form"):
        st.subheader("Telegram")
        bot_token = st.text_input("Bot Token", value=os.getenv("TELEGRAM_BOT_TOKEN", ""), type="password")
        chat_id   = st.text_input("Chat ID",   value=os.getenv("TELEGRAM_CHAT_ID", ""))

        st.subheader("Signal Timing")
        alert_lead = st.slider("Alert Lead Seconds (before window opens)", 10, 120,
                               int(os.getenv("ALERT_LEAD_SECONDS", 30)))

        st.subheader("Wallet Filters")
        min_win = st.slider("Min Win Rate (%)", 50, 100,
                            int(float(os.getenv("MIN_WIN_RATE", 0.65)) * 100))
        min_trades = st.number_input("Min Trades to Qualify",
                                     value=int(os.getenv("MIN_TRADES_TO_QUALIFY", 5)))

        if st.form_submit_button("Save Settings"):
            env_path = ".env"
            lines_to_update = {
                "TELEGRAM_BOT_TOKEN": bot_token,
                "TELEGRAM_CHAT_ID":   chat_id,
                "ALERT_LEAD_SECONDS": str(alert_lead),
                "MIN_WIN_RATE":       str(min_win / 100),
                "MIN_TRADES_TO_QUALIFY": str(int(min_trades)),
            }
            if os.path.exists(env_path):
                with open(env_path) as f:
                    env_lines = f.readlines()
                updated = set()
                new_lines = []
                for line in env_lines:
                    key = line.split("=")[0].strip()
                    if key in lines_to_update:
                        new_lines.append(f"{key}={lines_to_update[key]}\n")
                        updated.add(key)
                    else:
                        new_lines.append(line)
                for key, val in lines_to_update.items():
                    if key not in updated:
                        new_lines.append(f"{key}={val}\n")
                with open(env_path, "w") as f:
                    f.writelines(new_lines)
            st.success("✅ Saved to .env — restart bot.py for changes to take effect.")

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption("Polymarket BTC Predict Bot  |  Alert-only mode  |  No trades placed automatically")
