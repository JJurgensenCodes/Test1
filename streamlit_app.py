"""
CryptoLounge_Beta — Streamlit web app
======================================
Deploy this file to Streamlit Community Cloud (or run locally with
`streamlit run streamlit_app.py`). It is a web front-end for core.py —
all signal logic lives in core.py so this file and webapp.py never
duplicate model code. Kept as close to the original CryptoBot streamlit_app.py
as possible -- same structure, same tabs, same helper functions -- just
updated for the current core.py engine (Current Call / Final Call split,
the model ensemble, reversion-risk warning, Kalshi odds, etc.).

Requirements (put these in requirements.txt when you upload to Streamlit):
    streamlit
    ccxt
    requests
    python-dotenv

Secrets (optional, for auto Discord alerts without typing a webhook URL
into the page every time): in Streamlit Cloud's "Secrets" panel, add:
    DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import core

APP_NAME = f"CryptoLounge_Beta{core.BETA_VERSION}"

TIMEZONES = {
    "Pacific (PT)": "America/Los_Angeles",
    "Mountain (MT)": "America/Denver",
    "Central (CT)": "America/Chicago",
    "Eastern (ET)": "America/New_York",
    "UTC": "UTC",
    "London (GMT/BST)": "Europe/London",
    "Tokyo (JST)": "Asia/Tokyo",
}

st.set_page_config(page_title=APP_NAME, page_icon="⚡", layout="wide")

# ── Session state ─────────────────────────────────────────────────────────
if "signal_cache" not in st.session_state:
    st.session_state.signal_cache = {}   # (asset, horizon) -> (dict, fetched_at)
if "last_discord_sent" not in st.session_state:
    st.session_state.last_discord_sent = {}  # (asset, horizon, window_label) -> True

CACHE_SECONDS = 3  # don't refetch faster than this even if the user mashes refresh
AUTO_REFRESH_SECONDS = 5


def get_cached_signal(asset: str, horizon: str, force: bool = False) -> dict:
    key = (asset, horizon)
    cached = st.session_state.signal_cache.get(key)
    now_ts = time.time()
    if not force and cached and (now_ts - cached[1]) < CACHE_SECONDS:
        return cached[0]
    sig = core.get_signal(asset, horizon)
    st.session_state.signal_cache[key] = (sig, now_ts)
    return sig


# ── Header + patch notes ─────────────────────────────────────────────────
top_l, top_r = st.columns([3, 1])
with top_l:
    st.title(f"⚡ {APP_NAME}")
    st.caption(f"core.py engine v{core.VERSION}")
with top_r:
    st.metric("Engine version", core.VERSION)

with st.expander(f"📋 Patch notes (latest: v{core.CHANGELOG[0]['version']} — {core.CHANGELOG[0]['date']})",
                  expanded=False):
    for entry in core.CHANGELOG[:8]:
        st.markdown(f"**v{entry['version']}** — {entry['date']}")
        for note in entry["notes"]:
            st.markdown(f"- {note}")
        st.divider()

st.markdown(
    "**CryptoLounge** — Kraken-primary multi-exchange blend, Current Call / "
    "Final Call split (the Final Call locks once and won't change for the "
    "rest of the window), a model ensemble (Confluence + Synthesis + "
    "Polymarket), and a reversion-risk warning that tries to flag a possible "
    "flip before it happens, not just after."
)

# ── Controls row ──────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.6, 1])
with c1:
    asset = st.selectbox("Market", list(core.SYMBOLS.keys()),
                          format_func=lambda a: f"{a} — {core.SYMBOLS[a]['name']}")
with c2:
    tz_label = st.selectbox("Timezone", list(TIMEZONES.keys()))
    tz = ZoneInfo(TIMEZONES[tz_label])
with c3:
    webhook = st.text_input("Discord webhook URL (optional)",
                             value=st.secrets.get("DISCORD_WEBHOOK_URL", "") if hasattr(st, "secrets") else "",
                             type="password",
                             help="Paste a Discord channel webhook URL to enable bet alerts.")
with c4:
    auto_refresh = st.toggle(f"Auto-refresh ({AUTO_REFRESH_SECONDS}s)", value=True)
    auto_discord = st.toggle("Auto-send to Discord", value=False,
                              help="Sends a Discord alert automatically once a window's Final Call locks in "
                                   "(only on STRONG/HIGH/MEDIUM confidence).")

info = core.SYMBOLS[asset]
if not info["has_kalshi"]:
    st.info(f"{info['name']} has no live Kalshi 15-minute strike market — the 15-min tab "
            "below shows a pure price-direction call/probability instead of a strike-gap bet.")

now_local = datetime.now(tz).strftime("%I:%M:%S %p")
st.caption(f"Local time ({tz_label}): {now_local}")


def render_probability_gauge(prob_pct: float, direction: str):
    color = "#3fb950" if direction == "UP" else "#f85149"
    st.markdown(
        f"""
        <div style="background:#161b22;border-radius:10px;padding:14px 18px;margin-bottom:6px;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;">
            <span style="color:#8b949e;font-size:13px;">Probability {direction}</span>
            <span style="color:{color};font-size:28px;font-weight:700;">{prob_pct:.0f}%</span>
          </div>
          <div style="background:#0e1117;border-radius:6px;height:10px;margin-top:8px;overflow:hidden;">
            <div style="background:{color};height:100%;width:{max(0,min(100,prob_pct)):.0f}%;"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_signal_block(sig: dict, tz: ZoneInfo, send_discord_box, webhook: str):
    if sig.get("error"):
        st.error(sig["error"])
        return

    # Current Call = live, updates continuously. Final Call = locked once
    # warmup ends, never changes again that window -- that's the real bet.
    cur = sig.get("current_call") or {"direction": sig["direction"], "confidence": sig["confidence"],
                                       "prob": sig.get("confluence_prob", 50)}
    fin = sig.get("final_call")
    dirn = cur["direction"]
    icon = "🟢" if dirn == "UP" else "🔴"

    col_a, col_b = st.columns([1.3, 1])
    with col_a:
        warmup_tag = "  ⏳ *(still gathering data)*" if cur.get("warmup") else ""
        st.markdown(f"### {icon} {dirn}  ·  {cur['confidence']}{warmup_tag}")
        st.write(sig["bet_size"])
        st.caption(sig["window_label"])
        if fin:
            fin_icon = "🟢" if fin["direction"] == "UP" else "🔴"
            st.markdown(f"🔒 **Final Call:** {fin_icon} {fin['direction']} — {fin['confidence']} "
                        f"({fin['prob']:.0f}%) — locked for the rest of this window")
        else:
            st.caption("🔒 Final Call: not locked yet — still in the first couple minutes of the window")
        if sig.get("price"):
            price_line = f"💰 **${sig['price']:,.2f}**"
            if sig.get("strike") is not None:
                gap = sig["price"] - sig["strike"]
                gap_txt = f"+${gap:,.2f} ABOVE" if gap >= 0 else f"-${abs(gap):,.2f} BELOW"
                price_line += f"  ·  Strike **${sig['strike']:,.2f}**  ({gap_txt})"
            st.markdown(price_line)
        if sig.get("kalshi_odds"):
            ko = sig["kalshi_odds"]
            warn = " ⚠️ *thin payout, already priced in*" if (ko.get("yes_low_value") or ko.get("no_low_value")) else ""
            st.caption(f"Kalshi YES {ko['yes_pct']:.0f}% ({ko['yes_multiplier']:.2f}x)  ·  "
                       f"NO {ko['no_pct']:.0f}% ({ko['no_multiplier']:.2f}x){warn}")
        risk = sig.get("reversion_risk")
        if risk and risk["label"] != "LOW":
            st.warning(f"⚠️ {risk['label']} risk of reversal toward {risk['direction']} "
                       f"({risk['score']:.0f}/100) — {', '.join(risk.get('reasons', [])) or 'see breakdown below'}")
    with col_b:
        render_probability_gauge(cur["prob"], dirn)

    with st.expander("Indicator breakdown (confluence model)"):
        agr = sig.get("indicator_agreement")
        if agr:
            st.caption(f"{agr['agree']}/{agr['total_decisive']} agree (of {agr['total_registered']} total)")
        for label, direction_i, detail in sig.get("votes", []):
            arrow = "⬆️" if direction_i > 0 else "⬇️" if direction_i < 0 else "➖"
            st.markdown(f"{arrow} **{label}** — {detail}")

    if sig.get("synthesis_components") or sig.get("bridge") or sig.get("physics"):
        with st.expander("Model components (Bridge / Physics / Synthesis blend)"):
            if sig.get("synthesis_components"):
                st.json(sig["synthesis_components"])
            if sig.get("bridge_record"):
                st.caption(f"Bridge-alone record: {sig['bridge_record']}")
            if sig.get("physics_record"):
                st.caption(f"Physics-alone record: {sig['physics_record']}")
            if sig.get("poly_record"):
                st.caption(f"Polymarket-alone record: {sig['poly_record']}")

    if send_discord_box and webhook:
        if st.button("📨 Send this call to Discord", key=f"send_{sig['asset']}_{sig['horizon']}"):
            ok = core.send_discord(core.format_signal_for_discord(sig), webhook)
            st.success("Sent!") if ok else st.error("Discord send failed — check the webhook URL.")


tab_overview, tab_guess, tab_1h = st.tabs(["📊 Overview / 15-min", "🔮 Guess", "🕐 1-Hour"])

with tab_overview:
    sig15 = get_cached_signal(asset, "15m", force=not auto_refresh)
    render_signal_block(sig15, tz, send_discord_box=True, webhook=webhook)

    # Auto Discord alert — fires once the FINAL CALL locks in (the real bet,
    # not the live-updating Current Call), only once per window.
    if auto_discord and webhook and not sig15.get("error"):
        key = (asset, "15m", sig15.get("window_label"))
        already = st.session_state.last_discord_sent.get(key)
        fin15 = sig15.get("final_call")
        if not already and fin15 and fin15.get("confidence") in ("STRONG", "HIGH", "MEDIUM"):
            core.send_discord(core.format_signal_for_discord(sig15), webhook)
            st.session_state.last_discord_sent[key] = True
            st.toast("Sent bet alert to Discord", icon="📨")

with tab_guess:
    st.caption("Same confluence-model read as Overview, framed as a quick best-guess "
               "(this mirrors the original bot's Guess tab — the indicator agreement "
               "behind the call, at a glance).")
    sig_guess = get_cached_signal(asset, "15m")
    if sig_guess.get("error"):
        st.error(sig_guess["error"])
    else:
        n_up = sum(1 for _, d, _ in sig_guess["votes"] if d > 0)
        n_down = sum(1 for _, d, _ in sig_guess["votes"] if d < 0)
        n_neut = sum(1 for _, d, _ in sig_guess["votes"] if d == 0)
        cur_guess = sig_guess.get("current_call") or {"direction": sig_guess["direction"], "confidence": sig_guess["confidence"]}
        icon = "⬆️" if cur_guess["direction"] == "UP" else "⬇️"
        st.markdown(f"## {icon} Guess: **{cur_guess['direction']}**")
        st.write(f"Indicators: ▲{n_up}  ▼{n_down}  ─{n_neut}  ·  Confidence: {cur_guess['confidence']}")
        render_probability_gauge(
            cur_guess.get("prob", sig_guess.get("confluence_prob", 50)),
            cur_guess["direction"],
        )

with tab_1h:
    st.caption("1-hour-ahead model read. Uses spot price as the reference threshold "
               "(no Kalshi hourly strike market is queried), so this is a probability "
               "of price being above/below where it is right now, one hour from now.")
    sig1h = get_cached_signal(asset, "1h")
    render_signal_block(sig1h, tz, send_discord_box=True, webhook=webhook)

st.divider()
st.caption(f"{APP_NAME} · Data blended across Kraken (primary)/Coinbase/Gemini/Crypto.com · "
           "Not financial advice.")

if auto_refresh:
    time.sleep(AUTO_REFRESH_SECONDS)
    st.rerun()
