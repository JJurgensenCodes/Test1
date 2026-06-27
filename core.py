"""
@ChillLobsterBot — Kalshi BTC 15-Minute Signal Bot

Fires a signal 1 minute after each Kalshi window opens (:01, :16, :31, :46).
Uses 5 indicators on 5-minute candles for confluence scoring.
Only recommends a bet when 3+ of 5 indicators agree.

Output is local-only: every message is printed to the console and handed to
notify() (this used to be a Telegram bot — that integration is fully removed;
notify() now only prints locally / feeds monitor.py's GUI capture buffer).
Run monitor.py alongside this in a separate terminal for the GUI dashboard.
"""
import os
import sys
import math
import time
import json
import statistics
import threading
import ccxt
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

ET = ZoneInfo("America/Los_Angeles")   # display timezone (PT)
KALSHI_TZ = ZoneInfo("America/New_York")  # Kalshi tickers always use ET


# ── Version / patch notes ────────────────────────────────────────────────────
# Bump VERSION whenever behavior changes. CHANGELOG is newest-first — keep
# entries short, one line per change. App renamed CryptoBot -> CryptoLounge;
# BETA_VERSION resets to V0.1 as this brand's baseline and increments forward
# from here (V0.1.1, V0.1.2, ... bigger changes bump to V0.2, etc.) — same
# pattern as before, just continuing under the new name instead of resetting
# it again next time.
VERSION = "2.9.1"          # core engine/model version (indicators, Bridge, Physics, Synthesis)
BETA_VERSION = "V0.3.1"     # CryptoLounge_Beta_V0.3.1 — overall app/package release name

CHANGELOG: list[dict] = [
    {
        "version": "BetaV0.3.1",
        "date": "2026-06-27",
        "notes": [
            "FIXED: Order Book Imbalance / Whale Trade Flow showing 'no "
            "book data' / 'no trade data' -- detect_whale_activity()'s "
            "no-data fallback was missing the trade_score/book_score keys "
            "entirely, so any time all 4 exchange calls failed (easy to "
            "trigger: that path makes 8 extra network calls every poll), "
            "downstream code got None instead of a neutral 0.0.",
            "Order Book Imbalance now reads from KALSHI'S OWN order book "
            "first (GET /markets/{ticker}/orderbook, public, no auth) -- "
            "one reliable call against the actual market being bet on, "
            "instead of 4 fragile parallel calls across crypto exchanges. "
            "Falls back to the cross-exchange reading only if Kalshi's book "
            "isn't available. (BTCC was suggested as an alternate source "
            "but isn't viable -- it was delisted from the ccxt library "
            "years ago and isn't fetchable through it.)",
        ],
    },
    {
        "version": "BetaV0.3",
        "date": "2026-06-27",
        "notes": [
            "CRITICAL FIX: the Final Call's displayed probability was the "
            "ensemble's P(UP) passed straight through regardless of which "
            "direction was actually picked -- when DOWN was favored (e.g. "
            "P(UP)=29%), it showed '29% DOWN' next to a 71%-UP bar instead "
            "of '71% DOWN'. The direction was always right; only its "
            "confidence number was the OTHER direction's probability. Now "
            "correctly flips to P(DOWN) when DOWN is picked.",
            "FIXED a double-counting bug in the model ensemble: Synthesis "
            "already blends Confluence (30%), Physics (20%), and Bridge's "
            "sub-models (40%+10%) internally -- last update's ensemble was "
            "also re-adding Confluence/Bridge/Physics as separate top-level "
            "votes on top of that, triple-counting some signals. Rebuilt "
            "using this project's own documented Guess-tab ratio (Bet 60% / "
            "Synthesis 40%) as the correct non-double-counted consensus, "
            "with Polymarket blended in as the one genuinely independent "
            "addition. Bridge/Physics remain fully visible with their own "
            "live win-rate benchmarks, just without a separate top-level vote.",
            "Added Order Book Imbalance and Whale Trade Flow as two new "
            "confluence indicators, using detect_whale_activity() (which "
            "already existed in this file but was never actually called "
            "from get_signal() until now) -- standing bid/ask depth and "
            "net large-trade flow across exchanges, both forward-looking "
            "in a way price-history indicators aren't.",
        ],
    },
    {
        "version": "BetaV0.2.1",
        "date": "2026-06-27",
        "notes": [
            "FIXED: bet_recommendation() embedded raw <b> tags that leaked "
            "as literal text wherever they landed in a .textContent field "
            "(the ugly '<b>Tiny bet</b>' look) -- now returns plain text, "
            "with the bet size shown as its own clean badge in the UI.",
            "Added an indicator-agreement count ('X/Y indicators agree') "
            "next to the Final Call and the Indicator Breakdown header, "
            "measured against the Final Call's actual direction.",
        ],
    },
    {
        "version": "BetaV0.2",
        "date": "2026-06-27",
        "notes": [
            "NEW: Synthesis/Bridge/Physics/Polymarket now actually feed into "
            "the Final Call (combine_model_probabilities) instead of being "
            "display-only. Combined into one probability-of-UP, weighted by "
            "each model's OWN live measured win rate -- a model performing "
            "above 50% gets boosted up to 1.5x influence, one performing "
            "below 50% gets reduced to as low as 0.5x. Bridge and Physics "
            "now also get tracked for real from the live app (previously "
            "only the old CLI loop populated their win/loss).",
            "Added a first-minute WARMUP period: the Final Call tracks "
            "freely for the first 60s of every window (still gathering "
            "data), then locks in a solid pick once warmup ends -- that "
            "locked pick is what the rest of the window's stability "
            "logic protects.",
            "Added OBV (On-Balance Volume trend) as a new confluence "
            "indicator -- reads whether volume is flowing WITH price "
            "(confirms a move) or against it (move running on thin "
            "participation), independent of the existing Volume check "
            "which only looks at magnitude.",
            "FIXED: the probability bar and lean badge were computed from "
            "the raw, separately-evolving confluence read while the Final "
            "Call used the stabilized one -- they could show numbers that "
            "looked contradictory (e.g. a 39% DOWN bar next to an UP-favored "
            "call). Both now derive from the exact same stabilized value.",
            "Cleaned up the Bridge/Physics/Synthesis/Polymarket breakdown: "
            "replaced the dot-joined inline text with clean rows, each "
            "tagged with its live win/loss record and its current % "
            "influence on the call, sorted highest-influence first.",
        ],
    },
    {
        "version": "BetaV0.1.2",
        "date": "2026-06-27",
        "notes": [
            "Added a labeled 'Final Call' marking in the UI, backed by a new "
            "stability layer (apply_final_call_stability): the headline pick "
            "no longer flips on every noisy poll. A direction change is only "
            "accepted once (1) a minimum hold time has passed, (2) the "
            "spot-vs-strike gap actually supports it by a margin that scales "
            "with TIME DECAY (a bigger move is required early in the window "
            "when there's still time for it to reverse; a smaller move is "
            "enough near the close), and (3) the recent rolling average of "
            "price is already trending that way, not just the latest tick. "
            "Rejected flips show 'holding previous call' instead of flickering. "
            "W/L tracking and Discord alerts now use this stabilized call.",
            "Removed pywebview entirely -- the desktop build now just opens "
            "your default browser, same as the web version. Simpler and one "
            "less native dependency that could fail to bundle into the exe.",
            "webapp.py (plain `python webapp.py` / run_windows.bat) now also "
            "auto-opens your default browser, matching the desktop build.",
            "build_windows.bat / build_mac.sh now check Python/pip/PyInstaller "
            "exit codes at every step and print a clear error instead of "
            "silently leaving no dist/ folder.",
        ],
    },
    {
        "version": "BetaV0.1.1",
        "date": "2026-06-27",
        "notes": [
            "FIXED: get_signal() never fetched 1-minute candles, so the "
            "'Window Body' indicator — designed to read the current "
            "window's first 1m candle, its strongest Kalshi-specific "
            "signal — was silently always falling back to a weaker 3-candle "
            "5m check instead. Same gap existed in compute_synthesis's "
            "internal confluence call. Both now get real 1m data.",
        ],
    },
    {
        "version": "BetaV0.1",
        "date": "2026-06-27",
        "notes": [
            "RENAMED: CryptoBot -> CryptoLounge (CryptoLounge_Beta_V0.1). "
            "Version numbering resets to V0.1 under the new name and "
            "increments forward from here.",
            "Merged the two parallel prototypes into one engine: ported the "
            "Kalshi multi-host fallback (tries 3 known API hosts, caches "
            "whichever one answers) and added Polymarket's own crowd-priced "
            "15-min BTC odds as an independent benchmark, tracked win/loss "
            "exactly like the Bridge/Physics trackers.",
            "Consolidated to ONE frontend (Flask + static/index.html) split "
            "into webapp.py (deploy to a real webpage) and "
            "desktop_launcher.py (same Flask app, wrapped in a native "
            "window for the Mac/Windows build) so the two can never drift "
            "apart. Retired the separate Streamlit and Tkinter front-ends.",
        ],
    },
    {
        "version": "BetaV0.1.1",
        "date": "2026-06-26",
        "notes": [
            "Added a THIRD front-end, webgui_app.py: a styled HTML/CSS/JS "
            "dashboard (static/index.html) served by a small local Flask "
            "API and opened in its own native-feeling app window via "
            "pywebview (no browser tabs/address bar) — visual style matches "
            "the dark-card dashboard reference (win/loss banner, "
            "probability bar, patch-notes modal, indicator breakdown). "
            "Falls back to opening the system's default browser if no "
            "native webview runtime is found. Packageable into a standalone "
            "exe/.app exactly like desktop_app.py (build_windows_webgui.bat "
            "/ build_mac_webgui.sh).",
            "Added REAL win/loss tracking, wired directly into "
            "core.get_signal(): each 15-min-horizon call is recorded, and "
            "the previous window's call is resolved against current spot "
            "the next time get_signal() is polled after that window rolls "
            "over. This now actually populates the Win/Loss banner and "
            "History table in all three front-ends (previously these "
            "displayed nothing, since get_signal() was read-only).",
            "Added core.DATA_DIR — a single canonical location (next to the "
            "running script, or next to the .exe/.app when frozen by "
            "PyInstaller) for stats/signals/staged files, so all three apps "
            "(and any future ones) read and write the exact same data "
            "regardless of current working directory.",
        ],
    },
    {
        "version": "BetaV0.1",
        "date": "2026-06-26",
        "notes": [
            "NEW PACKAGE: split into core.py (shared engine), streamlit_app.py "
            "(web app, deployable to Streamlit Community Cloud), and "
            "desktop_app.py (Tkinter, PyInstaller-ready for a Windows .exe or "
            "Mac .app). Both front-ends call the same core.get_signal() so "
            "they can never drift out of sync with each other.",
            "Added multi-asset support: BTC, ETH, SOL, XRP, DOGE via a new "
            "SYMBOLS registry. BTC/ETH have real Kalshi 15-min strike markets "
            "(KX{ASSET}15M); SOL/XRP/DOGE get the full confluence/Bridge/"
            "Physics/Synthesis read as a pure price-direction probability "
            "(no Kalshi strike-gap line, since Kalshi doesn't list those).",
            "Added a 1-Hour tab/horizon: reuses the exact same confluence + "
            "Bridge + Physics + Synthesis models on 1h-resampled candles, "
            "using current spot price as the binary threshold (since there's "
            "no Kalshi hourly strike to bet against) — shows both a bet-style "
            "UP/DOWN+confidence call and a raw blended probability.",
            "Added Discord webhook notifications (core.send_discord / "
            "format_signal_for_discord): manual 'send this call' buttons in "
            "both apps, plus an optional auto-send toggle that fires once per "
            "window on STRONG/HIGH/MEDIUM confidence calls.",
            "Added a timezone selector (PT/MT/CT/ET/UTC/London/Tokyo) in both "
            "apps — all window/clock displays respect the selected zone.",
            "Added patch notes banner at the top of both apps, pulled live "
            "from this CHANGELOG so the engine and the UI never go stale "
            "relative to each other.",
            "Per-asset stats/signals files (stats_BTC.json, signals_ETH.json, "
            "etc.) so tracking multiple coins never overwrites a shared file.",
            "Scope note: the original whale-detection scanner, vs-BTC alt "
            "tracker, and the automatic indicator-weight retraining scheduler "
            "were NOT ported into this v0.1 — they still exist in core.py's "
            "underlying functions but aren't wired into get_signal() yet. "
            "Planned for a follow-up Beta version.",
        ],
    },
] + [
    {
        "version": "2.6.0",
        "date": "2026-06-23",
        "notes": [
            "Added market session system: 6 session buttons appear above the "
            "tabs (Asian, London Open, US Pre-Market, US Session, US Close, "
            "Overnight). Each has a historically-grounded vol multiplier that "
            "scales the ATR flat-market gate threshold and Volatility Spike "
            "gate ratio to match the regime's actual activity level. Active "
            "session auto-detects from ET time every 60 seconds; clicking any "
            "button sets a manual override (shown with 🔒) that persists until "
            "cleared.",
            "Added retraining system: a background RetrainScheduler thread runs "
            "every 2 hours, scanning the last 12 hours of signal history. It "
            "computes the recent win rate and adjusts indicator weights — "
            "boosting momentum-type indicators (EMA, ROC, MACD, Trends, QQE) "
            "when win rate is high (trending regime) and boosting "
            "reversion-type indicators (RSI, Bollinger, VWAP, Window Body) "
            "when win rate is low (choppy regime). Weights are bounded to "
            "[0.5, 3.0] to prevent runaway drift. Retrain status shown in the "
            "session bar.",
        ],
    },
    {
        "version": "2.5.2",
        "date": "2026-06-23",
        "notes": [
            "Widened Synthesis NEUTRAL band from 0.45-0.55 to 0.40-0.60: "
            "the quant models (Physics, Merton, fBM) converge toward 0.50 "
            "as time-to-close approaches zero when price is near the strike, "
            "producing spurious NEUTRAL calls in the last 2 minutes. A wider "
            "band requires a genuinely stronger blended signal before calling "
            "ABOVE/BELOW, reducing last-minute NEUTRAL without fabricating "
            "conviction that isn't there.",
        ],
    },
    {
        "version": "2.5.1",
        "date": "2026-06-23",
        "notes": [
            "FIXED Synthesis tab weighting: the confluence model's contribution "
            "was being derived from recommendation()'s discrete tier label "
            "(55/60/64/68%) rather than a genuine probability, compressing it "
            "into a ±0.18 band around 0.50 regardless of actual indicator "
            "agreement strength. This caused the 40%-weight momentum models "
            "(which range freely from ~0 to ~1) to completely dominate the "
            "blend, effectively making the weights non-functional. Fixed by "
            "deriving conf_prob from the raw weighted vote score via a logistic "
            "function — strong consensus now pushes the confluence component "
            "toward the extremes proportionally (range 0.08–0.94 in testing), "
            "so the 30% averaging weight actually carries meaningful influence.",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-06-23",
        "notes": [
            "Added Synthesis tab: combines the confluence model (Bet tab), "
            "Bridge quantitative pricing, and Physics Fokker-Planck into a "
            "single weighted directional probability. Weights per design: "
            "Momentum 40% (Merton Jump-Diffusion + fBM), Averaging 30% "
            "(10-indicator confluence model), Distribution 20% (Physics OU), "
            "Supporting 10% (Bachelier + Time-Changed BM). Shows component "
            "breakdown with individual model probabilities, weighted blend, "
            "and the formula note showing how the final number was computed.",
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-06-22",
        "notes": [
            "FIXED Bridge and Physics overconfidence: both models were "
            "collapsing to 0% or 100% probability for any gap beyond ~$20 "
            "from the strike, because vol estimates from short calm windows "
            "were 5-15% annualized (realistic BTC vol is 50-120%). Added a "
            "40% annualized minimum vol floor for Bridge (affects all four "
            "sub-models), and an ATR-based minimum uncertainty floor for "
            "Physics (0.5× ATR or 0.05% of spot) so the Fokker-Planck "
            "distribution never becomes unrealistically narrow. Models now "
            "produce meaningful 30-70% probabilities at typical Kalshi strike "
            "gaps ($20-$200), not just 0% or 100%.",
            "Added independent Win/Loss trackers for Bridge and Physics models "
            "— each model's ABOVE/BELOW call is recorded every window and "
            "scored against settlement at close, exactly like the QQE-only "
            "and Predict trackers. W/L cards appear at the top of each tab "
            "so you can observe real accuracy over time.",
        ],
    },
    {
        "version": "2.3.2",
        "date": "2026-06-22",
        "notes": [
            "FIXED Physics tab drift calculation: the original μ = (EMA9-EMA21)/300 "
            "was measuring an EMA spread as if it were a price velocity, producing "
            "physically incorrect and often sign-wrong drift estimates that "
            "dominated the mean-reversion term. Replaced with actual realized "
            "price velocity over 25 minutes (5 × 5m candles) in genuine dollars/second, "
            "with exponential momentum decay (half-life ~3 minutes) so the drift "
            "attenuates toward zero over the remaining window rather than applying "
            "at full strength — fixing the 'consistently wrong in the opposite "
            "direction' failure mode during reversals.",
            "FIXED Physics tab damping floor: previous minimum γ = 1e-4 corresponded "
            "to a 2.7-hour mean-reversion timescale, far too slow for 15-minute "
            "windows. New bounds: 1/900 (~15-min) to 1/120 (~2-min), default 1/300 (~5-min).",
        ],
    },
    {
        "version": "2.3.1",
        "date": "2026-06-22",
        "notes": [
            "Updated MACD Anchor to compound across the last 3 complete "
            "15-minute windows: computes the MACD line independently at "
            "the end of each of the last 3 rounds (each round = 3 five-"
            "minute candles), then averages the three readings before "
            "classifying. This gives a smoothed positional estimate "
            "rather than a single snapshot that can be distorted by one "
            "candle's anomaly. Falls back to single-snapshot if fewer "
            "than 84 candles are available. Guess tab now shows "
            "per-round breakdown (R1/R2/R3) alongside the averaged values.",
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-06-22",
        "notes": [
            "Added Physics tab: Langevin/Fokker-Planck dynamical systems "
            "model treating BTC price as a driven, damped oscillator. "
            "Derives the most probable settlement price S*(T) via the "
            "Ornstein-Uhlenbeck minimum-action path, and P(S_T>K) from "
            "the OU process's Gaussian stationary distribution. Also "
            "decomposes the system into KE (momentum) and PE (mean-"
            "reversion pull) as a Hamiltonian, with regime detection "
            "(MOMENTUM / MEAN-REVERTING / BALANCED). Jump correction "
            "from whale detector flow scores. Updates every 10s.",
            "FIXED: Whale Detector, MACD Anchor (Guess tab), Bridge, "
            "and vs-BTC alt-asset scanner all had the same silent-failure "
            "bug: _ensure_exchanges() set self._exchanges={} on any "
            "connection error, which is not None, so the guard "
            "'if not None: return' permanently blocked all retries after "
            "even one transient failure. Fixed to use None on failure so "
            "every subsequent poll retries the connection.",
            "FIXED: Overview tab now has a vertical scrollbar wrapping "
            "all content below the stat cards (QQE strip, Reversion "
            "Detector, Open Bet, Live card + whale detector, Recent "
            "Signals) so nothing is clipped on smaller screens.",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-06-22",
        "notes": [
            "Added Bridge tab: four-model quantitative pricing blended "
            "equally into a single P(settlement > strike) probability. "
            "Models: (1) Merton Jump-Diffusion — adds Poisson jump process "
            "for BTC's sudden price gaps; (2) Fractional BM — Hurst-driven "
            "momentum persistence at short horizons; (3) Bachelier — "
            "arithmetic BM, more appropriate for fixed-dollar strike "
            "threshold than lognormal; (4) Time-Changed/Subordinated BM — "
            "volume-scaled effective time, capturing that high-activity "
            "minutes age faster than quiet ones. IV extracted directly from "
            "Kalshi's YES mid-price when available (0.02–0.98 range), "
            "falling back to realized vol from 5m candles. Hurst exponent "
            "estimated live via variance-ratio from the return series. "
            "Model spread drives confidence rating. Updates every 10s.",
            "Added math and statistics to stdlib imports (were missing, "
            "used elsewhere in the codebase but never imported explicitly)",
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-06-21",
        "notes": [
            "Added a window-open instant signal: previously 'CURRENT OPEN "
            "BET' stayed empty for the first ~1-6 minutes of every round "
            "(nothing wrote to pending until the :01 main signal or the "
            "9-min EARLY READ). Now fires immediately at window open using "
            "whatever data is available — necessarily a rougher read since "
            "this window's own candles barely exist yet — and gets safely "
            "superseded by the EARLY READ and :01 signal a few minutes "
            "later via the same window-matched override pattern, with no "
            "risk of duplicate history rows",
            "GUI tags this early read distinctly ('🕐 early read', not an "
            "error/warning state) so it's never confused with a fully "
            "analyzed call, and the tag disappears once superseded",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-06-21",
        "notes": [
            "FIXED a real bug behind bets staying labeled 'open' indefinitely: "
            "Stats.resolve() matched signal-history rows by 'first unresolved "
            "scanning backwards' instead of by window — if even one signal "
            "ever got orphaned (skipped by an exception, a restart, etc.), "
            "every later resolve() call kept clearing only the NEWEST "
            "unresolved row and never revisited the stuck older one. Now "
            "matches by window AND sweeps any orphaned older unresolved rows "
            "in the same pass, so backlogs can't silently accumulate forever",
            "Added Stats.stuck_unresolved_count() — diagnostic only, reports "
            "how many signals are still unresolved without touching them "
            "(an existing pre-fix backlog needs real historical settlement "
            "prices to resolve correctly, which aren't recoverable after the "
            "fact, so old stuck entries are reported, not auto-resolved)",
            "Rebuilt the Bet and Predict tabs from plain scrolling text into "
            "structured panels matching the Guess tab's layout: a headline "
            "signal (same 22pt bold + icon styling as the Confluence Model "
            "standard) on top, condensed detail below — same information as "
            "before, fewer words",
        ],
    },
    {
        "version": "1.9.1",
        "date": "2026-06-21",
        "notes": [
            "FIXED: missed rename — the signal message headline 'ChillPlaysBTC' "
            "(a different string from '@ChillPlayzBot', not caught by the "
            "earlier rename pass) is now 'ChillLobsterBTC', in both the "
            "on-demand /bet message and the main auto-fired signal message",
        ],
    },
    {
        "version": "1.9.0",
        "date": "2026-06-21",
        "notes": [
            "FIXED: 'No open bet' showing even while a window was live the "
            "entire round. Root cause: if anything threw inside the main "
            "loop's signal-firing block (network blip, exchange error, "
            "etc.) AFTER resolve() cleared the previous pending but BEFORE "
            "record_signal() set a new one, the exception was caught and "
            "logged but pending stayed None for the rest of that window. "
            "Added a fallback: on any caught exception, if pending is still "
            "empty, record a clearly-tagged DEGRADED signal (repeats the "
            "last known direction, or UP if there's no history at all) so "
            "the GUI always reflects an active call during a live window",
            "Standardized every direction-signal display in the GUI (the "
            "Overview 'CURRENT OPEN BET' display included) to match the "
            "Confluence Model panel's styling on the Guess tab: 22pt bold "
            "with a 🟢/🔴/⚪ icon prefix, consistent color logic everywhere",
        ],
    },
    {
        "version": "1.8.0",
        "date": "2026-06-21",
        "notes": [
            "Renamed every @ChillPlayzBot reference to @ChillLobsterBot "
            "throughout bot.py's messages and console output",
            "Added a Logo tab — the FIRST tab, opens by default — showing "
            "logo.png centered and resized to fit. Requires Pillow "
            "(pip install pillow) for resizing; falls back gracefully to "
            "tkinter's native PhotoImage (no resize) if Pillow isn't "
            "installed, and to a 'file not found' notice if logo.png isn't "
            "present in the same folder as bot.py/monitor.py",
        ],
    },
    {
        "version": "1.7.1",
        "date": "2026-06-21",
        "notes": [
            "Reordered tabs: Overview, Bet, Predict, 9-min Bet, 2-min, Guess, "
            "vs BTC, History, QQE, Patch Notes",
            "Renamed '6-min Bet' tab to '9-min Bet' — the EARLY READ has "
            "actually fired at 9 minutes before close (MID_OFFSET=540s) for "
            "a while now, the tab label was just stale",
            "Guess tab content replaced entirely: now shows the standalone "
            "10m/15m MACD Anchor read plus the side-by-side Confluence-vs-"
            "MACD comparison, instead of the old /guess command text output",
            "/guess removed from the auto-refresh command cycle (nothing "
            "displays its output anymore, so it no longer runs)",
        ],
    },
    {
        "version": "1.7.0",
        "date": "2026-06-21",
        "notes": [
            "Reversion Detector is now an actual confidence-downgrade GATE, not "
            "just display text — flagged price-reversion or signal-instability "
            "now auto-downgrades a call to LEAN, in both the auto-fired signal "
            "and /bet, same pattern as the existing ATR/circuit-breaker filters",
            "Added Volatility Spike Detector (purely relative to the bot's own "
            "recent ATR history, no fixed clock hours) — also wired as a real "
            "downgrade gate, addressing trend-following getting caught chasing "
            "reversals during abnormally high-volatility stretches",
            "Added Methodology tab: side-by-side comparison of the existing "
            "confluence model vs the new 10m/15m MACD Anchor model, with an "
            "agree/disagree strip",
        ],
    },
    {
        "version": "1.6.0",
        "date": "2026-06-21",
        "notes": [
            "Added Reversion Detector indicator to the Overview tab — price "
            "mean-reversion and signal-flip/choppiness, both previously only "
            "visible buried in /bet text output, now their own dedicated strip",
            "Added 10m/15m MACD Anchor methodology (separate from the existing "
            "multi-indicator confluence model) — classifies BULL/BEAR/EARLY "
            "TELL/WAIT off the MACD line's sign (stable trend signal), using "
            "the histogram for confirmation strength rather than as the "
            "primary signal, since pure histogram-sign can flip on momentary "
            "deceleration even mid-trend",
        ],
    },
    {
        "version": "1.5.1",
        "date": "2026-06-21",
        "notes": [
            "Whale Detector trade lookback widened from 60s to 180s — a large "
            "trade could fully age out of the old 60s window before the next "
            "poll, especially if it helped form a 5m/15m candle",
            "Whale Detector's degenerate-tape guard loosened from 3x to 2.2x "
            "median — 3x was also suppressing real moves driven by several "
            "moderately-elevated trades clustered together (no single huge "
            "outlier), not just the uniform/quiet tapes it was meant to block",
        ],
    },
    {
        "version": "1.5.0",
        "date": "2026-06-21",
        "notes": [
            "Signal history cap raised from 20 to 96 (a full day at 4 windows/hour)",
            "Added scrollbar + mouse-wheel support to Overview's Recent Signals table",
            "Added new History tab: full signal history in a compact one-line-per-"
            "signal view, grouped by day, with its own scrollbar",
        ],
    },
    {
        "version": "1.4.0",
        "date": "2026-06-21",
        "notes": [
            "Whale Detector now rendered in the GUI: dark/green/red whale icon "
            "on the Overview tab, with expected-drift % and confidence",
            "Added 'vs BTC' tab: SOL/XRP/HYPE/BNB/ETH/DOGE momentum vs BTC "
            "(FOLLOWING/REVERSED/DELAYED), each asset's Kalshi strike + YES price",
            "Record Reset button wired up in the header (confirm dialog, wipes "
            "main + QQE + predict W/L, streaks, pending bets, signal history)",
            "Version number shown in header; added Patch Notes tab",
            "Predict W/L card added to Overview; QQE-vs-overall-call agreement "
            "strip added to Overview (separate from the QQE-only W/L tracker)",
        ],
    },
    {
        "version": "1.3.0",
        "date": "2026-06-21",
        "notes": [
            "Added Reversion Detector (price mean-reversion + bot signal-flip detection)",
            "Removed remaining Telegram naming — send_telegram() renamed to notify()",
            "Added /predict-specific win/loss tracking, separate from main + QQE stats",
            "QQE block visually separated from the overall bet call in /bet and the main fired signal",
            "Added Whale Detector backend (large-trade scan + order-book imbalance, "
            "blended across exchanges) and cross-asset trend backend (SOL/XRP/HYPE/BNB/ETH/DOGE)",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-06-20",
        "notes": [
            "Added next-window strike estimate (real Kalshi value when listed, "
            "else at-the-money + damped EMA drift forecast), refreshed every 10s",
            "Settlement capture now pulls its 60-tick BRTI average 1s before the "
            "true window boundary, then resyncs — :01/:16/:31/:46 firing unchanged",
        ],
    },
]


# ── Exchange weighting ───────────────────────────────────────────────────────
# Coinbase is primary. The blended OHLCV feed (see fetch_blended_ohlcv below)
# pulls candles from every reachable exchange and combines them into one
# weighted-average synthetic series used everywhere in the bot. If an exchange
# is unreachable on a given call, its weight is dropped and the rest are
# re-normalized so the blend always sums to 100%.
EXCHANGE_NAMES_WEIGHTS = [
    ("coinbase",  0.40),
    ("kraken",    0.30),
    ("gemini",    0.15),
    ("cryptocom", 0.15),
]
PRIMARY_EXCHANGE = "coinbase"

# ── Multi-asset registry (V0.1) ──────────────────────────────────────────────
# Every "BTC-only" assumption in the original single-asset bot gets routed
# through this table now. has_kalshi=True means the asset has a real, live
# Kalshi 15-minute strike/YES-price market (KX{CODE}15M) we can fetch and bet
# against; assets with has_kalshi=False still get the full confluence /
# Bridge / Physics / Synthesis read, just without a strike-gap line (pure
# price-direction probability instead of "price vs Kalshi strike").
SYMBOLS: dict[str, dict] = {
    "BTC":  {"ccxt": "BTC/USD",  "kalshi_code": "BTC",  "name": "Bitcoin",  "has_kalshi": True},
    "ETH":  {"ccxt": "ETH/USD",  "kalshi_code": "ETH",  "name": "Ethereum", "has_kalshi": True},
    "SOL":  {"ccxt": "SOL/USD",  "kalshi_code": "SOL",  "name": "Solana",   "has_kalshi": False},
    "XRP":  {"ccxt": "XRP/USD",  "kalshi_code": "XRP",  "name": "XRP",      "has_kalshi": False},
    "DOGE": {"ccxt": "DOGE/USD", "kalshi_code": "DOGE", "name": "Dogecoin", "has_kalshi": False},
}
DEFAULT_ASSET = "BTC"

# Legacy alias so any old call sites that still build the BTC-only weight
# table keep working unchanged.
EXCHANGE_WEIGHTS = [(n, "BTC/USD", w) for n, w in EXCHANGE_NAMES_WEIGHTS]


# ── Discord notifications ────────────────────────────────────────────────────
def send_discord(message: str, webhook_url: str | None) -> bool:
    """POST a plain-text message to a Discord webhook. Strips the bot's
    <b>/<i>/<code> HTML-ish tags first since Discord doesn't render them.
    Returns True on success, False on any failure (never raises — a failed
    notification should never crash a signal cycle)."""
    if not webhook_url:
        return False
    try:
        text = strip_tags(message)
        if len(text) > 1900:  # Discord hard-caps a message at 2000 chars
            text = text[:1900] + "\n…(truncated)"
        resp = requests.post(webhook_url, json={"content": text}, timeout=8)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[discord] send failed: {e}")
        return False


# ── Output ───────────────────────────────────────────────────────────────────
# notify() is a holdover name — it now just prints to console. Kept as
# a function (rather than inlining print everywhere) so monitor.py can swap it
# out and capture the text for the GUI, exactly like it did with Telegram.

def now() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p PT")


def strip_tags(message: str) -> str:
    import re as _re
    return _re.sub(r"<[^>]+>", "", message)


def notify(message: str) -> None:
    """Local notification — prints the message to console (and is captured by
    monitor.py's GUI). No network call; this used to send to Telegram, that
    integration has been fully removed."""
    print(f"[{now()}] {strip_tags(message)}")


# ── Exchange ──────────────────────────────────────────────────────────────────

def build_exchanges(asset_code: str = DEFAULT_ASSET) -> tuple[dict, str]:
    """Connect to every exchange for the given asset that's reachable.
    Returns ({name: (ccxt_instance, weight)}, symbol). Re-normalizes weights
    over whichever exchanges actually connected. Exits only if NONE connect."""
    info = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])
    symbol = info["ccxt"]
    live: dict[str, tuple] = {}
    for name, weight in EXCHANGE_NAMES_WEIGHTS:
        try:
            ex = getattr(ccxt, name)()
            ex.fetch_ohlcv(symbol, timeframe="5m", limit=1)
            live[name] = (ex, weight)
            print(f"[{now()}] Exchange connected: {name} (weight {weight:.0%})")
        except Exception as e:
            print(f"[{now()}] {name} unavailable for {symbol}: {e}")
    if not live:
        print(f"[{now()}] No exchange available for {symbol} — exiting")
        sys.exit(1)
    total_w = sum(w for _, w in live.values())
    live = {n: (ex, w / total_w) for n, (ex, w) in live.items()}
    if len(live) < len(EXCHANGE_NAMES_WEIGHTS):
        missing = [n for n, _ in EXCHANGE_NAMES_WEIGHTS if n not in live]
        print(f"[{now()}] Re-normalized weights (missing: {', '.join(missing)}): "
              + ", ".join(f"{n} {w:.0%}" for n, (_, w) in live.items()))
    return live, symbol


def fetch_blended_ohlcv(exchanges: dict, symbol: str, timeframe: str, limit: int) -> list:
    """Fetch OHLCV from every live exchange and combine into one weighted-
    average synthetic series (open/high/low/close/volume all blended by
    weight, aligned on timestamp). Falls back to PRIMARY_EXCHANGE alone if
    blending isn't possible (e.g. only one exchange reachable, or a mismatch
    in candle counts/timestamps across exchanges)."""
    per_exchange: dict[str, tuple[list, float]] = {}
    for name, (ex, weight) in exchanges.items():
        try:
            candles = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if candles:
                per_exchange[name] = (candles, weight)
        except Exception as e:
            print(f"[{now()}] {name} fetch_ohlcv({timeframe}) failed: {e}")

    if not per_exchange:
        raise RuntimeError(f"No exchange returned {timeframe} candles")

    if len(per_exchange) == 1:
        return next(iter(per_exchange.values()))[0]

    # Re-normalize weights over exchanges that actually returned data this call.
    total_w = sum(w for _, w in per_exchange.values())
    per_exchange = {n: (c, w / total_w) for n, (c, w) in per_exchange.items()}

    # Align by timestamp using the shortest series as the reference — every
    # exchange's candles must share that timestamp or the row is skipped.
    name_ref = min(per_exchange, key=lambda n: len(per_exchange[n][0]))
    ref_candles = per_exchange[name_ref][0]

    blended = []
    for ref_row in ref_candles:
        ts = ref_row[0]
        rows_at_ts = []
        weights_at_ts = []
        for name, (candles, weight) in per_exchange.items():
            match = next((c for c in candles if c[0] == ts), None)
            if match is not None:
                rows_at_ts.append(match)
                weights_at_ts.append(weight)
        if not rows_at_ts:
            continue
        w_total = sum(weights_at_ts)
        w_norm = [w / w_total for w in weights_at_ts]
        o = sum(r[1] * w for r, w in zip(rows_at_ts, w_norm))
        h = sum(r[2] * w for r, w in zip(rows_at_ts, w_norm))
        l = sum(r[3] * w for r, w in zip(rows_at_ts, w_norm))
        c = sum(r[4] * w for r, w in zip(rows_at_ts, w_norm))
        v = sum(r[5] for r in rows_at_ts)  # volume: sum, not weighted-avg
        blended.append([ts, o, h, l, c, v])

    if not blended:
        # Timestamps didn't line up across exchanges — fall back to primary
        # if it's in the mix, else whichever exchange returned the most data.
        if PRIMARY_EXCHANGE in per_exchange:
            print(f"[{now()}] Blend alignment failed for {timeframe} — using {PRIMARY_EXCHANGE} alone")
            return per_exchange[PRIMARY_EXCHANGE][0]
        fallback_name = max(per_exchange, key=lambda n: len(per_exchange[n][0]))
        print(f"[{now()}] Blend alignment failed for {timeframe} — using {fallback_name} alone")
        return per_exchange[fallback_name][0]

    return blended


_CF_BRTI = {  # CF Benchmarks real-time index tickers that actually exist
    "BTC": "BRTI",
    "ETH": "ETRI",
}


def get_cf_price(fallback: float, asset_code: str = DEFAULT_ASSET) -> float:
    """
    Fetch the current real-time index price Kalshi settles against for this
    asset (CF Benchmarks BRTI for BTC, ETRI for ETH). For assets with no CF
    Benchmarks index (SOL/XRP/DOGE), skips straight to the exchange spot
    ticker. Falls back to the supplied fallback price (the blended
    multi-exchange close) if everything else fails.
    """
    index = _CF_BRTI.get(asset_code)
    if index:
        try:
            resp = requests.get(
                f"https://www.cfbenchmarks.com/api/v1/indices/{index}/ticks",
                params={"n": 1},
                timeout=5,
            )
            ticks = resp.json().get("result", [])
            if ticks:
                price = float(ticks[0]["v"])
                print(f"[{now()}] CF Benchmarks {index}: ${price:,.2f}")
                return price
        except Exception:
            pass
    # Exchange real-time ticker (primary exchange, always fresh)
    pair = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])["ccxt"].replace("/", "-")
    try:
        resp = requests.get(f"https://api.coinbase.com/v2/prices/{pair}/spot", timeout=5)
        price = float(resp.json()["data"]["amount"])
        print(f"[{now()}] Coinbase ticker: ${price:,.2f} ({asset_code} fallback)")
        return price
    except Exception:
        pass
    print(f"[{now()}] Using blended exchange close as price fallback: ${fallback:,.2f}")
    return fallback


def get_cf_price_avg(fallback: float, n: int = 60, lead_seconds: float = 0.0,
                      asset_code: str = DEFAULT_ASSET) -> float:
    """
    Replicates Kalshi's own settlement math: at expiration, Kalshi averages
    the last 60 one-second index ticks. Same index selection as get_cf_price()
    above (BRTI/ETRI where available, else falls straight back).
    """
    index = _CF_BRTI.get(asset_code)
    if index:
        try:
            resp = requests.get(
                f"https://www.cfbenchmarks.com/api/v1/indices/{index}/ticks",
                params={"n": n},
                timeout=5,
            )
            ticks = resp.json().get("result", [])
            values = [float(t["v"]) for t in ticks if "v" in t]
            if values:
                avg_price = sum(values) / len(values)
                lead_note = f" [captured {lead_seconds:.0f}s early]" if lead_seconds else ""
                print(f"[{now()}] CF Benchmarks {index} 1-min avg: ${avg_price:,.2f} "
                      f"({len(values)} ticks, latest ${values[0]:,.2f}){lead_note}")
                return avg_price
        except Exception as e:
            print(f"[{now()}] {index} 1-min avg fetch failed: {e} — falling back to single tick")
    return get_cf_price(fallback, asset_code)


# ── Indicators ────────────────────────────────────────────────────────────────

def ema_series(closes: list[float], period: int) -> list[float]:
    """Full EMA series (same length, first period-1 entries are None)."""
    k = 2.0 / (period + 1)
    out: list = [None] * (period - 1)
    out.append(sum(closes[:period]) / period)
    for price in closes[period:]:
        out.append(price * k + out[-1] * (1 - k))
    return out


def ema_last(closes: list[float], period: int) -> float:
    return ema_series(closes, period)[-1]


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 2:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + avg_g / avg_l)


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI as a clean series. Length = len(closes) - period."""
    if len(closes) < period + 1:
        return []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def _val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1 + ag / al)

    out = [_val(avg_g, avg_l)]
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(_val(avg_g, avg_l))
    return out


def _ema_clean(values: list[float], period: int) -> list[float]:
    """EMA over a clean list, seeded with SMA. Returns values from the seed onward."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def qqe(closes: list[float], rsi_period: int = 14,
        smoothing: int = 5, qqe_factor: float = 4.236):
    """
    QQE (Quantitative Qualitative Estimation) — a smoothed-RSI trend indicator.

    It takes RSI, smooths it (RsiMa), then builds an ATR-of-RSI trailing stop.
    When RsiMa is above the trailing line the trend is bullish, below = bearish.

    Returns (trend, rsi_ma, trailing_line):
      trend  — +1 bullish, -1 bearish, 0 insufficient data
      rsi_ma — current smoothed RSI value (or None)
      trailing_line — current QQE trailing stop level (or None)
    """
    wilders = rsi_period * 2 - 1
    rsi_s = _rsi_series(closes, rsi_period)
    if len(rsi_s) < smoothing + 2 * wilders + 4:
        return 0, None, None

    rsi_ma = _ema_clean(rsi_s, smoothing)
    atr_rsi = [abs(rsi_ma[i] - rsi_ma[i - 1]) for i in range(1, len(rsi_ma))]
    ma_atr = _ema_clean(atr_rsi, wilders)
    dar = [x * qqe_factor for x in _ema_clean(ma_atr, wilders)]
    if len(dar) < 2:
        return 0, None, None

    rsi_ma_aligned = rsi_ma[-len(dar):]

    longband = shortband = 0.0
    prev_long = prev_short = 0.0
    trend = 1
    for i in range(len(dar)):
        rsi_now = rsi_ma_aligned[i]
        newlong = rsi_now - dar[i]
        newshort = rsi_now + dar[i]
        if i == 0:
            longband, shortband = newlong, newshort
            prev_long, prev_short = longband, shortband
            continue
        rsi_prev = rsi_ma_aligned[i - 1]
        if rsi_prev > prev_long and rsi_now > prev_long:
            longband = max(prev_long, newlong)
        else:
            longband = newlong
        if rsi_prev < prev_short and rsi_now < prev_short:
            shortband = min(prev_short, newshort)
        else:
            shortband = newshort
        if rsi_prev <= prev_short and rsi_now > prev_short:
            trend = 1
        elif rsi_prev >= prev_long and rsi_now < prev_long:
            trend = -1
        prev_long, prev_short = longband, shortband

    trailing_line = longband if trend == 1 else shortband
    return trend, rsi_ma_aligned[-1], trailing_line


def macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9):
    """Returns (macd_val, signal_val, histogram) or None if not enough data."""
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_s, slow_s) if f is not None and s is not None]
    if len(line) < sig + 1:
        return None
    sig_s = ema_series(line, sig)
    m = line[-1]
    sv = sig_s[-1]
    return m, sv, m - sv


# ── MACD Anchor methodology (10m/15m histogram-led) ─────────────────────────
# A second, separate prediction methodology alongside analyze()'s multi-
# indicator confluence model. This one is deliberately simple by design:
# the 10-minute and 15-minute MACD histogram are the PRIMARY signal, not one
# vote among many — confluence with other indicators is intentionally NOT
# used here, since the whole point of this methodology (per prior live-
# trading results) is that anchoring on a single dominant timeframe-pair
# read outperformed blending many indicators together, which is more prone
# to short-term noise from indicators disagreeing.
#
# Four states, matching the original "10m/15m MACD anchor panel" concept:
#   BULL       — both 10m and 15m histograms positive and 15m confirms 10m
#   BEAR       — both 10m and 15m histograms negative and 15m confirms 10m
#   EARLY TELL — 10m has flipped but 15m hasn't caught up yet (leading signal,
#                lower confidence — the 10m timeframe is quicker to turn)
#   WAIT       — 10m and 15m histograms disagree in a way that isn't a clean
#                EARLY TELL (e.g. both near zero, or actively conflicting)

def resample_ohlcv(ohlcv_5m: list, group_size: int) -> list:
    """
    Resample a 5-minute OHLCV series into a coarser timeframe by grouping
    `group_size` consecutive 5m candles into one synthetic candle
    (group_size=2 → 10m, group_size=3 → 15m). Standard OHLC resampling:
    open = first candle's open, close = last candle's close,
    high = max of highs, low = min of lows, volume = sum.

    Only full groups are kept — a partial trailing group (fewer than
    group_size candles) is dropped rather than producing a short/incomplete
    synthetic candle that would understate true range and skew MACD.
    Input/output rows are both [ts, o, h, l, c, v], oldest-first, matching
    fetch_blended_ohlcv()'s row shape throughout the rest of the file.
    """
    if group_size <= 1:
        return list(ohlcv_5m)
    n_groups = len(ohlcv_5m) // group_size
    if n_groups == 0:
        return []
    # Keep only the most recent n_groups*group_size candles so we group from
    # the END (most recent data), not the start — matters when the input
    # isn't an exact multiple of group_size.
    trimmed = ohlcv_5m[-(n_groups * group_size):]
    out = []
    for i in range(0, len(trimmed), group_size):
        chunk = trimmed[i:i + group_size]
        ts = chunk[0][0]
        o = chunk[0][1]
        h = max(c[2] for c in chunk)
        l = min(c[3] for c in chunk)
        c_close = chunk[-1][4]
        v = sum(c[5] for c in chunk)
        out.append([ts, o, h, l, c_close, v])
    return out


def macd_anchor_read(ohlcv_5m: list) -> dict:
    """
    Computes the 10m/15m MACD anchor read from a 5m candle series,
    compounded across the last 3 complete 15-minute windows.

    Each 15-min Kalshi window = 3 five-minute candles. This function
    computes the MACD line independently at the end of each of the last
    3 complete windows, then averages the three readings before
    classifying — giving a smoothed, multi-round positional estimate
    rather than a single noisy snapshot that can be distorted by a
    single candle's anomaly.

    Compounding logic:
      round_1_end = closes[:n-6]   (3 windows back)
      round_2_end = closes[:n-3]   (2 windows back)
      round_3_end = closes[:]      (current/most recent)
      avg_macd_10m = (r1_macd + r2_macd + r3_macd) / 3
      avg_macd_15m = (r1_macd + r2_macd + r3_macd) / 3
    Classification uses the averaged MACD LINE sign (same
    BULL/BEAR/EARLY TELL/WAIT logic as before, validated as more stable
    than histogram-sign-only classification).

    Falls back to single-snapshot read if not enough candles for 3 rounds.

    Returns:
      {"state": "BULL"|"BEAR"|"EARLY TELL"|"WAIT",
       "direction": "UP"|"DOWN"|None,
       "macd_10m": float|None,      # averaged across 3 rounds
       "macd_15m": float|None,      # averaged across 3 rounds
       "hist_10m": float|None,      # most recent round only
       "hist_15m": float|None,      # most recent round only
       "prev_macd_10m": float|None, # one candle back from most recent round
       "round_readings": list,      # per-round macd_10m/macd_15m for display
       "note": str}
    """
    CANDLES_PER_ROUND = 3   # 3 five-minute candles = one 15-min window

    def _single_read(candles_5m: list) -> dict | None:
        """Compute MACD anchor from one candle slice. Returns None if
        not enough data for either timeframe's MACD."""
        ohlcv_10m = resample_ohlcv(candles_5m, 2)
        ohlcv_15m_s = resample_ohlcv(candles_5m, 3)
        closes_10m = [c[4] for c in ohlcv_10m]
        closes_15m = [c[4] for c in ohlcv_15m_s]
        m10 = macd(closes_10m, fast=12, slow=26, sig=9)
        m15 = macd(closes_15m, fast=12, slow=26, sig=9)
        if m10 is None or m15 is None:
            return None
        return {
            "macd_10m": m10[0], "hist_10m": m10[2],
            "macd_15m": m15[0], "hist_15m": m15[2],
        }

    # Try to compute 3-round compounded average.
    n = len(ohlcv_5m)
    n_rounds = 3
    min_needed = n_rounds * CANDLES_PER_ROUND + 75  # 75 for MACD warmup
    reads = []
    if n >= min_needed:
        for r in range(n_rounds, 0, -1):
            # Slice up to end of this round (r rounds back from present)
            end_idx = n - (r - 1) * CANDLES_PER_ROUND
            read = _single_read(ohlcv_5m[:end_idx])
            if read is not None:
                reads.append(read)

    # Fall back to single snapshot if we don't have 3 valid reads
    if len(reads) == 0:
        read = _single_read(ohlcv_5m)
        if read is not None:
            reads = [read]

    if not reads:
        return {"state": "WAIT", "direction": None,
                "macd_10m": None, "macd_15m": None,
                "hist_10m": None, "hist_15m": None,
                "prev_macd_10m": None, "round_readings": [],
                "note": "insufficient data for 10m/15m MACD"}

    # Average MACD lines across available rounds (compounded smoothing)
    avg_macd_10m = sum(r["macd_10m"] for r in reads) / len(reads)
    avg_macd_15m = sum(r["macd_15m"] for r in reads) / len(reads)
    # Use most recent round's histogram for display/confirmation
    hist_10m = reads[-1]["hist_10m"]
    hist_15m = reads[-1]["hist_15m"]

    # Prev read for EARLY TELL detection: second-most-recent round's MACD
    prev_macd_10m = reads[-2]["macd_10m"] if len(reads) >= 2 else None

    # Classification on averaged MACD lines (same logic, more stable input)
    bull_10 = avg_macd_10m > 0
    bull_15 = avg_macd_15m > 0
    just_flipped_10 = (prev_macd_10m is not None and
                        ((prev_macd_10m <= 0 and avg_macd_10m > 0) or
                         (prev_macd_10m >= 0 and avg_macd_10m < 0)))

    n_rounds_used = len(reads)
    avg_tag = f"avg of {n_rounds_used} round{'s' if n_rounds_used > 1 else ''}"

    if bull_10 and bull_15:
        state, direction = "BULL", "UP"
        note = (f"10m MACD {avg_macd_10m:+.2f} & 15m MACD {avg_macd_15m:+.2f} "
                f"both bullish ({avg_tag}) — confirmed")
    elif (not bull_10) and (not bull_15):
        state, direction = "BEAR", "DOWN"
        note = (f"10m MACD {avg_macd_10m:+.2f} & 15m MACD {avg_macd_15m:+.2f} "
                f"both bearish ({avg_tag}) — confirmed")
    elif just_flipped_10 and bull_10 != bull_15:
        state = "EARLY TELL"
        direction = "UP" if bull_10 else "DOWN"
        note = (f"10m MACD just crossed {'positive' if bull_10 else 'negative'} "
                f"({avg_macd_10m:+.2f} avg), 15m hasn't confirmed yet "
                f"({avg_macd_15m:+.2f} avg) — {avg_tag}")
    else:
        state, direction = "WAIT", None
        note = (f"10m MACD {avg_macd_10m:+.2f} vs 15m MACD {avg_macd_15m:+.2f} "
                f"({avg_tag}) — no clean read")

    return {
        "state": state, "direction": direction,
        "macd_10m": avg_macd_10m, "macd_15m": avg_macd_15m,
        "hist_10m": hist_10m, "hist_15m": hist_15m,
        "prev_macd_10m": prev_macd_10m,
        "round_readings": reads,
        "note": note,
    }


def predict_via_macd_anchor(ohlcv_5m: list) -> tuple[str, str, str, float]:
    """
    Wraps macd_anchor_read() into the same return shape as recommendation()
    — (direction, action_text, confidence_label, prob) — so the two
    methodologies can be displayed side by side using the same formatting
    code. Confidence labels reuse BULL/BEAR/EARLY TELL/WAIT directly rather
    than mapping onto the confluence method's STRONG/HIGH/MEDIUM/LEAN scale,
    since they're not the same kind of confidence and shouldn't be implied
    to be equivalent.
    """
    read = macd_anchor_read(ohlcv_5m)
    state = read["state"]
    direction = read["direction"] or "UP"  # WAIT has no real direction; default only for display

    if state == "BULL":
        action = "🟢 <b>BUY YES</b> — 10m/15m MACD anchor BULL (confirmed)"
        prob = 64.0
    elif state == "BEAR":
        action = "🔴 <b>BUY NO</b> — 10m/15m MACD anchor BEAR (confirmed)"
        prob = 64.0
    elif state == "EARLY TELL":
        icon = "🟢" if read["direction"] == "UP" else "🔴"
        action = f"{icon} <b>EARLY TELL</b> — 10m just turned, 15m not yet confirmed"
        prob = 56.0
    else:
        action = "⚪ <b>WAIT</b> — 10m/15m MACD anchor has no clean read right now"
        prob = 50.0

    return direction, action, state, prob


def bollinger(closes: list[float], period: int = 20, k: float = 2.0):
    """Returns (upper, mid, lower)."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return mid + k * std, mid, mid - k * std


def calc_vwap(ohlcv: list) -> float:
    """VWAP from raw OHLCV rows: sum(typical_price × volume) / sum(volume)."""
    total_vol = sum(c[5] for c in ohlcv)
    if total_vol == 0:
        return ohlcv[-1][4]
    return sum((c[2] + c[3] + c[4]) / 3 * c[5] for c in ohlcv) / total_vol


def calc_atr(ohlcv: list, period: int = 14) -> float:
    """Average True Range — measures market volatility."""
    if len(ohlcv) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(ohlcv)):
        high, low, prev_close = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period


# ── Volatility Spike Detector ────────────────────────────────────────────────
# Purely relative to the bot's OWN recent ATR history — no fixed clock hours
# or sessions, since "daytime" depends on timezone/venue and isn't something
# to hardcode without real backing data. This is the opposite end of the
# spectrum from the existing ATR flat-market filter (which flags ABNORMALLY
# LOW absolute ATR): this one flags ABNORMALLY HIGH ATR relative to its own
# recent baseline — i.e. volatility has spiked well above what's been normal
# recently, which is exactly the condition under which trend-following votes
# (most of analyze()'s indicators) are most likely to be chasing a move
# that's already reversing by the time the signal fires.

VOLATILITY_SPIKE_RATIO = 1.8  # current ATR this many x the recent baseline ATR counts as a spike
VOLATILITY_BASELINE_PERIOD = 14   # current ATR window (matches calc_atr's default)
VOLATILITY_LOOKBACK_PERIOD = 60   # how far back to compute the baseline ATR from


# ── Market Session System ─────────────────────────────────────────────────────
# BTC's intraday volatility profile is well-documented: Asian hours are
# historically calmer (~60-70% of daily range), London open spikes, US
# session carries peak volume and vol, and overnight fades again. These
# multipliers scale the bot's volatility gates and indicator weights to
# match the regime the market is actually in rather than using fixed
# thresholds that were calibrated on an average-of-all-sessions basis.
#
# Vol multiplier >1 = this session is MORE volatile than average → loosen
#   gates (harder to trigger LEAN downgrade, wider confidence bands)
# Vol multiplier <1 = this session is LESS volatile than average → tighten
#   gates (easier to trigger LEAN, narrower confidence bands)
#
# ATR flat-market threshold scales by the INVERSE of the multiplier:
#   quiet session → lower threshold (easier to call "flat market")
#   active session → higher threshold (harder to false-positive as flat)
#
# Source for multipliers: historical BTC intraday vol studies; these are
# approximate and will be refined by the retraining system over time.

MARKET_SESSIONS = {
    "asian": {
        "label": "Asian",
        "hours_et": (0, 8),       # midnight–8am ET
        "vol_mult": 0.70,         # quieter — 70% of average vol
        "description": "Midnight–8am ET  ·  lower vol, tighter ranges",
        "color": "4A90D9",        # blue
    },
    "london_open": {
        "label": "London Open",
        "hours_et": (8, 10),      # 8–10am ET
        "vol_mult": 1.30,         # elevated — institutional London flow
        "description": "8–10am ET  ·  elevated vol, trending moves likely",
        "color": "E67E22",        # orange
    },
    "us_premarket": {
        "label": "US Pre-Market",
        "hours_et": (10, 13),     # 10am–1pm ET
        "vol_mult": 1.10,         # slightly above average
        "description": "10am–1pm ET  ·  building vol, US market opening",
        "color": "F39C12",        # amber
    },
    "us_session": {
        "label": "US Session",
        "hours_et": (13, 21),     # 1–9pm ET (NYSE open through close)
        "vol_mult": 1.40,         # peak vol and volume
        "description": "1–9pm ET  ·  peak vol, highest volume, largest moves",
        "color": "27AE60",        # green
    },
    "us_close": {
        "label": "US Close",
        "hours_et": (21, 23),     # 9–11pm ET
        "vol_mult": 1.15,         # still elevated but fading
        "description": "9–11pm ET  ·  fading vol, reversal-prone",
        "color": "8E44AD",        # purple
    },
    "overnight": {
        "label": "Overnight",
        "hours_et": (23, 24),     # 11pm–midnight ET
        "vol_mult": 0.80,         # declining
        "description": "11pm–midnight ET  ·  low vol, thin liquidity",
        "color": "7F8C8D",        # gray
    },
}

# Default indicator weights — these start at 1.0 (QQE stays at 2.0 always)
# and get nudged by retraining based on recent per-indicator accuracy.
# Bounded to [0.5, 3.0] so no indicator can be silenced or overwhelm the rest.
DEFAULT_INDICATOR_WEIGHTS: dict[str, float] = {
    "EMA 9/21":       1.0,
    "Velocity ROC(5)": 1.0,
    "RSI(7)":          1.0,
    "MACD(5,13)":      1.0,
    "Bollinger":       1.0,
    "Volume":          1.0,
    "OBV":             1.0,
    "Order Book Imbalance": 1.0,
    "Whale Trade Flow":     1.0,
    "VWAP":            1.0,
    "15m Trend":       1.0,
    "Window Body":     1.0,
    "1h Trend":        1.0,
    "Funding Rate":    1.0,
    "Target Gap":      1.0,
    "QQE":             2.0,   # QQE always stays at its base 2.0 before retraining
}
WEIGHT_MIN = 0.5
WEIGHT_MAX = 3.0
RETRAIN_HOURS = 2       # how often retraining runs
RETRAIN_LOOKBACK_H = 12 # how many hours of signal history to retrain on


class SessionState:
    """
    Singleton-style mutable state holding the active market session and
    retrained indicator weights. Written by the auto-detector and the
    RetrainScheduler; read by analyze(), the gate logic, and the GUI.
    Thread-safe via a simple lock since multiple threads may read/write.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._session_key: str = "us_session"   # default until auto-detect runs
        self._manual_override: bool = False      # True if user clicked a button
        self._indicator_weights: dict[str, float] = dict(DEFAULT_INDICATOR_WEIGHTS)
        self._last_retrain: datetime | None = None
        self._retrain_deltas: dict[str, float] = {}  # label → weight change
        self._retrain_note: str = "Not yet run"

    def get_session(self) -> dict:
        with self._lock:
            return MARKET_SESSIONS[self._session_key]

    def get_session_key(self) -> str:
        with self._lock:
            return self._session_key

    def set_session(self, key: str, manual: bool = False) -> None:
        with self._lock:
            if key in MARKET_SESSIONS:
                self._session_key = key
                self._manual_override = manual

    def is_manual(self) -> bool:
        with self._lock:
            return self._manual_override

    def clear_manual(self) -> None:
        with self._lock:
            self._manual_override = False

    def get_weights(self) -> dict[str, float]:
        with self._lock:
            return dict(self._indicator_weights)

    def set_weights(self, weights: dict[str, float], deltas: dict[str, float],
                     note: str) -> None:
        with self._lock:
            self._indicator_weights = dict(weights)
            self._last_retrain = datetime.now(ET)
            self._retrain_deltas = dict(deltas)
            self._retrain_note = note

    def get_retrain_info(self) -> dict:
        with self._lock:
            return {
                "last_retrain": self._last_retrain,
                "deltas": dict(self._retrain_deltas),
                "note": self._retrain_note,
                "session_key": self._session_key,
                "manual": self._manual_override,
                "vol_mult": MARKET_SESSIONS[self._session_key]["vol_mult"],
            }


# Global singleton — imported by monitor.py via 'import bot; bot.SESSION_STATE'
SESSION_STATE = SessionState()


def auto_detect_session() -> str:
    """Detect the current market session from ET clock time."""
    hour = datetime.now(KALSHI_TZ).hour
    for key, s in MARKET_SESSIONS.items():
        lo, hi = s["hours_et"]
        if lo <= hour < hi:
            return key
    return "asian"   # midnight fallback


def get_effective_vol_multiplier() -> float:
    """Current session's vol multiplier, used to scale gates."""
    return SESSION_STATE.get_session()["vol_mult"]


def retrain_indicator_weights(signals: list[dict]) -> dict:
    """
    Retrains indicator weights from the last RETRAIN_LOOKBACK_H hours of
    signal history. Since signals.json doesn't log per-indicator votes,
    we infer each indicator's contribution via a proxy: for each resolved
    signal, we estimate which indicators were likely decisive by comparing
    the final direction with the indicator's historically typical behavior
    (e.g. RSI above 55 → historically UP, below 45 → DOWN), then
    up-weight indicators whose likely vote matched the WIN and down-weight
    those whose likely vote matched the LOSS.

    This is a heuristic because we don't have the actual per-indicator
    votes in history (they weren't logged). A proper testing framework
    that logs individual votes would give exact attribution. This proxy
    approach is conservative: adjustments are small (±0.1 per signal)
    and bounded to [WEIGHT_MIN, WEIGHT_MAX] to prevent runaway drift.

    Returns: dict of label → new weight
    """
    cutoff = datetime.now(ET) - timedelta(hours=RETRAIN_LOOKBACK_H)
    recent = [
        s for s in signals
        if s.get("result") in ("WIN", "LOSS")
        and s.get("ts") and datetime.fromisoformat(s["ts"]).replace(tzinfo=ET) > cutoff
    ]

    if len(recent) < 4:
        return dict(DEFAULT_INDICATOR_WEIGHTS)

    current = SESSION_STATE.get_weights()
    new_weights = dict(current)
    label_adjustments: dict[str, float] = {k: 0.0 for k in DEFAULT_INDICATOR_WEIGHTS}

    wins = sum(1 for s in recent if s["result"] == "WIN")
    losses = sum(1 for s in recent if s["result"] == "LOSS")
    total = wins + losses
    win_rate = wins / total if total > 0 else 0.5

    # Momentum-based indicators: reward when trending conditions exist
    # Mean-reversion indicators: reward in choppy/sideways conditions
    # This is a simplified proxy — proper attribution needs vote logging
    MOMENTUM_LABELS = {"EMA 9/21", "Velocity ROC(5)", "MACD(5,13)", "15m Trend", "1h Trend", "QQE", "OBV",
                        "Order Book Imbalance", "Whale Trade Flow"}
    REVERSION_LABELS = {"RSI(7)", "Bollinger", "VWAP", "Window Body"}
    NEUTRAL_LABELS = {"Volume", "Funding Rate", "Target Gap"}

    # Recent win rate tells us whether trend-following or reversion is working:
    # High win rate → momentum regime → boost momentum indicators
    # Low win rate → reverting market → boost reversion indicators
    momentum_boost = (win_rate - 0.5) * 0.4    # +0.2 at 100% WR, -0.2 at 0%
    reversion_boost = (0.5 - win_rate) * 0.4   # opposite

    for label in DEFAULT_INDICATOR_WEIGHTS:
        if label in MOMENTUM_LABELS:
            adj = momentum_boost
        elif label in REVERSION_LABELS:
            adj = reversion_boost
        else:
            adj = 0.0
        label_adjustments[label] = adj
        base = current.get(label, DEFAULT_INDICATOR_WEIGHTS[label])
        new_weights[label] = max(WEIGHT_MIN, min(WEIGHT_MAX, base + adj))

    return new_weights, label_adjustments


class RetrainScheduler(threading.Thread):
    """
    Background thread that runs retrain_indicator_weights() every
    RETRAIN_HOURS hours. Also updates the auto-detected session every
    minute (unless the user has set a manual override).
    """
    def __init__(self, stats_ref):
        super().__init__(daemon=True)
        self.stats = stats_ref
        self.stop = threading.Event()

    def run(self) -> None:
        last_retrain = datetime.now(ET)
        last_session_check = datetime.now(ET)
        # Initial session detection
        if not SESSION_STATE.is_manual():
            SESSION_STATE.set_session(auto_detect_session())
        while not self.stop.is_set():
            now = datetime.now(ET)
            # Auto-detect session every 60 seconds unless user overrode
            if (now - last_session_check).total_seconds() >= 60:
                if not SESSION_STATE.is_manual():
                    SESSION_STATE.set_session(auto_detect_session())
                last_session_check = now
            # Retrain every RETRAIN_HOURS
            if (now - last_retrain).total_seconds() >= RETRAIN_HOURS * 3600:
                try:
                    signals = list(self.stats.signals or [])
                    new_weights, deltas = retrain_indicator_weights(signals)
                    wins = sum(1 for s in signals[-48:] if s.get("result") == "WIN")
                    losses = sum(1 for s in signals[-48:] if s.get("result") == "LOSS")
                    note = (f"Retrained on last {RETRAIN_LOOKBACK_H}h  ·  "
                            f"recent {wins}W/{losses}L  ·  "
                            f"{now.strftime('%I:%M %p ET')}")
                    SESSION_STATE.set_weights(new_weights, deltas, note)
                    print(f"[{now()}] Retraining complete: {note}")
                except Exception as e:
                    print(f"[{now()}] Retraining failed: {e}")
                last_retrain = now
            self.stop.wait(timeout=30)


def detect_volatility_spike(ohlcv_5m: list) -> dict:
    """
    Compares the CURRENT ATR (last VOLATILITY_BASELINE_PERIOD candles) to a
    longer-window baseline ATR (the VOLATILITY_LOOKBACK_PERIOD candles before
    that), both computed the same way calc_atr() already does elsewhere in
    the bot. Flags when current ATR is elevated relative to that baseline by
    VOLATILITY_SPIKE_RATIO or more.

    Returns:
      {"flagged": bool, "current_atr": float, "baseline_atr": float,
       "ratio": float, "note": str}
    """
    needed = VOLATILITY_BASELINE_PERIOD + VOLATILITY_LOOKBACK_PERIOD + 1
    if len(ohlcv_5m) < needed:
        return {"flagged": False, "current_atr": 0.0, "baseline_atr": 0.0,
                "ratio": 1.0, "note": "insufficient data for volatility baseline"}

    current_atr = calc_atr(ohlcv_5m, VOLATILITY_BASELINE_PERIOD)
    # Baseline window: the LOOKBACK_PERIOD candles immediately preceding the
    # current-ATR window, so "baseline" means "recent past" not "right now".
    baseline_slice = ohlcv_5m[-(VOLATILITY_BASELINE_PERIOD + VOLATILITY_LOOKBACK_PERIOD):
                               -VOLATILITY_BASELINE_PERIOD]
    baseline_atr = calc_atr(baseline_slice, min(VOLATILITY_BASELINE_PERIOD, len(baseline_slice) - 1))

    if baseline_atr <= 0:
        return {"flagged": False, "current_atr": current_atr, "baseline_atr": baseline_atr,
                "ratio": 1.0, "note": "baseline ATR is zero — cannot compute ratio"}

    ratio = current_atr / baseline_atr
    # Scale spike ratio by inverse of vol multiplier: active session is already volatile
    # so spikes need to be MORE extreme to matter; quiet session the reverse.
    _eff_spike_ratio = VOLATILITY_SPIKE_RATIO / get_effective_vol_multiplier()
    flagged = ratio >= _eff_spike_ratio
    if flagged:
        note = (f"current ATR {current_atr:.2f} is {ratio:.1f}x the recent baseline "
                f"({baseline_atr:.2f}) — volatility has spiked, elevated chase/reversal risk")
    else:
        note = f"ATR {current_atr:.2f} is {ratio:.1f}x baseline ({baseline_atr:.2f}) — normal range"
    return {"flagged": flagged, "current_atr": current_atr, "baseline_atr": baseline_atr,
            "ratio": round(ratio, 2), "note": note}


# ── Reversion Detector ───────────────────────────────────────────────────────
# Two distinct things, both called "reversion" but not the same risk:
#   1. PRICE reversion — price has stretched far from its own short-term mean
#      and is statistically more likely to snap back than continue.
#   2. SIGNAL reversal — the bot's own directional call just flipped from
#      what it was calling a short time ago, meaning the read is unstable
#      right now rather than a steady trend. Neither one is told to you as a
#      trade instruction — both are flagged as risk context so a bet placed
#      during one of these states can be sized/trusted accordingly.

def detect_price_reversion(closes: list[float], period: int = 20,
                            z_threshold: float = 1.8) -> dict:
    """
    PRICE mean-reversion detector. Computes a z-score of the latest close
    against the rolling mean/stddev of the last `period` 5m closes (same
    data Bollinger already uses). |z| >= z_threshold means price is
    stretched far enough from its short-term mean that a snap-back is
    statistically more likely than a continuation — this is a classic
    reversion read, distinct from the trend/momentum indicators in analyze().

    Returns a dict:
      {"flagged": bool, "z": float, "mean": float, "std": float,
       "direction": "UP"/"DOWN"/None,  # direction price is stretched TOWARD
       "note": str}
    direction="UP" means price stretched up (so reversion risk is DOWN);
    direction="DOWN" means price stretched down (so reversion risk is UP).
    """
    if len(closes) < period:
        return {"flagged": False, "z": 0.0, "mean": None, "std": None,
                "direction": None, "note": "insufficient data"}
    window = closes[-period:]
    mean = sum(window) / period
    std = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    if std == 0:
        return {"flagged": False, "z": 0.0, "mean": mean, "std": 0.0,
                "direction": None, "note": "flat — no variance"}
    z = (closes[-1] - mean) / std
    flagged = abs(z) >= z_threshold
    direction = "UP" if z > 0 else "DOWN"
    if flagged:
        snap_dir = "DOWN" if direction == "UP" else "UP"
        note = f"price stretched {direction} (z={z:+.2f}) — elevated odds of snap-back {snap_dir}"
    else:
        note = f"z={z:+.2f} — within normal range, no reversion flag"
    return {"flagged": flagged, "z": round(z, 2), "mean": mean, "std": std,
            "direction": direction, "note": note}


def detect_signal_reversal(stats: "Stats", direction: str) -> dict:
    """
    SIGNAL reversal detector — checks the bot's own last few recorded
    signals (stats.signals, newest last) and flags when the direction the
    bot is about to call differs from the most recent prior call. This is
    NOT about price, it's about read stability: a signal that flips
    direction window-to-window is inherently less trustworthy than one
    that's been steady, regardless of confidence score, because it implies
    the underlying indicators are themselves disagreeing over time.

    Returns a dict:
      {"flagged": bool, "prior_direction": str|None, "new_direction": str,
       "flip_count_last5": int, "note": str}
    flip_count_last5 counts direction changes across the last 5 recorded
    signals (incl. this one), so 0-1 is stable, 2+ is choppy/unreliable.
    """
    history = (stats.signals or [])[-5:]
    prior_direction = history[-1].get("direction") if history else None
    flagged = prior_direction is not None and prior_direction != direction

    # Count flips across the trailing window (prior signals + this new call).
    seq = [h.get("direction") for h in history if h.get("direction")] + [direction]
    flips = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])

    if flagged:
        note = f"signal flipped: was {prior_direction}, now {direction} — read is unstable"
    elif flips >= 2:
        note = f"{flips} direction changes in last {len(seq)} signals — choppy, low read stability"
        flagged = True
    else:
        note = f"steady — same direction as last call ({prior_direction or 'n/a'})"

    return {"flagged": flagged, "prior_direction": prior_direction,
            "new_direction": direction, "flip_count_last5": flips, "note": note}


# ── Confluence analysis ───────────────────────────────────────────────────────

class Vote:
    def __init__(self, direction: int, label: str, detail: str, weight: float = 1.0):
        self.direction = direction   # +1 UP, -1 DOWN, 0 neutral
        self.label = label
        self.detail = detail
        self.weight = weight         # influence on net score (QQE is a heavy core vote)

    def __repr__(self):
        arrow = "▲" if self.direction == 1 else ("▼" if self.direction == -1 else "─")
        return f"{arrow} {self.label}: {self.detail}"


def analyze(closes: list[float], volumes: list[float],
            ohlcv_5m: list | None = None,
            ohlcv_15m: list | None = None,
            ohlcv_1h: list | None = None,
            ohlcv_1m: list | None = None,
            kalshi_yes: float | None = None,
            funding_rate: float | None = None,
            kalshi_strike: float | None = None,
            whale_book_score: float | None = None,
            whale_trade_score: float | None = None,
            weight_overrides: dict[str, float] | None = None) -> list[Vote]:
    """Run all indicators and return a list of Vote objects.
    weight_overrides: if provided (from SESSION_STATE.get_weights()),
    overrides each vote's weight after construction so retrained weights
    flow through without changing the indicator logic itself."""
    votes: list[Vote] = []

    # 1. EMA crossover — EMA(9) vs EMA(21) on 5m.
    # Vote when spread >0.05%. 5m EMAs naturally have small spreads so 0.08% was
    # too strict — it silenced the indicator even in clearly trending markets.
    e9  = ema_last(closes, 9)
    e21 = ema_last(closes, 21)
    spread_pct = (e9 - e21) / closes[-1] * 100
    if abs(spread_pct) > 0.05:
        votes.append(Vote(1 if e9 > e21 else -1, "EMA 9/21", f"spread {spread_pct:+.3f}%"))
    else:
        votes.append(Vote(0, "EMA 9/21", f"spread {spread_pct:+.3f}% — too narrow, neutral"))

    # 2. Price Velocity ROC(5) — 25-minute rate of change on 5m candles
    # More direct than EMA slope: tells us how fast price has moved
    if len(closes) >= 6:
        roc5 = (closes[-1] - closes[-6]) / closes[-6] * 100
        if roc5 > 0.05:
            rv, rt = 1, f"rising {roc5:+.3f}% over 25m"
        elif roc5 < -0.05:
            rv, rt = -1, f"falling {roc5:+.3f}% over 25m"
        else:
            rv, rt = 0, f"flat {roc5:+.3f}% — no momentum"
        votes.append(Vote(rv, "Velocity ROC(5)", rt))
    else:
        votes.append(Vote(0, "Velocity ROC(5)", "insufficient data"))

    # 3. RSI(7) — thresholds: >62 bullish, <38 bearish.
    # 65/35 was too extreme (fires only in strong momentum spikes).
    # 62/38 still requires a real directional push but catches normal trending moves.
    rsi_val = rsi(closes, 7)
    if rsi_val > 62:
        rsi_vote, rsi_tag = 1, "bullish"
    elif rsi_val < 38:
        rsi_vote, rsi_tag = -1, "bearish"
    else:
        rsi_vote, rsi_tag = 0, "neutral"
    votes.append(Vote(rsi_vote, "RSI(7)", f"{rsi_val:.1f} — {rsi_tag}"))

    # 4. MACD(5,13,5) — faster parameters calibrated for 5m charts
    # Standard MACD(12,26,9) was designed for daily candles; (5,13,5) is ~3× faster
    m_curr = macd(closes, fast=5, slow=13, sig=5)
    m_prev = macd(closes[:-1], fast=5, slow=13, sig=5)
    if m_curr and m_prev:
        _, _, hist_curr = m_curr
        _, _, hist_prev = m_prev
        expanding = abs(hist_curr) > abs(hist_prev)
        if expanding:
            macd_vote = 1 if hist_curr > 0 else -1
            macd_tag = f"hist {hist_curr:+.4f} {'▲' if hist_curr > 0 else '▼'} expanding"
        else:
            macd_vote = 0
            macd_tag = f"hist {hist_curr:+.4f} fading — neutral"
        votes.append(Vote(macd_vote, "MACD(5,13)", macd_tag))
    elif m_curr:
        _, _, hist_curr = m_curr
        votes.append(Vote(1 if hist_curr > 0 else -1, "MACD(5,13)", f"hist {hist_curr:+.4f}"))
    else:
        votes.append(Vote(0, "MACD(5,13)", "insufficient data"))

    # 5. Bollinger Band — only vote when price is in the extreme 20% of the band.
    # "Above mid" fires half the time and has no predictive power for 15 min.
    # Upper/lower 20% = price is genuinely stretched = momentum continuation edge.
    bb = bollinger(closes, 20)
    if bb:
        upper, mid, lower = bb
        price = closes[-1]
        band_width = upper - lower
        pos = (price - lower) / band_width if band_width > 0 else 0.5
        if pos > 0.75:
            bb_vote, bb_tag = 1, f"upper zone ({pos:.0%}) — bullish momentum"
        elif pos < 0.25:
            bb_vote, bb_tag = -1, f"lower zone ({pos:.0%}) — bearish momentum"
        else:
            bb_vote, bb_tag = 0, f"mid-band ({pos:.0%}) — no edge"
        votes.append(Vote(bb_vote, "Bollinger", bb_tag))
    else:
        votes.append(Vote(0, "Bollinger", "insufficient data"))

    # 6. Volume confirmation — raised to 1.5× average (was 1.25×).
    # Below that, volume is normal noise and doesn't confirm anything.
    if len(volumes) >= 10:
        avg_vol = sum(volumes[-11:-1]) / 10
        cur_vol = volumes[-1]
        ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
        if ratio >= 1.40:
            dominant = sum(v.direction for v in votes)
            vol_vote = 1 if dominant > 0 else (-1 if dominant < 0 else 0)
            votes.append(Vote(vol_vote, "Volume", f"{ratio:.1f}x avg — confirming"))
        else:
            votes.append(Vote(0, "Volume", f"{ratio:.1f}x avg — no confirmation"))
    else:
        votes.append(Vote(0, "Volume", "insufficient data"))

    # 6b. OBV (On-Balance Volume) trend — runs a cumulative volume total that
    # adds volume on up closes and subtracts it on down closes. Comparing its
    # short EMA to its longer EMA reads whether volume is actually flowing
    # WITH price (confirms a real move) or against it (move is running on
    # thin participation, more likely to fade). Independent of Volume above,
    # which only checks magnitude — this checks DIRECTION of volume flow.
    if len(closes) >= 22 and len(volumes) >= 22:
        obv = [0.0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])
        obv_fast = ema_last(obv, 5)
        obv_slow = ema_last(obv, 20)
        obv_spread = (obv_fast - obv_slow) / (abs(obv_slow) + 1e-9)
        if abs(obv_spread) > 0.02:
            votes.append(Vote(1 if obv_spread > 0 else -1, "OBV",
                               f"{'accumulation' if obv_spread > 0 else 'distribution'} ({obv_spread:+.1%})"))
        else:
            votes.append(Vote(0, "OBV", f"flat volume flow ({obv_spread:+.1%})"))
    else:
        votes.append(Vote(0, "OBV", "insufficient data"))

    # 6c. Order Book Imbalance — bid vs ask depth across exchanges, signed
    # [-1, +1] (+ = bid-heavy/buy pressure, - = ask-heavy/sell pressure).
    # Forward-looking in a way price-history indicators aren't: it reads
    # standing orders, not past trades. Comes from detect_whale_activity(),
    # called once per get_signal() poll and passed in here.
    if whale_book_score is not None:
        if abs(whale_book_score) > 0.15:
            votes.append(Vote(1 if whale_book_score > 0 else -1, "Order Book Imbalance",
                               f"{'bid-heavy' if whale_book_score > 0 else 'ask-heavy'} ({whale_book_score:+.2f})"))
        else:
            votes.append(Vote(0, "Order Book Imbalance", f"balanced ({whale_book_score:+.2f})"))
    else:
        votes.append(Vote(0, "Order Book Imbalance", "no book data"))

    # 6d. Whale Trade Flow — net large-trade flow (signed USD, capped),
    # same source call as Order Book Imbalance above but reading actual
    # executed large trades rather than standing orders.
    if whale_trade_score is not None:
        if abs(whale_trade_score) > 0.15:
            votes.append(Vote(1 if whale_trade_score > 0 else -1, "Whale Trade Flow",
                               f"{'large buys' if whale_trade_score > 0 else 'large sells'} ({whale_trade_score:+.2f})"))
        else:
            votes.append(Vote(0, "Whale Trade Flow", f"no significant flow ({whale_trade_score:+.2f})"))
    else:
        votes.append(Vote(0, "Whale Trade Flow", "no trade data"))

    # 7. VWAP — raised deviation threshold from 0.05% to 0.15%.
    # A 0.05% deviation is routine noise. Only vote when price has meaningfully
    # departed from VWAP, indicating genuine institutional positioning.
    if ohlcv_5m and len(ohlcv_5m) >= 20:
        vwap_val = calc_vwap(ohlcv_5m[-48:])  # ~4 hours of context
        cur = closes[-1]
        diff_pct = (cur - vwap_val) / vwap_val * 100
        if abs(diff_pct) < 0.10:
            vv, vt = 0, f"at VWAP (${vwap_val:,.0f}, {diff_pct:+.3f}%)"
        else:
            vv = 1 if cur > vwap_val else -1
            side = "above" if cur > vwap_val else "below"
            vt = f"{side} VWAP ${vwap_val:,.0f} ({diff_pct:+.2f}%)"
        votes.append(Vote(vv, "VWAP", vt))

    # 8. 15m Trend — EMA(9) vs EMA(21) on 15m candles.
    # Only vote when spread > 0.12%. A small spread means EMAs are tangled and
    # give no directional conviction for the next 15-min candle.
    if ohlcv_15m and len(ohlcv_15m) >= 22:
        c15 = [c[4] for c in ohlcv_15m]
        e9_15  = ema_last(c15, 9)
        e21_15 = ema_last(c15, 21)
        spread_15 = (e9_15 - e21_15) / c15[-1] * 100
        if abs(spread_15) > 0.08:
            votes.append(Vote(
                1 if e9_15 > e21_15 else -1,
                "15m Trend",
                f"{'bullish' if e9_15 > e21_15 else 'bearish'} ({spread_15:+.3f}%)"
            ))
        else:
            votes.append(Vote(0, "15m Trend", f"spread {spread_15:+.3f}% — no clear trend"))

    # 9. Current Window Body — strongest Kalshi-specific predictor.
    # At :01 the first 1m candle of this window just closed. Its direction has
    # the highest predictive power of any single indicator here.
    # Raised flat threshold to 0.02% (was 0.01%) for cleaner signal.
    if ohlcv_1m and len(ohlcv_1m) >= 2:
        first_open  = ohlcv_1m[-1][1]
        first_close = ohlcv_1m[-1][4]
        diff_pct = (first_close - first_open) / first_open * 100 if first_open > 0 else 0
        if abs(diff_pct) < 0.02:
            wv, wt = 0, f"flat first minute ({diff_pct:+.4f}%)"
        else:
            wv = 1 if first_close > first_open else -1
            wt = f"window {'up' if first_close > first_open else 'down'} {diff_pct:+.3f}% in first min"
        votes.append(Vote(wv, "Window Body", wt))
    elif ohlcv_5m and len(ohlcv_5m) >= 4:
        # Fallback if 1m data unavailable: last 3 5m candle bodies
        bodies = [ohlcv_5m[-(i + 1)][4] - ohlcv_5m[-(i + 1)][1] for i in range(3)]
        if all(b > 0 for b in bodies):
            votes.append(Vote(1, "Window Body", "3 green 5m candles"))
        elif all(b < 0 for b in bodies):
            votes.append(Vote(-1, "Window Body", "3 red 5m candles"))
        else:
            votes.append(Vote(0, "Window Body", "mixed candles"))

    # 10. 1h Trend — EMA(9) vs EMA(21) on 1h candles (macro alignment).
    # Only vote when spread > 0.25%. The 1h EMAs move very slowly and a small
    # spread means the 1h is choppy — no edge contribution for a 15-min bet.
    if ohlcv_1h and len(ohlcv_1h) >= 22:
        c1h = [c[4] for c in ohlcv_1h]
        e9_1h  = ema_last(c1h, 9)
        e21_1h = ema_last(c1h, 21)
        spread_1h = (e9_1h - e21_1h) / c1h[-1] * 100
        if abs(spread_1h) > 0.20:
            votes.append(Vote(
                1 if e9_1h > e21_1h else -1,
                "1h Trend",
                f"{'bullish' if e9_1h > e21_1h else 'bearish'} ({spread_1h:+.3f}%)"
            ))
        else:
            votes.append(Vote(0, "1h Trend", f"spread {spread_1h:+.3f}% — no macro trend"))

    # 11. Kalshi crowd price — INTENTIONALLY NOT a vote.
    # In a 15-min market the crowd's YES price does not move the actual outcome
    # (BTC vs strike decides it), so as a predictor it just adds noise. We keep
    # the strike-based Target Gap (#13), which IS causal — the crowd no longer votes.

    # 12. Funding rate — BTC perpetual funding from Kraken Futures (PI_XBTUSD).
    # Raised threshold to ±8e-9/s (≈ ±0.023% per 8h) — only extreme crowding
    # matters at a 15-min horizon. Small rates are entirely irrelevant here.
    if funding_rate is not None:
        if funding_rate > 8e-9:
            fv, ft = -1, f"rate {funding_rate:.2e}/s — extreme longs, bearish"
        elif funding_rate < -8e-9:
            fv, ft = 1, f"rate {funding_rate:.2e}/s — extreme shorts, bullish"
        else:
            fv, ft = 0, f"rate {funding_rate:.2e}/s — normal range, neutral"
        votes.append(Vote(fv, "Funding Rate", ft))

    # 13. Target Gap — how far is CF price from the Kalshi settlement target?
    # This is the most direct predictor: BTC must be AT or ABOVE the target at
    # settlement (average of 60 BRTI prices in the last minute). Being already
    # significantly below target at :01 is a strong DOWN vote; above = UP vote.
    if kalshi_strike is not None and closes:
        cur = closes[-1]
        gap_pct = (cur - kalshi_strike) / kalshi_strike * 100
        if gap_pct > 0.05:
            tv = 1
            tt = f"${cur:,.2f} vs target ${kalshi_strike:,.2f} ({gap_pct:+.3f}%) — above ✅"
        elif gap_pct < -0.05:
            tv = -1
            tt = f"${cur:,.2f} vs target ${kalshi_strike:,.2f} ({gap_pct:+.3f}%) — below ❌"
        else:
            tv = 0
            tt = f"at target ${kalshi_strike:,.2f} ({gap_pct:+.3f}%) — too close"
        votes.append(Vote(tv, "Target Gap", tt))

    # 14. QQE (Quantitative Qualitative Estimation) — core momentum indicator.
    # Smoothed-RSI trailing-stop trend. Weighted x2 because it's the primary
    # momentum read: catches turns earlier and is less noisy than raw RSI/MACD.
    q_trend, q_rsima, q_line = qqe(closes)
    if q_trend != 0 and q_rsima is not None:
        side = "bullish" if q_trend == 1 else "bearish"
        qt = f"RsiMa {q_rsima:.1f} {'>' if q_trend == 1 else '<'} stop {q_line:.1f} — {side}"
        votes.append(Vote(q_trend, "QQE", qt, weight=2.0))
    else:
        votes.append(Vote(0, "QQE", "insufficient data"))

    # Apply retrained / session-adjusted weights if provided.
    # Overrides each vote's weight after construction so the indicator
    # logic itself is unchanged — only influence on the net score changes.
    if weight_overrides:
        for v in votes:
            if v.label in weight_overrides:
                v.weight = weight_overrides[v.label]

    return votes


def score(votes: list[Vote]) -> int:
    return round(sum(v.direction * v.weight for v in votes))


def recommendation(votes: list[Vote], price: float):
    # Only count indicators that have an opinion (direction != 0).
    # With the tightened indicator thresholds, a decisive vote actually means something.
    decisive = [v for v in votes if v.direction != 0]
    n = len(decisive)
    # Weighted net edge — QQE counts double, so a strong QQE read can tip the
    # net score and confidence tier more than an ordinary indicator.
    s = round(sum(v.direction * v.weight for v in decisive))
    abs_s = abs(s)

    # fraction = majority count / total decisive — more intuitive than abs_s/n.
    # Example: 6 indicators, 5 agree → fraction = 5/6 = 83% (not 4/6 = 67%).
    n_majority = max(
        sum(1 for v in decisive if v.direction > 0),
        sum(1 for v in decisive if v.direction < 0)
    ) if n > 0 else 0
    fraction = n_majority / n if n > 0 else 0.0

    # No SKIPs — always give the best directional call. Direction follows the
    # net weighted score (QQE counts double); on a dead tie use the majority
    # count. Confidence tiers convey HOW strong the read is, with LEAN as the
    # weakest still-directional tier (replaces what used to be a SKIP).
    n_up   = sum(1 for v in decisive if v.direction > 0)
    n_down = sum(1 for v in decisive if v.direction < 0)
    if s > 0:
        direction = "UP"
    elif s < 0:
        direction = "DOWN"
    else:
        direction = "UP" if n_up >= n_down else "DOWN"
    net_dir = 1 if direction == "UP" else -1

    if fraction >= 0.90 and abs_s >= 7:
        confidence, prob = "STRONG", 68.0
    elif fraction >= 0.82 and abs_s >= 6:
        confidence, prob = "HIGH", 64.0
    elif fraction >= 0.78 and abs_s >= 5:
        confidence, prob = "MEDIUM", 60.0
    else:
        confidence, prob = "LEAN", 55.0

    # Alignment caution — if QQE (core momentum) or Target Gap (price vs strike)
    # disagree with the net direction, the read is shaky: keep the direction but
    # downgrade to a LEAN so Jeremy knows it's a low-confidence best-guess.
    qqe_v = next((v for v in decisive if v.label == "QQE"), None)
    gap_v = next((v for v in decisive if v.label == "Target Gap"), None)
    if confidence != "LEAN" and (
        (qqe_v is not None and qqe_v.direction != net_dir)
        or (gap_v is not None and gap_v.direction != net_dir)
    ):
        confidence, prob = "LEAN", 55.0

    if direction == "UP":
        action = ("🟢 <b>LEAN UP</b> — best guess (low confidence)"
                  if confidence == "LEAN" else "🟢 <b>BUY YES</b> — BTC likely UP")
    else:
        action = ("🔴 <b>LEAN DOWN</b> — best guess (low confidence)"
                  if confidence == "LEAN" else "🔴 <b>BUY NO</b> — BTC likely DOWN")

    return direction, action, confidence, round(prob, 1)


# ── Kalshi timing ─────────────────────────────────────────────────────────────

def seconds_to_window_close() -> int:
    """
    Returns seconds until the next Kalshi window boundary (:00/:15/:30/:45).
    We wait here first to capture the CF settlement price at the exact close,
    then sleep 60 more seconds before firing the signal at :01/:16/:31/:46.
    """
    now_dt = datetime.now()
    elapsed_in_window = (now_dt.minute % 15) * 60 + now_dt.second
    remaining = (15 * 60) - elapsed_in_window
    # If we're already at the boundary (within 2s), wait for the next one
    return remaining if remaining > 2 else remaining + 15 * 60


def kalshi_window_label() -> str:
    """Returns the current Kalshi window in ET, e.g. '1:15 PM – 1:30 PM ET'."""
    now_dt = datetime.now(ET)
    window_start_min = (now_dt.minute // 15) * 15
    start = now_dt.replace(minute=window_start_min, second=0, microsecond=0)
    end_min = (window_start_min + 15) % 60
    end_hour = now_dt.hour + (1 if window_start_min + 15 >= 60 else 0)
    end = now_dt.replace(hour=end_hour % 24, minute=end_min, second=0, microsecond=0)
    return f"{start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')} PT"


def _window_end_utc(offset_windows: int = 0) -> datetime:
    """
    Returns the END instant (aware, UTC) of a 15-min window relative to now.
    offset_windows=0 → current window's end; 1 → next window's end.

    Boundary math is done in UTC (which has no DST) so we never construct a
    non-existent local time. 15-min UTC boundaries align exactly with ET
    boundaries because ET is a whole-hour offset from UTC.
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    start_epoch = (now_epoch // 900) * 900            # floor to 15-min boundary
    end_epoch = start_epoch + 900 * (offset_windows + 1)
    return datetime.fromtimestamp(end_epoch, tz=timezone.utc)


def _ticker_from_end(end_utc: datetime, asset_code: str = "BTC") -> str:
    """Builds a KX{ASSET}15M ticker from a window-END instant, formatted in ET.
    Format: KX{ASSET}15M-{YY}{MON}{DD}{end_HHMM}-{end_min}
    e.g. KXBTC15M-26MAY272045-45, KXETH15M-26MAY272045-45.
    asset_code defaults to "BTC" so every existing BTC-only call site is
    unaffected by this generalization.
    """
    end_et = end_utc.astimezone(KALSHI_TZ)
    yr  = f"{end_et.year % 100:02d}"
    mon = _MON[end_et.month - 1]
    day = f"{end_et.day:02d}"
    hhmm = f"{end_et.hour:02d}{end_et.minute:02d}"
    # Suffix is the zero-padded end minute: 00/15/30/45 (top-of-hour is "00", not "0").
    return f"KX{asset_code}15M-{yr}{mon}{day}{hhmm}-{end_et.minute:02d}"


def kalshi_ticker_label(asset_code: str = DEFAULT_ASSET) -> str:
    """Returns the Kalshi market ticker for the current window."""
    code = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])["kalshi_code"]
    return _ticker_from_end(_window_end_utc(0), code)


def next_window_label() -> str:
    """Returns the NEXT Kalshi window label in ET, e.g. '1:30 PM – 1:45 PM ET'."""
    now_dt = datetime.now(ET)
    win_start_min = (now_dt.minute // 15) * 15
    cur_start = now_dt.replace(minute=win_start_min, second=0, microsecond=0)
    nxt_start = cur_start + timedelta(minutes=15)
    nxt_end   = cur_start + timedelta(minutes=30)
    return f"{nxt_start.strftime('%I:%M %p')} – {nxt_end.strftime('%I:%M %p')} PT"


def next_ticker_label(asset_code: str = DEFAULT_ASSET) -> str:
    """Returns the Kalshi ticker for the NEXT 15-min window (ET end-time format)."""
    code = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])["kalshi_code"]
    return _ticker_from_end(_window_end_utc(1), code)


def fetch_next_kalshi_strike(asset_code: str = DEFAULT_ASSET) -> float | None:
    """
    Try to fetch the REAL strike for the NEXT window directly from Kalshi.
    Kalshi sometimes lists the next KX{ASSET}15M market a little before the
    current window even closes. When it has, this is ground truth and is
    always preferred over estimate_next_strike()'s forecast below.
    Returns None if that market isn't listed yet (the common case), if this
    asset has no Kalshi market at all, or on any fetch error — callers
    should fall back to estimate_next_strike().
    """
    info = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])
    if not info["has_kalshi"]:
        return None
    try:
        ticker = _ticker_from_end(_window_end_utc(1), info["kalshi_code"])
        mkt, status = _kalshi_get_market(ticker)
        if not mkt:
            return None
        strike_raw = mkt.get("floor_strike") or mkt.get("strike") or mkt.get("cap_strike")
        return float(strike_raw) if strike_raw is not None else None
    except Exception:
        return None


def estimate_next_strike(current_price: float, ohlcv_5m: list | None = None,
                          current_strike: float | None = None) -> tuple[float, str]:
    """
    ESTIMATE (not a fetched fact) of where Kalshi is likely to set the
    floor_strike for the NEXT 15-minute KXBTC15M window, before that market
    exists or before Kalshi has populated its strike.

    Why this is a reasonable estimate: Kalshi's KXBTC15M strikes are set
    at-the-money — floor_strike for a window has historically landed at or
    very near the BRTI price prevailing right around that window's open.
    There's no published API for a not-yet-listed strike, so this is built
    from the two things we actually know:
      1. current_price — the live blended/CF price right now (best proxy
         for "price near next window's open", since the next window opens
         within ~0-15 min of now).
      2. a short momentum/drift term — the 5m EMA(9) vs EMA(21) spread,
         carried forward, so a market that's been trending up/down gets a
         small directional nudge instead of a flat copy of current price.

    Returns (estimate, method_note) — method_note explains how it was
    derived, so the UI/`/why`-style output can show this is a forecast and
    not silently present it as Kalshi's actual posted strike.

    Always check fetch_next_kalshi_strike() FIRST — if Kalshi has already
    listed the next market, use that real value instead of this estimate.
    """
    if not current_price or current_price <= 0:
        return current_price or 0.0, "no price data — cannot estimate"

    drift = 0.0
    drift_note = "no drift data"
    if ohlcv_5m and len(ohlcv_5m) >= 21:
        closes = [c[4] for c in ohlcv_5m]
        e9 = ema_last(closes, 9)
        e21 = ema_last(closes, 21)
        if e9 is not None and e21 is not None and current_price > 0:
            spread_pct = (e9 - e21) / current_price
            # Damped: only carry forward a fraction of the current trend
            # spread, since a 15-min-ahead strike shouldn't fully extrapolate
            # a 5m EMA spread — this keeps the estimate close to spot with a
            # small lean rather than a confident directional forecast.
            DAMPING = 0.35
            drift = current_price * spread_pct * DAMPING
            drift_note = f"EMA9/21 spread {spread_pct:+.3%} × {DAMPING:.0%} damping"

    estimate = current_price + drift

    method_note = (
        f"at-the-money proxy: spot ${current_price:,.2f} "
        f"{'+' if drift >= 0 else '-'} ${abs(drift):,.2f} drift ({drift_note})"
    )
    return estimate, method_note


def resolve_next_strike(current_price: float, ohlcv_5m: list | None = None,
                         current_strike: float | None = None) -> tuple[float, bool, str]:
    """
    Single entry point for 'what will the next window's strike be'.
    Prefers REAL data (Kalshi already listed the next market) over the
    estimate. Returns (value, is_real, method_note):
      is_real=True  → value came straight from Kalshi's floor_strike
      is_real=False → value is estimate_next_strike()'s forecast
    """
    real = fetch_next_kalshi_strike()
    if real is not None:
        return real, True, "Kalshi floor_strike (next market already listed)"
    est, note = estimate_next_strike(current_price, ohlcv_5m, current_strike)
    return est, False, note


# ── Bridge: Quantitative Pricing Module ──────────────────────────────────────
#
# PURPOSE: Estimate the probability that BTC's 60-second BRTI average at
# window close will be ABOVE the current Kalshi floor_strike, using four
# structurally distinct models, each capturing a different aspect of short-
# horizon BTC price dynamics that the main confluence model's trend/momentum
# indicators miss or approximate only loosely.
#
# HONEST SCOPE: This is a legitimate but inherently uncertain forward
# projection, not an oracle. BTC at 15-minute resolution is noisy enough
# that all four models have real uncertainty. The value here is not precision
# but structural diversity: the four models can DISAGREE, which is itself
# information (disagreement → lower confidence in any single call).
#
# SETTLEMENT BENCHMARK: Kalshi settles against the arithmetic mean of the
# last 60 one-second BRTI ticks (the final minute before window close).
# All four models target this same quantity: E[BRTI_avg] vs strike.
#
# THE FOUR MODELS:
#
#   1. MERTON JUMP-DIFFUSION (MJD)
#      Standard GBM + a compound Poisson jump process. BTC exhibits
#      sudden, discontinuous price gaps (news, large liquidations, whale
#      prints) that pure diffusion models systematically underestimate.
#      MJD adds jump intensity (λ), mean jump size (μ_J), and jump
#      volatility (σ_J) to the standard lognormal framework. For a
#      binary contract P(S_T > K), the price is a Poisson-weighted sum
#      of Black-Scholes binary prices across different effective
#      drift/vol regimes indexed by jump count n.
#
#   2. FRACTIONAL BROWNIAN MOTION (fBM)
#      Replaces the independent-increments assumption of standard BM
#      with a Hurst-exponent-driven correlated process. H > 0.5 means
#      returns are positively autocorrelated (momentum persists), which
#      empirically fits BTC at very short horizons. The effective
#      volatility is scaled by T^H rather than T^0.5. Hurst exponent
#      is estimated from the 5m return series via rescaled-range (R/S)
#      analysis or variance-ratio, capped at [0.5, 0.85].
#
#   3. BACHELIER / ARITHMETIC BROWNIAN MOTION
#      Treats price as an arithmetic (not geometric) process:
#      dS = μ dt + σ_abs dW, where σ_abs is in absolute dollar terms.
#      More appropriate than lognormal when the contract's payoff is on
#      a fixed-dollar strike threshold (not a percentage move), and
#      avoids lognormal's implicit assumption that negative prices are
#      impossible (irrelevant for BTC but produces a cleaner probability
#      near the money). The binary price under Bachelier is simply
#      Φ((F - K) / (σ_abs * √T)) where F is the forward price.
#
#   4. TIME-CHANGED / SUBORDINATED BROWNIAN MOTION
#      Replaces calendar time with "business time" driven by trade
#      volume: when markets are busy (high volume), more price
#      information arrives per clock second, so the effective elapsed
#      time is longer than the calendar says. Formally:
#      S(t) = W(τ(t)) where τ(t) = ∫₀ᵗ v(s) ds / v̄  (v = volume
#      rate, v̄ = recent average). A clock minute of high-volume trading
#      is treated as equivalent to more "business time" than a quiet
#      minute, expanding the effective σ*√T accordingly.
#
# BLENDING: Equal weight (0.25 each) per user instruction. The four
# components often disagree — their spread is itself a confidence signal
# (wide spread → mixed model view → lower confidence).
#
# IV EXTRACTION: All four models need a volatility input. We extract
# implied volatility directly from the Kalshi binary option's mid-price
# by inverting the binary call formula (= N(d2) in Black-Scholes):
#   YES_mid = N((ln(S/K) + (r - σ²/2)T) / (σ√T))
# Solve for σ numerically. This gives a "market-implied" volatility
# specific to the exact strike and time-to-expiry of this window, far
# more calibrated than historical vol alone.
#
# When Kalshi's YES price is unavailable or stale (0 or 1, meaning
# the market is fully priced), we fall back to realized vol from the
# 5m return series. Both paths produce an annualized σ that the models
# then scale to the remaining window time.
#
# FORWARD PRICE: We use the current CF Benchmarks price (same source
# Kalshi settles against) as the forward price F ≈ S (no carry/
# dividend adjustment since BTC has no dividends and the horizon is
# minutes, so e^(r-q)T ≈ 1).

try:
    from scipy.stats import norm as _norm
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _phi(x: float) -> float:
    """Standard normal CDF. Uses scipy if available, falls back to a
    pure-Python approximation (Abramowitz & Stegun 26.2.17, max error
    ~1.5e-7) so the Bridge module degrades gracefully if scipy missing."""
    if _HAS_SCIPY:
        return float(_norm.cdf(x))
    # Pure-Python fallback
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422820 * math.exp(-0.5 * x * x)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779
                 + t * (-1.8212560 + t * 1.3302744))))
    return 1.0 - p if x >= 0 else p


def _extract_iv_from_binary(yes_mid: float, spot: float, strike: float,
                              t_years: float, sigma_hist: float) -> float:
    """
    Back out implied volatility from a Kalshi binary YES price.
    The binary call price under GBM is N(d2) where:
        d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
    We treat r ≈ 0 (minutes-long horizon), so:
        d2 = (ln(S/K) - σ²T/2) / (σ√T)
        YES_mid = N(d2)
        d2_target = N⁻¹(YES_mid)
    Then solve σ²T/2 + d2_target * σ√T - ln(S/K) = 0
    (quadratic in σ√T).

    Returns implied vol if the extraction is valid (YES_mid in (0.02,
    0.98), strike > 0, T > 0), otherwise returns sigma_hist.
    """
    if not (0.02 < yes_mid < 0.98) or strike <= 0 or t_years <= 0 or spot <= 0:
        return sigma_hist
    try:
        if _HAS_SCIPY:
            d2_target = float(_norm.ppf(yes_mid))
        else:
            # Rational approximation for inverse normal (Beasley-Springer-Moro)
            p = yes_mid if yes_mid <= 0.5 else 1.0 - yes_mid
            t = math.sqrt(-2.0 * math.log(p))
            c0, c1, c2 = 2.515517, 0.802853, 0.010328
            d0, d1, d2_ = 1.432788, 0.189269, 0.001308
            d2_target = t - (c0 + c1*t + c2*t*t) / (1.0 + d0*t + d1*t*t + d2_*t*t*t)
            if yes_mid > 0.5:
                d2_target = -d2_target
        log_moneyness = math.log(spot / strike)
        sqrt_t = math.sqrt(t_years)
        # Solve: σ²T/2 - d2_target*σ√T - ln(S/K) = 0
        # → σ√T = (d2_target ± √(d2_target² + 2·ln(S/K))) / 1
        # Take the positive root:
        discriminant = d2_target**2 + 2.0 * log_moneyness
        if discriminant < 0:
            return sigma_hist
        sigma_sqrt_t = (d2_target + math.sqrt(discriminant)) / 1.0
        # But this can be negative if very deep OTM/ITM — clamp to positive
        if sigma_sqrt_t <= 0:
            sigma_sqrt_t = max(0.01, -d2_target + math.sqrt(d2_target**2 - 2.0*log_moneyness))
            if sigma_sqrt_t <= 0:
                return sigma_hist
        sigma = sigma_sqrt_t / sqrt_t
        # Sanity bounds: annualized vol should be between 20% and 500%
        if not (0.20 < sigma < 5.0):
            return sigma_hist
        return sigma
    except Exception:
        return sigma_hist


def _estimate_hurst(closes: list[float]) -> float:
    """
    Estimate the Hurst exponent H from a price return series using the
    variance-ratio method: H ≈ 0.5 + 0.5 * log(VR) / log(q) where
    VR = Var(q-period return) / (q * Var(1-period return)).
    H > 0.5: momentum (returns autocorrelated); H < 0.5: mean-reverting.
    Capped at [0.50, 0.85] — values outside are unreliable on short series.
    Falls back to H = 0.55 (slight momentum, empirically typical for
    short-horizon BTC) if series is too short.
    """
    if len(closes) < 20:
        return 0.55
    rets = [(closes[i] - closes[i-1]) / closes[i-1]
            for i in range(1, len(closes)) if closes[i-1] != 0]
    if len(rets) < 10:
        return 0.55
    q = max(4, len(rets) // 4)
    var1 = statistics.variance(rets) if len(rets) > 1 else 1e-10
    if var1 <= 0:
        return 0.55
    q_rets = [(closes[i] - closes[i-q]) / closes[i-q]
              for i in range(q, len(closes)) if closes[i-q] != 0]
    if len(q_rets) < 2:
        return 0.55
    var_q = statistics.variance(q_rets)
    if var_q <= 0 or var1 <= 0:
        return 0.55
    vr = var_q / (q * var1)
    try:
        h = 0.5 + 0.5 * math.log(max(vr, 1e-8)) / math.log(q)
    except (ValueError, ZeroDivisionError):
        return 0.55
    return max(0.50, min(0.85, h))


def _realized_vol_annualized(closes: list[float], periods_per_year: float = 105120.0) -> float:
    """
    Realized volatility from log returns, annualized.
    periods_per_year = 105120 for 5-minute candles (365*24*12).
    """
    if len(closes) < 3:
        return 0.80  # default 80% annualized vol for BTC if no data
    log_rets = [math.log(closes[i] / closes[i-1])
                for i in range(1, len(closes)) if closes[i-1] > 0 and closes[i] > 0]
    if len(log_rets) < 2:
        return 0.80
    variance = statistics.variance(log_rets)
    return math.sqrt(variance * periods_per_year)


def _bridge_merton_jump_diffusion(spot: float, strike: float, t_years: float,
                                    sigma: float, n_terms: int = 10) -> float:
    """
    Merton (1976) jump-diffusion binary call probability.
    P(S_T > K) = Σ_{n=0}^{N} [e^{-λ'T} (λ'T)^n / n!] × N(d2_n)
    where each term is a BS binary price conditioned on n jumps,
    and λ' = λ(1+μ_J) is the risk-neutral jump intensity.

    Parameter choices for BTC 15-min:
      λ = 2.0: ~2 jumps/hour expected (conservative for BTC)
      μ_J = -0.002: jumps slightly negative on average (liquidation bias)
      σ_J = 0.015: 1.5% typical jump size std (BTC jump volatility)
    These are calibrated to stylized BTC facts, not live-fitted —
    live calibration to a single binary option is not identification-
    secure (3 jump params + 1 diffusion vol from 1 price = underdetermined).
    """
    if t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    # BTC-calibrated jump parameters
    lam = 2.0       # jumps per year (≈ 2 expected per hour is aggressive but BTC)
    mu_j = -0.002   # mean log jump size
    sig_j = 0.015   # jump size std dev
    r = 0.0         # risk-free rate ≈ 0 for minutes horizon
    # Risk-neutral jump intensity and drift adjustment
    lam_rn = lam * math.exp(mu_j + 0.5 * sig_j**2)
    # Compensator (keeps process martingale)
    k_bar = math.exp(mu_j + 0.5 * sig_j**2) - 1.0
    # For each term n (conditioning on n jumps occurring)
    prob = 0.0
    for n in range(n_terms):
        # Poisson weight
        lam_t = lam_rn * t_years
        try:
            poisson_w = math.exp(-lam_t) * (lam_t ** n) / math.factorial(n)
        except (OverflowError, ValueError):
            break
        # Effective vol and drift for this jump count
        sig_n = math.sqrt(sigma**2 + n * sig_j**2 / t_years) if t_years > 0 else sigma
        # Drift: r - lam*k_bar - sig²/2 + n*mu_j/T
        drift_n = r - lam * k_bar - 0.5 * sigma**2 + (n * mu_j / t_years if t_years > 0 else 0)
        d2_n = (math.log(spot / strike) + drift_n * t_years) / (sig_n * math.sqrt(t_years))
        prob += poisson_w * _phi(d2_n)
    return max(0.01, min(0.99, prob))


def _bridge_fbm(spot: float, strike: float, t_years: float,
                 sigma: float, hurst: float) -> float:
    """
    Fractional Brownian Motion binary probability.
    Under fBM, the effective 'time' is scaled by T^(2H) rather than T,
    and the process is no longer a martingale for H≠0.5. For H>0.5
    (momentum), the effective vol over [0,T] is σ*T^H (not σ*√T),
    making the distribution wider under trending conditions.

    Formally: ln(S_T/S_0) ~ N(μT, σ²T^{2H})
    Binary price: Φ((ln(S/K) + μT) / (σ * T^H))
    where μ = r - σ²T^{2H-1}/2 (Wick-Itô correction for fBM).
    """
    if t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    r = 0.0
    # fBM effective variance uses T^(2H)
    wick_correction = -0.5 * sigma**2 * (t_years ** (2.0 * hurst - 1.0)) if t_years > 0 else 0
    numerator = math.log(spot / strike) + (r + wick_correction) * t_years
    denominator = sigma * (t_years ** hurst)
    if denominator <= 0:
        return 0.5
    d = numerator / denominator
    return max(0.01, min(0.99, _phi(d)))


def _bridge_bachelier(spot: float, strike: float, t_years: float,
                       sigma_abs: float) -> float:
    """
    Bachelier (arithmetic BM) binary probability.
    dS = μ dt + σ_abs dW  (σ_abs in dollars, not %)
    P(S_T > K) = Φ((S - K + μT) / (σ_abs * √T))
    μ ≈ 0 (no drift assumed over a 15-min window; BTC spot has no
    predictable drift at this horizon independent of vol).

    σ_abs is the per-period dollar volatility, derived from the
    annualized percentage vol: σ_abs = spot * σ_pct * √(T/T_year)
    but kept as an annual dollar vol here and scaled by √T.
    """
    if t_years <= 0 or sigma_abs <= 0:
        return 0.5
    forward = spot  # no carry
    sigma_t = sigma_abs * math.sqrt(t_years)
    if sigma_t <= 0:
        return 0.5
    d = (forward - strike) / sigma_t
    return max(0.01, min(0.99, _phi(d)))


def _bridge_time_changed(spot: float, strike: float, t_years: float,
                          sigma: float, volume_ratio: float) -> float:
    """
    Time-changed / subordinated BM binary probability.
    Business time τ = t * volume_ratio, where volume_ratio =
    current_volume / recent_average_volume. High-volume periods
    'age' faster than clock time — more price information arrives
    per second — expanding effective vol:
        σ_eff = σ * √(volume_ratio)
    The binary probability is then a standard normal CDF with this
    expanded vol:
        P(S_T > K) = Φ(d2) where d2 uses σ_eff instead of σ.

    This captures the empirical observation that during high-volume
    stretches (e.g. a news spike, whale print, or liquidation cascade),
    prices move much faster than the historical vol alone would predict
    over the same calendar time.
    """
    if t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    # Clamp volume ratio to avoid extreme distortion
    vol_ratio = max(0.25, min(4.0, volume_ratio))
    sigma_eff = sigma * math.sqrt(vol_ratio)
    r = 0.0
    sqrt_t = math.sqrt(t_years)
    if sigma_eff * sqrt_t <= 0:
        return 0.5
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma_eff**2) * t_years) / (sigma_eff * sqrt_t)
    return max(0.01, min(0.99, _phi(d2)))


def compute_bridge(spot: float, strike: float, yes_mid: float | None,
                    ohlcv_5m: list, seconds_remaining: int,
                    kalshi_yes: float | None = None) -> dict:
    """
    Master entry point for the Bridge tab. Runs all four quantitative
    models and blends them equally into a single probability estimate.

    Args:
        spot          : current CF Benchmarks / blended price
        strike        : current window's Kalshi floor_strike
        yes_mid       : Kalshi YES mid-price (0-1) for IV extraction;
                        None or outside (0.02, 0.98) → fall back to realized vol
        ohlcv_5m      : recent 5-minute OHLCV candles for vol/volume/Hurst
        seconds_remaining: seconds until this window closes
        kalshi_yes    : alias for yes_mid (one of the two must be set)

    Returns a dict with:
        blended_prob     : float — P(settlement > strike), [0.01, 0.99]
        implied_vol      : float — annualized IV (from Kalshi or realized)
        iv_source        : "kalshi_implied" | "realized"
        hurst            : float — estimated Hurst exponent
        volume_ratio     : float — current/recent volume ratio
        model_probs      : dict  — per-model probability breakdown
        signal           : "ABOVE" | "BELOW" | "NEUTRAL"  — directional call
        confidence       : "HIGH" | "MEDIUM" | "LOW"
        note             : str — human-readable summary
        settlement_check : str — post-hoc check note (only meaningful after close)
    """
    result = {
        "blended_prob": 0.5, "implied_vol": 0.0, "iv_source": "realized",
        "hurst": 0.55, "volume_ratio": 1.0,
        "model_probs": {"merton": 0.5, "fbm": 0.5, "bachelier": 0.5, "time_changed": 0.5},
        "signal": "NEUTRAL", "confidence": "LOW",
        "note": "insufficient data", "settlement_check": "n/a",
    }

    if not ohlcv_5m or len(ohlcv_5m) < 5 or spot <= 0 or not strike or strike <= 0:
        result["note"] = "insufficient price/strike data"
        return result

    t_years = max(seconds_remaining, 10) / (365.25 * 24 * 3600)
    closes = [c[4] for c in ohlcv_5m]
    volumes = [c[5] for c in ohlcv_5m if len(c) > 5]

    # ── Implied Volatility ──────────────────────────────────────────────────
    # BTC's annualized realized vol is typically 50-120%. A floor of 40% is
    # very conservative — at 15-minute resolution BTC almost never has
    # genuine annualized vol below this. Without a floor, a calm 30-minute
    # window can produce sigma_hist=5-15%, which collapses all four models
    # to 0% or 100% probabilities for any gap > 0.1% from the strike.
    BTC_MIN_ANNUAL_VOL = 0.40   # 40% annualized minimum for BTC
    sigma_hist = max(_realized_vol_annualized(closes), BTC_MIN_ANNUAL_VOL)
    yes_price = yes_mid if yes_mid is not None else kalshi_yes
    sigma_iv = _extract_iv_from_binary(yes_price or 0.0, spot, strike, t_years, sigma_hist)
    # Apply the same floor to extracted IV
    sigma_iv = max(sigma_iv, BTC_MIN_ANNUAL_VOL)
    iv_source = "kalshi_implied" if (yes_price and 0.02 < yes_price < 0.98) else "realized"
    # Blend IV and historical for stability: 60/40 IV/hist when IV available
    sigma = (0.6 * sigma_iv + 0.4 * sigma_hist) if iv_source == "kalshi_implied" else sigma_hist
    # Final floor after blending
    sigma = max(sigma, BTC_MIN_ANNUAL_VOL)

    # ── Hurst Exponent ──────────────────────────────────────────────────────
    hurst = _estimate_hurst(closes)

    # ── Volume Ratio ────────────────────────────────────────────────────────
    volume_ratio = 1.0
    if volumes and len(volumes) >= 5:
        recent_vol = volumes[-1] if volumes else 1.0
        avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if volumes else 1.0
        volume_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    # ── σ_abs for Bachelier (convert from % to $ vol) ──────────────────────
    # σ_abs_annual = spot * σ_pct (annual)
    sigma_abs = spot * sigma

    # ── Run Four Models ─────────────────────────────────────────────────────
    p_merton = _bridge_merton_jump_diffusion(spot, strike, t_years, sigma)
    p_fbm = _bridge_fbm(spot, strike, t_years, sigma, hurst)
    p_bach = _bridge_bachelier(spot, strike, t_years, sigma_abs)
    p_tc = _bridge_time_changed(spot, strike, t_years, sigma, volume_ratio)

    # ── Equal-weight blend ──────────────────────────────────────────────────
    blended = (p_merton + p_fbm + p_bach + p_tc) / 4.0

    # ── Confidence from model spread ────────────────────────────────────────
    # Tight agreement → higher confidence. Wide spread → models disagree
    # about the regime → lower confidence. Threshold: ±0.08 from mean.
    probs = [p_merton, p_fbm, p_bach, p_tc]
    spread = max(probs) - min(probs)
    if spread < 0.08:
        confidence = "HIGH"
    elif spread < 0.18:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # ── Signal ───────────────────────────────────────────────────────────────
    if blended >= 0.55:
        signal = "ABOVE"
    elif blended <= 0.45:
        signal = "BELOW"
    else:
        signal = "NEUTRAL"

    # ── Settlement check note ───────────────────────────────────────────────
    settlement_check = (
        "⏳ window still live — compare after close"
        if seconds_remaining > 0 else
        "window closed — check settlement vs strike"
    )

    note = (
        f"IV {sigma*100:.1f}% ({iv_source.replace('_',' ')})  "
        f"H={hurst:.2f}  vol×{volume_ratio:.1f}  "
        f"spread {spread:.2f}  →  {blended*100:.1f}% prob ABOVE"
    )

    result.update({
        "blended_prob": round(blended, 4),
        "implied_vol": round(sigma, 4),
        "iv_source": iv_source,
        "hurst": round(hurst, 3),
        "volume_ratio": round(volume_ratio, 2),
        "model_probs": {
            "merton": round(p_merton, 4),
            "fbm": round(p_fbm, 4),
            "bachelier": round(p_bach, 4),
            "time_changed": round(p_tc, 4),
        },
        "signal": signal,
        "confidence": confidence,
        "note": note,
        "settlement_check": settlement_check,
    })
    return result


# ── Physics Module: BTC Price as a Dynamical System ──────────────────────────
#
# CONCEPTUAL FRAMEWORK
# --------------------
# We treat BTC's 15-minute price evolution as a driven, damped nonlinear
# oscillator subject to stochastic forcing — the same class of system that
# describes everything from a pendulum in a fluid to a nanoparticle in
# Brownian motion. The "definitive" physics solution to such a system is the
# Fokker-Planck equation, which gives a PROBABILITY DENSITY over all possible
# settlement prices — not a single number, because the system is stochastic.
#
# The price at settlement S(T) evolves under:
#
#   dS = [μ(S,t) - γ(S - S_eq)] dt + σ(S,t) dW + J dN   ... (1)
#
# where:
#   μ(S,t)  = deterministic drift (momentum, estimated from recent returns)
#   γ        = damping coefficient (mean-reversion strength, from ATR/Bollinger)
#   S_eq     = equilibrium price (VWAP, the "potential minimum")
#   σ(S,t)  = local volatility (regime-dependent, from realized vol)
#   dW       = Wiener process (continuous random noise)
#   J dN     = jump component (Poisson, from order-flow imbalance / whale data)
#
# This is a Langevin equation. Its Fokker-Planck (FP) counterpart gives the
# evolution of the probability density P(S,t):
#
#   ∂P/∂t = -∂/∂S[A(S)P] + ½ ∂²/∂S²[D(S)P]             ... (2)
#
# where A(S) = μ(S,t) - γ(S - S_eq) is the drift and D(S) = σ² is the
# diffusion coefficient. We solve (2) for the STATIONARY distribution P*(S)
# at t=T (window close), then integrate to get P(S_T > K) for the strike K.
#
# PARAMETERS (all derived from live market data, not hardcoded):
#
#   γ (damping) — estimated from how quickly price reverts to VWAP. Strong
#       Bollinger compression → strong damping (restoring force). Measured
#       as the ratio of the Bollinger mean-reversion speed to the ATR.
#
#   S_eq (equilibrium) — VWAP over the current window. In a physical oscillator
#       this is the "rest position" the system is attracted toward. Price far
#       from VWAP experiences a stronger restoring force (spring-like).
#
#   μ (drift/forcing) — EMA(9) minus EMA(21) slope, scaled to price/second.
#       This is the "external driving force" — a persistent push in one
#       direction from momentum. High μ can overcome γ temporarily (like
#       pushing an oscillator off its equilibrium — it still returns, just
#       with a different center).
#
#   σ (diffusion) — realized vol from the last 20 candles, scaled to the
#       remaining window time. Represents thermal noise in the system.
#
#   J, λ (jumps) — from order-flow imbalance (whale detector's trade_score
#       and book_score). A large positive J means a buying surge is likely
#       to displace S(T) above what diffusion alone would predict.
#
# HAMILTONIAN DECOMPOSITION
# -------------------------
# We also compute the system's "Hamiltonian" (total energy):
#   H = KE + PE
#   KE = ½ m v²  where v = current price velocity (rate of change)
#   PE = ½ γ (S - S_eq)²  (harmonic potential well — spring energy)
#
# This decomposes the price system into how much of its motion is "kinetic"
# (momentum-driven, likely to continue) vs "potential" (stored restoring
# force, likely to pull price back). High KE/PE ratio → momentum dominant.
# High PE/KE ratio → mean-reversion dominant.
#
# MOST PROBABLE PATH (action minimization)
# -----------------------------------------
# The most probable trajectory from S(0) to S(T) is the one that minimizes
# the Onsager-Machlup action:
#   S_action = ∫₀ᵀ (dS/dt - A(S))² / (2D) dt
# For a linear drift A(S) = μ - γ(S - S_eq), the minimum-action path is an
# exponential relaxation toward equilibrium:
#   S*(t) = S_eq + (S(0) - S_eq)e^{-γt} + (μ/γ)(1 - e^{-γt})
# This gives the "physics prediction" for the most likely settlement price.
#
# FOKKER-PLANCK STATIONARY SOLUTION
# -----------------------------------
# For the Ornstein-Uhlenbeck process (linear drift + constant diffusion),
# the FP stationary solution is a Gaussian:
#   P*(S) = N(μ/γ + S_eq, σ²/(2γ))   [for γ > 0]
#   P*(S) = N(S₀ + μT, σ²T)            [for γ ≈ 0, free diffusion]
# The mean IS the most probable path endpoint S*(T), and the variance is
# determined by the balance between diffusion and damping.
#
# ALL FOUR OUTPUTS:
#   1. Most probable settlement price (the "physics prediction")
#   2. Full Gaussian probability density P*(S) evaluated at K → P(S_T > K)
#   3. Hamiltonian decomposition (KE/PE ratio, regime indicator)
#   4. Jump-corrected settlement estimate (adds expected jump displacement)

def compute_physics(spot: float, strike: float, ohlcv_5m: list,
                     seconds_remaining: int,
                     whale_trade_score: float = 0.0,
                     whale_book_score: float = 0.0) -> dict:
    """
    Physics-based settlement price estimator using Langevin/Fokker-Planck
    dynamics. All parameters are derived live from market data.

    Args:
        spot              : current CF Benchmarks price
        strike            : current window's floor_strike
        ohlcv_5m          : recent 5m candles [ts, o, h, l, c, v]
        seconds_remaining : seconds until window close
        whale_trade_score : signed [-1,1] from detect_whale_activity trade flow
        whale_book_score  : signed [-1,1] from detect_whale_activity book imbalance

    Returns dict with:
        most_probable_price  : float — the physics prediction for S(T)
        prob_above_strike    : float — P(S_T > K) from Fokker-Planck
        signal               : "ABOVE" | "BELOW" | "NEUTRAL"
        confidence           : "HIGH" | "MEDIUM" | "LOW"
        ke                   : float — kinetic energy (momentum component)
        pe                   : float — potential energy (mean-reversion pull)
        ke_pe_ratio          : float — KE/PE, >1 means momentum dominant
        regime               : "MOMENTUM" | "MEAN-REVERTING" | "BALANCED"
        gamma                : float — damping coefficient
        mu                   : float — drift (external forcing)
        s_eq                 : float — equilibrium price (VWAP)
        sigma                : float — diffusion coefficient (σ per √second)
        jump_displacement    : float — expected jump contribution to S(T)
        note                 : str
    """
    EMPTY = {
        "most_probable_price": spot or 0.0, "prob_above_strike": 0.5,
        "signal": "NEUTRAL", "confidence": "LOW",
        "ke": 0.0, "pe": 0.0, "ke_pe_ratio": 1.0, "regime": "BALANCED",
        "gamma": 0.0, "mu": 0.0, "s_eq": spot or 0.0, "sigma": 0.0,
        "jump_displacement": 0.0, "note": "insufficient data",
    }
    if not ohlcv_5m or len(ohlcv_5m) < 10 or spot <= 0 or not strike or strike <= 0:
        return EMPTY

    closes = [c[4] for c in ohlcv_5m]
    T = max(seconds_remaining, 5)   # seconds remaining

    # ── Equilibrium price S_eq = VWAP ──────────────────────────────────────
    s_eq = calc_vwap(ohlcv_5m[-20:]) if len(ohlcv_5m) >= 20 else spot

    # ── Drift μ (external forcing, price/second) ────────────────────────────
    # Use the ACTUAL realized price velocity over the last 25 minutes (5 × 5m
    # candles = 1500 seconds) as the drift estimate. This is physically correct:
    # μ = ΔS/Δt in genuine dollars/second, not an EMA spread misread as a rate.
    # We use a 25-minute lookback (5 candles) rather than 1 candle to smooth
    # out single-bar noise while still capturing the current directional bias.
    # Critically, we DECAY this drift: BTC trends don't persist indefinitely
    # at their current rate — the longer the remaining window, the more the
    # drift should attenuate toward zero. We use a half-life of ~3 minutes
    # (180 seconds), meaning a trend that's been running for 9 minutes has
    # already decayed to (0.5)^(3) = 12.5% of its initial magnitude. This
    # is the primary fix for the "consistently wrong when market reverses"
    # failure mode: the original implementation used full undecayed drift
    # which amplified momentum errors catastrophically in reversing markets.
    DRIFT_HALFLIFE_SECONDS = 180.0   # trend momentum halves every ~3 minutes
    DRIFT_LOOKBACK_CANDLES = 5       # 5 × 5m = 25 minutes of realized velocity
    if len(closes) >= DRIFT_LOOKBACK_CANDLES + 1:
        lookback_secs = DRIFT_LOOKBACK_CANDLES * 300.0
        raw_velocity = (closes[-1] - closes[-(DRIFT_LOOKBACK_CANDLES + 1)]) / lookback_secs
    elif len(closes) >= 2:
        raw_velocity = (closes[-1] - closes[-2]) / 300.0
    else:
        raw_velocity = 0.0
    # Decay the drift toward zero over the remaining window using exponential
    # decay: μ_effective = μ_raw × e^(-T/halflife). This is the integral-
    # averaged drift over [0,T] for a process whose momentum is decaying.
    decay_factor = math.exp(-T / DRIFT_HALFLIFE_SECONDS)
    # Effective mean drift over remaining T seconds (integrated, not terminal)
    if T > 0:
        mu_per_sec = raw_velocity * (DRIFT_HALFLIFE_SECONDS / T) * (1.0 - decay_factor)
    else:
        mu_per_sec = 0.0

    # ── Damping γ (mean-reversion coefficient, 1/second) ───────────────────
    # Physical interpretation: γ = 1/τ where τ is the mean-reversion
    # timescale. We estimate τ from the Bollinger band width ratio and ATR:
    # narrow bands (price compressed near mean) → strong restoring force →
    # large γ. Wide bands → weak restoring force → small γ.
    atr_val = calc_atr(ohlcv_5m, 14)
    boll = bollinger(closes, 20, 2.0)
    if boll and atr_val > 0:
        bb_width = (boll[0] - boll[2]) / spot   # fractional Bollinger width
        # γ scales inversely with BB width: tight bands → fast reversion.
        # Bounds: min = 1/900 (~15-min reversion, slow) to max = 1/120 (~2-min,
        # very fast). Previous floor of 1e-4 (~2.7-hour timescale) was too slow
        # and caused drift to dominate over mean-reversion.
        gamma = max(1/900, min(1/120, 0.003 / max(bb_width, 0.001)))
    else:
        gamma = 1/300   # default: ~5-min reversion timescale

    # ── Diffusion σ (volatility per √second, in dollars) ───────────────────
    # σ_annual_pct → σ_per_sec in dollars:
    # σ_dollar/sec = spot * σ_annual / √(seconds_per_year)
    sigma_annual = _realized_vol_annualized(closes)
    sigma_per_sqrt_sec = spot * sigma_annual / math.sqrt(365.25 * 24 * 3600)

    # ── MOST PROBABLE PATH: Ornstein-Uhlenbeck minimum-action solution ─────
    # S*(T) = S_eq + (S₀ - S_eq)e^{-γT} + (μ/γ)(1 - e^{-γT})
    # This is the analytic solution to the deterministic part of the Langevin
    # equation (ignoring noise, finding where the "center of mass" of the
    # probability density ends up at time T).
    exp_decay = math.exp(-gamma * T)
    if gamma > 1e-6:
        s_star = s_eq + (spot - s_eq) * exp_decay + (mu_per_sec / gamma) * (1.0 - exp_decay)
    else:
        # γ → 0 limit: free Brownian motion with drift
        s_star = spot + mu_per_sec * T

    # ── JUMP DISPLACEMENT ──────────────────────────────────────────────────
    # Expected displacement from jumps over remaining window.
    # Jump intensity from whale signals: combined score → expected # jumps.
    # Mean jump size proportional to combined score × ATR.
    combined_flow = (whale_trade_score + whale_book_score) / 2.0
    jump_size_expected = combined_flow * atr_val * 0.5  # conservative: 50% of ATR
    # Poisson mean: λ*T where λ = 2/hour = 2/3600 per second
    lam_per_sec = 2.0 / 3600.0
    expected_n_jumps = lam_per_sec * T
    jump_displacement = expected_n_jumps * jump_size_expected
    s_star_jump = s_star + jump_displacement

    # ── FOKKER-PLANCK: Gaussian P*(S_T) ────────────────────────────────────
    # For OU process the marginal distribution at time T is Gaussian:
    #   P(S_T) = N(s_star, variance_T)
    # Variance = σ²/(2γ) * (1 - e^{-2γT})  [OU exact result]
    # In the γ→0 limit: variance = σ² * T  [standard diffusion]
    if gamma > 1e-6:
        variance_T = (sigma_per_sqrt_sec**2 / (2.0 * gamma)) * (1.0 - math.exp(-2.0 * gamma * T))
    else:
        variance_T = sigma_per_sqrt_sec**2 * T

    # Minimum uncertainty floor based on ATR — much more realistic than $1.
    # The ATR (Average True Range) is a direct market-based estimate of how
    # much BTC actually moves per candle period. Over T seconds remaining,
    # the minimum plausible price uncertainty is at least 0.5 * ATR, since
    # BTC regularly moves by a full ATR or more in any 5-minute window.
    # Without this floor, a calm 30-minute window produces σ_T ~$13
    # and collapses all probabilities to 0% or 100% for any non-trivial gap.
    # A $1 floor (the old value) is absurdly tight for a $100k asset.
    min_std_T = max(atr_val * 0.5, spot * 0.0005)  # 0.5× ATR or 0.05% of spot, whichever larger
    variance_T = max(variance_T, min_std_T ** 2)
    std_T = math.sqrt(variance_T)

    # P(S_T > K) = Φ((s_star_jump - K) / std_T)
    prob_above = _phi((s_star_jump - strike) / std_T) if std_T > 0 else 0.5

    # ── HAMILTONIAN DECOMPOSITION ───────────────────────────────────────────
    # Kinetic energy: KE = ½ m v²
    # Mass m set to 1 (normalized). Velocity from most recent 5m return:
    price_velocity = (closes[-1] - closes[-2]) / 300.0 if len(closes) >= 2 else 0.0  # $/sec
    ke = 0.5 * price_velocity**2
    # Potential energy (harmonic oscillator): PE = ½ γ (S - S_eq)²
    pe = 0.5 * gamma * (spot - s_eq)**2
    total_energy = ke + pe
    ke_pe_ratio = (ke / pe) if pe > 1e-10 else (float("inf") if ke > 0 else 1.0)

    if ke_pe_ratio > 2.0:
        regime = "MOMENTUM"
    elif ke_pe_ratio < 0.5:
        regime = "MEAN-REVERTING"
    else:
        regime = "BALANCED"

    # ── Signal & Confidence ────────────────────────────────────────────────
    if prob_above >= 0.57:
        signal = "ABOVE"
    elif prob_above <= 0.43:
        signal = "BELOW"
    else:
        signal = "NEUTRAL"

    # Confidence: higher when damping is strong (physics cleaner), when
    # momentum and mean-reversion agree, or when std_T is small relative
    # to the s_star → strike gap.
    gap_z = abs(s_star_jump - strike) / std_T if std_T > 0 else 0.0
    if gap_z >= 1.0 and (ke_pe_ratio < 0.5 or ke_pe_ratio > 2.0):
        confidence = "HIGH"
    elif gap_z >= 0.5:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    note = (
        f"S*(T)=${s_star_jump:,.2f} vs K=${strike:,.2f}  "
        f"σ_T=${std_T:,.2f}  γ={gamma:.4f}/s  "
        f"regime={regime}  KE/PE={ke_pe_ratio:.2f}"
    )

    return {
        "most_probable_price": round(s_star_jump, 2),
        "prob_above_strike": round(prob_above, 4),
        "signal": signal,
        "confidence": confidence,
        "ke": round(ke, 6),
        "pe": round(pe, 6),
        "ke_pe_ratio": round(ke_pe_ratio, 3),
        "regime": regime,
        "gamma": round(gamma, 6),
        "mu": round(mu_per_sec, 4),
        "s_eq": round(s_eq, 2),
        "sigma": round(sigma_per_sqrt_sec, 4),
        "jump_displacement": round(jump_displacement, 2),
        "note": note,
    }



# Honest scope: this reads EXCHANGE order flow (public trade tape + order
# book), not on-chain wallet movements. There is no free real-time feed of
# actual whale wallets for any of EXCHANGE_WEIGHTS, so "whale" here means
# "unusually large exchange order flow relative to that symbol's own recent
# activity" — which is the standard, legitimate definition retail tools use,
# but it is not the same claim as "a specific known whale wallet moved BTC".
#
# Two signals are blended:
#   1. Large-trade scan — recent public trades (~60s) that are statistically
#      large vs. that exchange's own recent trade-size distribution (top
#      ~1-2% by size), netted buy-vs-sell.
#   2. Order-book imbalance — resting bid vs ask volume within a tight band
#      of the mid price, which reflects standing large orders ("walls").
# Both are blended across exchanges using the SAME EXCHANGE_WEIGHTS used for
# price everywhere else in the bot, so a thin/quiet exchange can't dominate
# the read.

WHALE_TRADE_LOOKBACK_SECONDS = 180  # how far back to scan the public trade tape
# Widened from 60s: a 60s window can fully age out a real large trade by the
# time the next 5s poll happens if you're not staring at the exact second it
# printed, and it can't see a whale trade that helped form a 5m/15m candle if
# that trade happened more than a minute before the poll. 180s is still
# "recent" for a 15-min Kalshi window but gives genuine large prints enough
# runway to actually get caught before the detector's view of them expires.
WHALE_TRADE_PERCENTILE = 98         # a trade in the top (100-98)=2% by size counts as "large"
WHALE_BOOK_DEPTH_PCT = 0.15         # % band around mid price counted as "near the market"
WHALE_DRIFT_DAMPING = 0.25          # same spirit as estimate_next_strike's drift damping


def _scan_trades_for_whales(ex, symbol: str) -> dict | None:
    """
    Pull recent public trades from one exchange and find ones that are
    statistically large for THAT exchange right now. Returns None on any
    fetch failure (caller drops this exchange from the blend, same pattern
    as fetch_blended_ohlcv). Returns:
      {"net_large_volume": float,  # signed: +buy-side, -sell-side, in BTC
       "large_buy_usd": float, "large_sell_usd": float,
       "n_large": int, "n_total": int}
    """
    try:
        trades = ex.fetch_trades(symbol, limit=200)
    except Exception:
        return None
    if not trades:
        return None

    cutoff_ms = (time.time() - WHALE_TRADE_LOOKBACK_SECONDS) * 1000
    recent = [t for t in trades if t.get("timestamp") and t["timestamp"] >= cutoff_ms]
    if len(recent) < 5:
        # Not enough recent flow to say anything statistically meaningful.
        return {"net_large_volume": 0.0, "large_buy_usd": 0.0, "large_sell_usd": 0.0,
                "n_large": 0, "n_total": len(recent)}

    sizes = sorted(t.get("amount", 0.0) for t in recent if t.get("amount"))
    if not sizes:
        return {"net_large_volume": 0.0, "large_buy_usd": 0.0, "large_sell_usd": 0.0,
                "n_large": 0, "n_total": len(recent)}

    idx = min(len(sizes) - 1, int(len(sizes) * WHALE_TRADE_PERCENTILE / 100))
    threshold = sizes[idx]
    median = sizes[len(sizes) // 2]
    # Guard against degenerate/low-variance tapes (e.g. a quiet exchange where
    # most trades cluster near the same small size): a percentile rank alone
    # isn't enough — require the threshold to be MEANINGFULLY above the
    # median, or every trade in a uniform tape would incorrectly count as
    # "large" just by rank position. Lowered from 3x to 2.2x median: 3x was
    # also suppressing real moves driven by several moderately-elevated
    # trades clustered together rather than one dominant outlier — 2.2x still
    # blocks uniform/degenerate tapes but lets a genuine cluster of bigger
    # prints through.
    MEDIAN_FLOOR_MULT = 2.2
    if threshold <= 0 or threshold < median * MEDIAN_FLOOR_MULT:
        return {"net_large_volume": 0.0, "large_buy_usd": 0.0, "large_sell_usd": 0.0,
                "n_large": 0, "n_total": len(recent)}

    net_vol = 0.0
    buy_usd = 0.0
    sell_usd = 0.0
    n_large = 0
    for t in recent:
        amt = t.get("amount", 0.0) or 0.0
        if amt < threshold:
            continue
        price = t.get("price", 0.0) or 0.0
        side = t.get("side")  # "buy" or "sell" — taker side, ccxt-normalized
        n_large += 1
        if side == "buy":
            net_vol += amt
            buy_usd += amt * price
        elif side == "sell":
            net_vol -= amt
            sell_usd += amt * price

    return {"net_large_volume": net_vol, "large_buy_usd": buy_usd,
            "large_sell_usd": sell_usd, "n_large": n_large, "n_total": len(recent)}


def _scan_orderbook_imbalance(ex, symbol: str) -> dict | None:
    """
    Pull the current order book from one exchange and measure bid vs ask
    volume within WHALE_BOOK_DEPTH_PCT of the mid price — i.e. standing
    size close enough to the market to matter in the next few minutes, not
    deep resting orders far from price. Returns None on fetch failure.
      {"bid_volume": float, "ask_volume": float, "mid": float}
    """
    try:
        book = ex.fetch_order_book(symbol, limit=50)
    except Exception:
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    if not best_bid or not best_ask:
        return None
    mid = (best_bid + best_ask) / 2.0
    band = mid * WHALE_BOOK_DEPTH_PCT / 100.0
    bid_vol = sum(amt for price, amt in bids if price >= mid - band)
    ask_vol = sum(amt for price, amt in asks if price <= mid + band)
    return {"bid_volume": bid_vol, "ask_volume": ask_vol, "mid": mid}


# ── Synthesis Model: Combined Signal ─────────────────────────────────────────
#
# PURPOSE: Combines the Bet tab's multi-indicator confluence model with the
# Bridge tab's quantitative pricing model and the Physics tab's Fokker-Planck
# dynamical model into a single weighted directional probability.
#
# WEIGHTING RATIONALE:
#   Momentum (40%): Merton Jump-Diffusion + Fractional BM
#       Both capture directional momentum — Merton adds jump bias from
#       order flow, fBM captures trend persistence via Hurst exponent.
#       These are the most directionally sensitive models.
#
#   Averaging (30%): Confluence model (10 indicators, weighted vote)
#       Literally an averaging methodology by construction — the most
#       stable and least noise-prone component, deserving high weight.
#
#   Distribution (20%): Physics (Fokker-Planck / OU process)
#       Gives the full probability distribution over terminal prices
#       from first principles — most complete but most sensitive to
#       parameter uncertainty, so slightly lower weight.
#
#   Supporting (10%): Bachelier + Time-Changed BM (averaged)
#       Fill out the blend without dominating; Bachelier handles
#       absolute-dollar moves, Time-Changed adjusts for volume intensity.
#
# WEIGHTS TOTAL: 40% + 30% + 20% + 10% = 100%

SYNTHESIS_WEIGHT_MOMENTUM    = 0.40   # Merton + fBM
SYNTHESIS_WEIGHT_AVERAGING   = 0.30   # Confluence model
SYNTHESIS_WEIGHT_DISTRIBUTION = 0.20  # Physics OU/Fokker-Planck
SYNTHESIS_WEIGHT_SUPPORTING  = 0.10   # Bachelier + Time-Changed


def compute_synthesis(spot: float, strike: float, yes_mid: float | None,
                       ohlcv_5m: list, ohlcv_15m: list | None,
                       ohlcv_1h: list | None, ohlcv_1m: list | None,
                       seconds_remaining: int,
                       kalshi_yes: float | None = None,
                       funding_rate: float | None = None,
                       whale_trade_score: float = 0.0,
                       whale_book_score: float = 0.0) -> dict:
    """
    Master synthesis function. Runs all three model families and blends
    them into a single P(settlement > strike) with explicit weights.

    Args:
        spot              : current CF Benchmarks price
        strike            : current Kalshi floor_strike
        yes_mid           : Kalshi YES mid-price for IV extraction
        ohlcv_5m          : 5m OHLCV candles (main series)
        ohlcv_15m/1h/1m   : additional timeframes for confluence model
        seconds_remaining : seconds until window close
        kalshi_yes        : alias for yes_mid
        funding_rate      : perpetual funding rate for confluence model
        whale_trade_score : signed [-1,1] from detect_whale_activity
        whale_book_score  : signed [-1,1] from detect_whale_activity

    Returns dict with:
        blended_prob      : float — final weighted P(S_T > K)
        signal            : "ABOVE" | "BELOW" | "NEUTRAL"
        confidence        : "HIGH" | "MEDIUM" | "LOW"
        component_probs   : dict — per-component probability before weighting
        weights_used      : dict — actual weights applied
        confluence_conf   : str  — confluence model confidence label
        confluence_prob   : float
        bridge_prob       : float — Bridge tab blended prob
        physics_prob      : float — Physics tab prob
        note              : str
    """
    EMPTY = {
        "blended_prob": 0.5, "signal": "NEUTRAL", "confidence": "LOW",
        "component_probs": {}, "weights_used": {},
        "confluence_conf": "—", "confluence_prob": 0.5,
        "bridge_prob": 0.5, "physics_prob": 0.5, "note": "insufficient data",
    }

    if not ohlcv_5m or len(ohlcv_5m) < 10 or spot <= 0 or not strike or strike <= 0:
        return EMPTY

    closes = [c[4] for c in ohlcv_5m]
    volumes = [c[5] for c in ohlcv_5m]

    # ── 1. Confluence model (Averaging component, 30%) ───────────────────
    conf_prob = 0.5
    conf_conf = "LEAN"
    try:
        votes = analyze(closes, volumes, ohlcv_5m, ohlcv_15m, ohlcv_1h,
                        ohlcv_1m, yes_mid or kalshi_yes, funding_rate, strike)
        conf_dir, _, conf_conf, conf_pct = recommendation(votes, spot)

        # FIXED: conf_pct from recommendation() is a discrete tier label
        # (55/60/64/68) not a true probability — it doesn't reflect how
        # strongly the indicators actually agreed, only which bucket they
        # fell into. Using it directly compresses the confluence component
        # into a ±0.18 band around 0.50 regardless of actual agreement
        # strength, letting the wider-ranging momentum models dominate the
        # blend. Instead, derive a genuine continuous probability from the
        # raw vote score: map the weighted score to [0.01, 0.99] using a
        # logistic function so that strong consensus (high |score|) pulls
        # the probability toward the extremes proportionally.
        decisive = [v for v in votes if v.direction != 0]
        raw_score = sum(v.direction * v.weight for v in decisive) if decisive else 0.0
        max_score = sum(v.weight for v in votes)   # theoretical maximum
        normalized = raw_score / max_score if max_score > 0 else 0.0
        # Logistic scaling: k=4 gives ~0.98 at normalized=1.0 and
        # ~0.73 at normalized=0.5 — wide enough to be meaningful but
        # not as extreme as the pure quant models.
        import math as _math
        conf_prob = 1.0 / (1.0 + math.exp(-4.0 * normalized))
        conf_prob = max(0.01, min(0.99, conf_prob))
    except Exception:
        pass

    # ── 2. Bridge sub-components ─────────────────────────────────────────
    # Run Bridge to get individual model probabilities, not just the blend.
    t_years = max(seconds_remaining, 10) / (365.25 * 24 * 3600)
    BTC_MIN_VOL = 0.40
    sigma_hist = max(_realized_vol_annualized(closes), BTC_MIN_VOL)
    yes_price = yes_mid if yes_mid is not None else kalshi_yes
    sigma_iv = _extract_iv_from_binary(yes_price or 0.0, spot, strike, t_years, sigma_hist)
    sigma_iv = max(sigma_iv, BTC_MIN_VOL)
    sigma = (0.6 * sigma_iv + 0.4 * sigma_hist) if (yes_price and 0.02 < yes_price < 0.98) else sigma_hist
    sigma = max(sigma, BTC_MIN_VOL)
    hurst = _estimate_hurst(closes)
    vol_ratio = 1.0
    if len(ohlcv_5m) >= 5:
        vols = [c[5] for c in ohlcv_5m if len(c) > 5]
        if vols:
            avg_v = sum(vols[-20:]) / len(vols[-20:])
            vol_ratio = vols[-1] / avg_v if avg_v > 0 else 1.0
    sigma_abs = spot * sigma

    p_merton = _bridge_merton_jump_diffusion(spot, strike, t_years, sigma)
    p_fbm    = _bridge_fbm(spot, strike, t_years, sigma, hurst)
    p_bach   = _bridge_bachelier(spot, strike, t_years, sigma_abs)
    p_tc     = _bridge_time_changed(spot, strike, t_years, sigma, vol_ratio)

    # Momentum component: Merton + fBM equally averaged
    p_momentum = (p_merton + p_fbm) / 2.0
    # Supporting component: Bachelier + Time-Changed equally averaged
    p_supporting = (p_bach + p_tc) / 2.0
    # Full bridge blend (for reference)
    bridge_prob = (p_merton + p_fbm + p_bach + p_tc) / 4.0

    # ── 3. Physics model (Distribution component, 20%) ───────────────────
    physics_prob = 0.5
    try:
        phys = compute_physics(spot, strike, ohlcv_5m, seconds_remaining,
                                whale_trade_score, whale_book_score)
        physics_prob = phys["prob_above_strike"]
    except Exception:
        pass

    # ── 4. Weighted blend ────────────────────────────────────────────────
    blended = (
        SYNTHESIS_WEIGHT_MOMENTUM    * p_momentum    +
        SYNTHESIS_WEIGHT_AVERAGING   * conf_prob     +
        SYNTHESIS_WEIGHT_DISTRIBUTION * physics_prob  +
        SYNTHESIS_WEIGHT_SUPPORTING  * p_supporting
    )
    blended = max(0.01, min(0.99, blended))

    # ── 5. Signal and confidence ─────────────────────────────────────────
    # Neutral band widened from 0.45-0.55 to 0.40-0.60: the quant models
    # (Physics, Merton, fBM) naturally converge toward 0.50 as T→0 when
    # price is near the strike, producing spurious NEUTRAL calls in the
    # last 2 minutes even when the confluence model has a clear lean.
    # A wider band requires a genuinely stronger signal before calling
    # ABOVE/BELOW, which reduces the last-minute NEUTRAL problem while
    # still keeping the call honest when conviction is truly low.
    if blended >= 0.60:
        signal = "ABOVE"
    elif blended <= 0.40:
        signal = "BELOW"
    else:
        signal = "NEUTRAL"

    # Confidence from spread across all four components
    all_probs = [p_momentum, conf_prob, physics_prob, p_supporting]
    spread = max(all_probs) - min(all_probs)
    if spread < 0.10 and blended > 0.56 or blended < 0.44:
        confidence = "HIGH"
    elif spread < 0.20:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    note = (
        f"Momentum {p_momentum*100:.1f}% (×{SYNTHESIS_WEIGHT_MOMENTUM:.0%})  "
        f"Avg {conf_prob*100:.1f}% (×{SYNTHESIS_WEIGHT_AVERAGING:.0%})  "
        f"Dist {physics_prob*100:.1f}% (×{SYNTHESIS_WEIGHT_DISTRIBUTION:.0%})  "
        f"Supp {p_supporting*100:.1f}% (×{SYNTHESIS_WEIGHT_SUPPORTING:.0%})  "
        f"→ {blended*100:.1f}%"
    )

    return {
        "blended_prob": round(blended, 4),
        "signal": signal,
        "confidence": confidence,
        "component_probs": {
            "momentum":    round(p_momentum, 4),
            "averaging":   round(conf_prob, 4),
            "distribution": round(physics_prob, 4),
            "supporting":  round(p_supporting, 4),
        },
        "weights_used": {
            "momentum": SYNTHESIS_WEIGHT_MOMENTUM,
            "averaging": SYNTHESIS_WEIGHT_AVERAGING,
            "distribution": SYNTHESIS_WEIGHT_DISTRIBUTION,
            "supporting": SYNTHESIS_WEIGHT_SUPPORTING,
        },
        "confluence_conf": conf_conf,
        "confluence_prob": round(conf_prob, 4),
        "bridge_prob": round(bridge_prob, 4),
        "physics_prob": round(physics_prob, 4),
        "merton_prob": round(p_merton, 4),
        "fbm_prob": round(p_fbm, 4),
        "bachelier_prob": round(p_bach, 4),
        "tc_prob": round(p_tc, 4),
        "implied_vol": round(sigma, 4),
        "hurst": round(hurst, 3),
        "note": note,
    }


def detect_whale_activity(exchanges: dict, symbol: str) -> dict:
    """
    Blend large-trade flow + order-book imbalance across every exchange in
    `exchanges` (same dict shape as build_exchanges()'s return: {name:
    (ccxt_instance, weight)}), weighted the same way price already is.

    Returns:
      {"state": "BUY" | "SELL" | "NONE",   # "lit up" state for the UI
       "expected_pct": float,               # signed % drift estimate, damped
       "confidence": "LOW"|"MEDIUM"|"HIGH", # how much real data backed this
       "note": str,                         # human-readable summary
       "per_exchange": dict}                # raw per-exchange reads, for /why-style detail

    expected_pct is NOT a price prediction in the same sense as the
    indicator confluence in analyze() — it's a damped estimate derived
    purely from order-flow imbalance, and is always presented as an
    estimate, not a fact, exactly like estimate_next_strike().
    """
    per_exchange = {}
    total_weight_seen = 0.0
    weighted_trade_net_usd = 0.0   # signed USD: + buy pressure, - sell pressure
    weighted_book_imbalance = 0.0  # signed ratio: + bid-heavy, - ask-heavy
    total_large_trades = 0
    mid_ref = None

    for name, (ex, weight) in exchanges.items():
        trade_read = _scan_trades_for_whales(ex, symbol)
        book_read = _scan_orderbook_imbalance(ex, symbol)
        per_exchange[name] = {"trades": trade_read, "book": book_read}
        if trade_read is None and book_read is None:
            continue
        total_weight_seen += weight

        if trade_read is not None:
            total_large_trades += trade_read["n_large"]
            net_usd = trade_read["large_buy_usd"] - trade_read["large_sell_usd"]
            weighted_trade_net_usd += net_usd * weight

        if book_read is not None:
            bid_v, ask_v = book_read["bid_volume"], book_read["ask_volume"]
            total_v = bid_v + ask_v
            if total_v > 0:
                imbalance = (bid_v - ask_v) / total_v  # +1 = all bids, -1 = all asks
                weighted_book_imbalance += imbalance * weight
            if mid_ref is None:
                mid_ref = book_read["mid"]

    if total_weight_seen == 0:
        return {"state": "NONE", "expected_pct": 0.0, "confidence": "LOW",
                "note": "no exchange data available", "per_exchange": per_exchange,
                "trade_score": 0.0, "book_score": 0.0}

    # Re-normalize over exchanges that actually returned data this call,
    # same convention as fetch_blended_ohlcv's weight re-normalization.
    weighted_trade_net_usd /= total_weight_seen
    weighted_book_imbalance /= total_weight_seen

    # Combine the two signals into one directional score in [-1, +1]:
    # trade flow sign (scaled by a soft cap so one huge print doesn't
    # saturate it) blended with book imbalance, equally weighted.
    TRADE_USD_SOFT_CAP = 250_000.0  # USD of net large-trade flow treated as "maxed out"
    trade_score = max(-1.0, min(1.0, weighted_trade_net_usd / TRADE_USD_SOFT_CAP))
    book_score = max(-1.0, min(1.0, weighted_book_imbalance))
    combined = (trade_score + book_score) / 2.0

    # State thresholds — small noise stays NONE (dark), only a real lean lights it up.
    if combined > 0.15:
        state = "BUY"
    elif combined < -0.15:
        state = "SELL"
    else:
        state = "NONE"

    # Damped expected % drift — same philosophy as estimate_next_strike's
    # DAMPING constant: never extrapolate the raw imbalance at full strength.
    expected_pct = combined * WHALE_DRIFT_DAMPING

    # Confidence reflects how much real data backed the read, not how
    # extreme the imbalance is — a strong signal from 1 exchange with 6
    # large trades is less trustworthy than a moderate one seen everywhere.
    if total_weight_seen >= 0.85 and total_large_trades >= 6:
        confidence = "HIGH"
    elif total_weight_seen >= 0.5 and total_large_trades >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if state == "NONE":
        note = f"no significant whale flow (score {combined:+.2f}) — {total_large_trades} large trades seen"
    else:
        side = "buying" if state == "BUY" else "selling"
        note = (f"whale {side} detected — trade flow {trade_score:+.2f}, "
                f"book imbalance {book_score:+.2f}, {total_large_trades} large trades, "
                f"{confidence.lower()} confidence")

    return {"state": state, "expected_pct": round(expected_pct, 4),
            "confidence": confidence, "note": note, "per_exchange": per_exchange,
            "trade_score": round(trade_score, 4), "book_score": round(book_score, 4)}


_KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2/markets"
_MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

# Kalshi's exact public hostname has drifted/varied across sources in the
# past. The ticker itself is built deterministically (_ticker_from_end), so
# we don't need to guess series names like an earlier prototype did — we
# only need a host fallback in case the primary one starts 404ing/erroring.
# Whichever host answers with a real market gets cached so every later call
# this session goes straight to it instead of re-trying all three.
_KALSHI_HOSTS = [
    "https://api.elections.kalshi.com/trade-api/v2/markets",
    "https://trading-api.kalshi.com/trade-api/v2/markets",
    "https://api.kalshi.com/trade-api/v2/markets",
]
_kalshi_working_host = [_KALSHI_API]  # mutable singleton; [0] is the live pick


def _kalshi_get_market(ticker: str) -> tuple[dict | None, int | None]:
    """GETs /markets/{ticker} against the cached host first, falling back
    through the others on failure. Returns (market_dict_or_None, http_status)."""
    hosts = [_kalshi_working_host[0]] + [h for h in _KALSHI_HOSTS if h != _kalshi_working_host[0]]
    last_status = None
    for host in hosts:
        try:
            resp = requests.get(f"{host}/{ticker}", timeout=5)
            last_status = resp.status_code
            if resp.status_code == 200:
                mkt = resp.json().get("market", {})
                if mkt:
                    _kalshi_working_host[0] = host  # remember the host that worked
                    return mkt, 200
            if resp.status_code == 404:
                continue  # try next host, this one doesn't have the market
        except Exception:
            continue
    return None, last_status


def fetch_kalshi_yes_price() -> tuple[float | None, float | None]:
    """
    Fetch the Kalshi YES mid price AND the settlement strike (target) price
    for the currently active KXBTC15M window.

    Returns (yes_mid, strike) where:
      yes_mid — float in [0,1] (e.g. 0.54 = 54% chance YES resolves)
      strike  — the CF Benchmarks BRTI target price (e.g. 75712.07)
    Either value may be None on failure.

    Settlement rule (from Kalshi): "the simple average of the sixty seconds
    of CF Benchmarks' BRTI before expiry". Our strike comes from floor_strike.

    Ticker format: KXBTC15M-{YY}{MON}{DD}{end_HHMM}-{end_min}  (ET, window END time)
    Example: KXBTC15M-26MAY272045-45
    """
    try:
        ticker = _ticker_from_end(_window_end_utc(0))
        mkt, status = _kalshi_get_market(ticker)
        if not mkt:
            return None, None
        bid_s = mkt.get("yes_bid_dollars")
        ask_s = mkt.get("yes_ask_dollars")
        if bid_s is None or ask_s is None:
            return None, None
        bid, ask = float(bid_s), float(ask_s)
        if bid <= 0 and ask <= 0:
            return None, None
        mid = (bid + ask) / 2.0
        # sanity: Kalshi prices are between 0.01 and 0.99
        if not (0.01 <= mid <= 0.99):
            return None, None
        # Strike price — the BRTI target BTC must reach for YES to resolve
        strike_raw = mkt.get("floor_strike") or mkt.get("strike") or mkt.get("cap_strike")
        strike = float(strike_raw) if strike_raw is not None else None
        # Reject illiquid markets: bid=0/ask=1 means no real price discovery.
        # A max-spread market gives useless 0.50 mid that would silently block
        # every signal through the confirmation gate. Treat it as no data.
        spread = ask - bid
        if spread > 0.80:
            print(f"[kalshi] {ticker}  illiquid (bid={bid:.2f} ask={ask:.2f} spread={spread:.2f}) — ignoring price"
                  + (f"  target=${strike:,.2f}" if strike else ""))
            return None, strike
        print(f"[kalshi] {ticker}  YES mid={mid:.2f}  bid={bid:.2f}  ask={ask:.2f}"
              + (f"  target=${strike:,.2f}" if strike else ""))
        return mid, strike
    except Exception as e:
        print(f"[kalshi] price fetch failed: {e}")
        return None, None


# ── Cross-asset trend tracker (SOL/XRP/HYPE/BNB/ETH/DOGE vs BTC) ────────────
# Kalshi lists 15-minute up/down markets for these assets in addition to BTC
# (ticker family KX{ASSET}15M, same shape as KXBTC15M). This module compares
# each asset's own short-term momentum against BTC's over the same lookback
# and classifies the relationship as FOLLOWING / REVERSED / DELAYED / NONE.
# This is a read on PRICE momentum correlation, not a Kalshi crowd-price
# comparison — Kalshi's own YES price isn't used for the classification,
# only to display each asset's own current strike/window alongside it.

ALT_ASSETS: list[dict] = [
    {"code": "SOL",  "symbol": "SOL/USD",  "label": "Solana"},
    {"code": "XRP",  "symbol": "XRP/USD",  "label": "XRP"},
    {"code": "HYPE", "symbol": "HYPE/USD", "label": "Hyperliquid"},
    {"code": "BNB",  "symbol": "BNB/USD",  "label": "BNB"},
    {"code": "ETH",  "symbol": "ETH/USD",  "label": "Ethereum"},
    {"code": "DOGE", "symbol": "DOGE/USD", "label": "Dogecoin"},
]

# How many of the most recent 5m closes to compare for momentum direction —
# matches the EMA(9)/EMA(21) building blocks already used elsewhere, so this
# reuses analyze()'s existing notion of "short-term trend" rather than
# inventing a new one.
ALT_TREND_LOOKBACK_5M_CANDLES = 30

# A correlation/lag read needs the trailing return SERIES, not just one
# spread number, so this works off ROC(1) per-candle (5m % change) over the
# lookback window for both BTC and the alt — same closes list shape as
# everywhere else: [c[4] for c in ohlcv_5m].


def _roc_series(closes: list[float]) -> list[float]:
    """Per-candle % change series: roc[i] = % change from closes[i-1] to closes[i]."""
    out = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            out.append((closes[i] - prev) / prev * 100.0)
        else:
            out.append(0.0)
    return out


def _pearson_corr(a: list[float], b: list[float]) -> float:
    """Plain Pearson correlation, no numpy dependency (matches the rest of
    bot.py's pure-Python indicator implementations). Returns 0.0 if either
    series has no variance (can't compute a meaningful correlation)."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return 0.0
    return cov / denom


def fetch_kalshi_market_for_asset(asset_code: str) -> dict:
    """
    Fetch the current-window Kalshi market for a given asset code (e.g.
    "ETH", "SOL"). Mirrors fetch_kalshi_yes_price() but generalized and
    returns a richer dict instead of a bare tuple, since the cross-asset
    tab needs to distinguish "market doesn't exist on Kalshi" from
    "fetch failed" from "fetched fine".

    Returns:
      {"available": bool,    # False if Kalshi has no such 15m market at all
       "yes_mid": float|None, "strike": float|None, "ticker": str}
    """
    ticker = _ticker_from_end(_window_end_utc(0), asset_code)
    try:
        mkt, status = _kalshi_get_market(ticker)
        if not mkt:
            return {"available": False, "yes_mid": None, "strike": None, "ticker": ticker}
        bid_s = mkt.get("yes_bid_dollars")
        ask_s = mkt.get("yes_ask_dollars")
        yes_mid = None
        if bid_s is not None and ask_s is not None:
            bid, ask = float(bid_s), float(ask_s)
            if 0.01 <= (bid + ask) / 2.0 <= 0.99:
                yes_mid = (bid + ask) / 2.0
        strike_raw = mkt.get("floor_strike") or mkt.get("strike") or mkt.get("cap_strike")
        strike = float(strike_raw) if strike_raw is not None else None
        return {"available": True, "yes_mid": yes_mid, "strike": strike, "ticker": ticker}
    except Exception as e:
        print(f"[kalshi-alt] {asset_code} fetch failed: {e}")
        return {"available": False, "yes_mid": None, "strike": None, "ticker": ticker}


# ── Kalshi's own order book (preferred source for Order Book Imbalance) ─────
# GET /markets/{ticker}/orderbook is public, no auth needed. Kalshi only
# returns BIDS (not asks) -- binary markets don't need separate ask listings
# since a YES bid at price X is mathematically a NO ask at (1.00 - X), and
# vice versa. Response shape has varied a bit across Kalshi's own docs
# (seen both {"orderbook": {"yes": [[price, count]...], "no": [...]}} and
# {"orderbook_fp": {"yes_dollars": [["0.42","13.00"]...], "no_dollars": [...]}})
# so this parses whichever one shows up rather than assuming.
# This is a much more reliable source than scanning 4 separate crypto
# exchanges' order books every poll (4x the network calls, 4x the chance of
# a rate-limit/timeout knocking the whole reading out) -- and arguably more
# relevant anyway, since it's literally the crowd betting on THIS market,
# not a proxy from spot exchanges.
def fetch_kalshi_orderbook_imbalance(ticker: str) -> dict | None:
    """Returns {"book_score": float [-1,1] (+ = YES-bid-heavy/bullish,
    - = NO-bid-heavy/bearish), "yes_depth": float, "no_depth": float} or
    None if the book isn't available/fetchable."""
    if not ticker:
        return None
    try:
        host = _kalshi_working_host[0]
        resp = requests.get(f"{host}/{ticker}/orderbook", timeout=5)
        if resp.status_code != 200:
            return None
        data = resp.json()
        ob = data.get("orderbook_fp") or data.get("orderbook") or {}
        yes_levels = ob.get("yes_dollars") or ob.get("yes") or []
        no_levels = ob.get("no_dollars") or ob.get("no") or []
        if not yes_levels and not no_levels:
            return None
        yes_depth = sum(float(count) for _, count in yes_levels)
        no_depth = sum(float(count) for _, count in no_levels)
        total = yes_depth + no_depth
        if total <= 0:
            return None
        # +1 = entirely YES bids (bullish positioning), -1 = entirely NO bids
        book_score = (yes_depth - no_depth) / total
        return {"book_score": round(book_score, 4), "yes_depth": yes_depth, "no_depth": no_depth}
    except Exception as e:
        print(f"[kalshi-orderbook] fetch failed: {e}")
        return None


# ── Polymarket comparison (independent crowd-priced odds, for benchmarking) ──
# Polymarket's BTC up/down markets use a deterministic slug:
#   btc-updown-{label}-{window_ts}, window_ts = unix window-start rounded down
# to a multiple of window_seconds. 15m product uses 900-second windows, lining
# up with Kalshi's own 15-minute window — so this is a genuine apples-to-apples
# second opinion on the SAME window, from a different crowd, not just another
# of our own model's outputs. Public Gamma API read endpoint, no auth needed.
_POLY_GAMMA = "https://gamma-api.polymarket.com"


def fetch_polymarket_15m() -> dict:
    """Returns {"available": bool, "up_pct": float|None, "down_pct": float|None,
    "question": str|None, "volume": float|None}. Never raises."""
    now_s = int(time.time())
    window_ts = now_s - (now_s % 900)
    slug = f"btc-updown-15m-{window_ts}"
    try:
        r = requests.get(f"{_POLY_GAMMA}/markets", params={"slug": slug}, timeout=5)
        if r.status_code != 200:
            return {"available": False, "up_pct": None, "down_pct": None}
        data = r.json()
        if not data:
            return {"available": False, "up_pct": None, "down_pct": None}
        m = data[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        up_pct = round(float(prices[0]) * 100, 1) if prices else None
        return {
            "available": up_pct is not None,
            "up_pct": up_pct,
            "down_pct": round(100 - up_pct, 1) if up_pct is not None else None,
            "question": m.get("question"),
            "volume": m.get("volume"),
        }
    except Exception as e:
        print(f"[polymarket] fetch failed: {e}")
        return {"available": False, "up_pct": None, "down_pct": None}


def classify_alt_vs_btc(btc_closes: list[float], alt_closes: list[float]) -> dict:
    """
    Classify an altcoin's recent price momentum relative to BTC's over the
    same window. Returns one of FOLLOWING / REVERSED / DELAYED / NONE plus
    supporting numbers, so the UI can show the call AND why.

    Method:
      - Trim both close series to the same length/alignment (most recent N).
      - Compute each one's ROC(1) per-candle % change series.
      - same-candle correlation: how closely the alt's return series tracks
        BTC's return series at zero lag → FOLLOWING if strongly positive.
      - sign mismatch on net direction over the window → REVERSED if BTC and
        the alt moved opposite net directions.
      - lagged correlation: check the alt's returns shifted +1/+2 candles
        against BTC's unshifted returns — if a shifted correlation is
        meaningfully HIGHER than the zero-lag correlation, the alt is
        trailing behind BTC's moves rather than moving with them → DELAYED.
      - NONE if no read is statistically meaningful (insufficient data or
        correlation too weak to call any of the above).

    Returns:
      {"classification": "FOLLOWING"|"REVERSED"|"DELAYED"|"NONE",
       "corr_0": float,        # zero-lag correlation
       "best_lag": int,        # 0, 1, or 2 — lag with the highest correlation
       "corr_best_lag": float,
       "btc_net_pct": float, "alt_net_pct": float,
       "note": str}
    """
    n = min(len(btc_closes), len(alt_closes))
    if n < ALT_TREND_LOOKBACK_5M_CANDLES:
        return {"classification": "NONE", "corr_0": 0.0, "best_lag": 0,
                "corr_best_lag": 0.0, "btc_net_pct": 0.0, "alt_net_pct": 0.0,
                "note": "insufficient overlapping data"}

    btc_c = btc_closes[-ALT_TREND_LOOKBACK_5M_CANDLES:]
    alt_c = alt_closes[-ALT_TREND_LOOKBACK_5M_CANDLES:]
    btc_roc = _roc_series(btc_c)
    alt_roc = _roc_series(alt_c)

    btc_net_pct = (btc_c[-1] - btc_c[0]) / btc_c[0] * 100.0 if btc_c[0] else 0.0
    alt_net_pct = (alt_c[-1] - alt_c[0]) / alt_c[0] * 100.0 if alt_c[0] else 0.0

    corr_0 = _pearson_corr(btc_roc, alt_roc)

    # Check if a lagged version of the alt's returns (shifted to align with
    # EARLIER btc returns — i.e. alt is reacting late) correlates better
    # than zero-lag. alt_roc[lag:] vs btc_roc[:-lag] tests "does alt's move
    # at time t look like BTC's move at time t-lag".
    best_lag, corr_best_lag = 0, corr_0
    for lag in (1, 2):
        if len(alt_roc) - lag < 5:
            continue
        shifted_corr = _pearson_corr(btc_roc[:-lag], alt_roc[lag:])
        if shifted_corr > corr_best_lag:
            best_lag, corr_best_lag = lag, shifted_corr

    CORR_FOLLOW_THRESHOLD = 0.45
    CORR_REVERSED_THRESHOLD = -0.35  # same-candle correlation must be genuinely negative
    CORR_DELAY_IMPROVEMENT = 0.15  # how much better a lag must be to call it "delayed" not "following"
    NET_REVERSAL_MIN_PCT = 0.03    # ignore near-zero net moves as noise, not a real reversal

    if best_lag > 0 and (corr_best_lag - corr_0) >= CORR_DELAY_IMPROVEMENT and corr_best_lag >= CORR_FOLLOW_THRESHOLD:
        classification = "DELAYED"
        note = (f"tracks BTC ~{best_lag * 5}m behind (lag-{best_lag} corr {corr_best_lag:+.2f} "
                f"vs same-candle {corr_0:+.2f})")
    elif (corr_0 <= CORR_REVERSED_THRESHOLD
          and abs(btc_net_pct) >= NET_REVERSAL_MIN_PCT and abs(alt_net_pct) >= NET_REVERSAL_MIN_PCT
          and (btc_net_pct > 0) != (alt_net_pct > 0)):
        # REVERSED requires BOTH a genuinely negative same-candle correlation
        # AND opposite net direction over the window — correlation alone
        # catches noisy choppy opposition, the net-direction check confirms
        # it actually amounted to a real opposite move, not just sign noise
        # in a directionless window.
        classification = "REVERSED"
        note = f"moved opposite BTC (corr {corr_0:+.2f}): BTC {btc_net_pct:+.3f}% vs this asset {alt_net_pct:+.3f}%"
    elif corr_0 >= CORR_FOLLOW_THRESHOLD:
        classification = "FOLLOWING"
        note = f"tracking BTC closely (same-candle corr {corr_0:+.2f})"
    else:
        classification = "NONE"
        note = f"no clear relationship right now (corr {corr_0:+.2f})"

    return {"classification": classification, "corr_0": round(corr_0, 3),
            "best_lag": best_lag, "corr_best_lag": round(corr_best_lag, 3),
            "btc_net_pct": round(btc_net_pct, 4), "alt_net_pct": round(alt_net_pct, 4),
            "note": note}


def scan_alt_assets_vs_btc(exchanges: dict, symbol: str, ohlcv_5m_btc: list) -> list[dict]:
    """
    For every asset in ALT_ASSETS: fetch its own blended 5m candles, classify
    vs BTC, and fetch its current Kalshi 15m market (if one exists). Designed
    to be called on a slow-ish cadence (this is 6 extra blended-candle fetches
    + 6 Kalshi calls) — see monitor.py's tab refresh timer, not the 10s
    LiveTicker loop. Returns a list of per-asset result dicts, always in
    ALT_ASSETS order, so the UI can render a stable table even if some
    assets fail this cycle.
    """
    btc_closes = [c[4] for c in ohlcv_5m_btc]
    results = []
    for asset in ALT_ASSETS:
        code, sym, label = asset["code"], asset["symbol"], asset["label"]
        row = {"code": code, "label": label, "symbol": sym,
               "price_available": False, "classification": "NONE",
               "note": "no data", "kalshi_available": False,
               "kalshi_strike": None, "kalshi_yes": None, "price": None}
        try:
            alt_ohlcv = fetch_blended_ohlcv(exchanges, sym, "5m", ALT_TREND_LOOKBACK_5M_CANDLES + 5)
            alt_closes = [c[4] for c in alt_ohlcv]
            row["price_available"] = True
            row["price"] = alt_closes[-1] if alt_closes else None
            cls = classify_alt_vs_btc(btc_closes, alt_closes)
            row.update(cls)
        except Exception as e:
            row["note"] = f"price fetch failed: {e}"

        try:
            mkt = fetch_kalshi_market_for_asset(code)
            row["kalshi_available"] = mkt["available"]
            row["kalshi_strike"] = mkt["strike"]
            row["kalshi_yes"] = mkt["yes_mid"]
        except Exception as e:
            row["note"] += f" | kalshi fetch failed: {e}"

        results.append(row)
    return results


def bet_recommendation(confidence: str) -> str:
    """Returns a recommended bet-size line for the signal message. Plain
    text (no HTML) so it displays correctly whether it ends up in a
    .textContent UI field, a Discord message, or anywhere else — embedding
    <b> tags here meant they showed up as literal text wherever the
    consumer didn't render HTML."""
    if confidence == "STRONG":
        return "💎 Bet FULL size"
    elif confidence == "HIGH":
        return "🔶 Bet 75% size"
    elif confidence == "MEDIUM":
        return "🔵 Bet 50% size"
    else:
        return "⚪ Tiny bet — low confidence"


def lean_action(direction: str, note: str = "") -> str:
    """Rebuilds the action line as a low-confidence LEAN call, used when a
    safety filter (flat market / cooldown) downgrades an otherwise confident
    read. Keeps the direction; flags it as a best-guess with an optional note."""
    tail = f"  {note}" if note else ""
    if direction == "UP":
        return f"🟢 <b>LEAN UP</b> — best guess (low confidence){tail}"
    return f"🔴 <b>LEAN DOWN</b> — best guess (low confidence){tail}"


def fetch_funding_rate() -> float | None:
    """
    Fetch BTC/USD perpetual funding rate from Kraken Futures (PI_XBTUSD).
    Returns per-second rate (positive = longs crowded = bearish pressure;
    negative = shorts crowded = bullish pressure), or None on failure.
    Threshold: ±2e-9/s ≈ ±0.006% per 8h — meaningful leverage imbalance.
    """
    try:
        resp = requests.get(
            "https://futures.kraken.com/derivatives/api/v3/tickers/PI_XBTUSD",
            timeout=5
        )
        if resp.status_code != 200:
            return None
        rate = resp.json().get("ticker", {}).get("fundingRate")
        if rate is None:
            return None
        fr = float(rate)
        print(f"[funding] PI_XBTUSD rate={fr:.3e}/s")
        return fr
    except Exception as e:
        print(f"[funding] fetch failed: {e}")
        return None


def fetch_kalshi_settlement(close_dt: datetime) -> str | None:
    """
    Fetch the official YES/NO result for the KXBTC15M window that closed at
    close_dt (ET). Returns "yes", "no", or None if not yet settled / unavailable.
    """
    try:
        # Kalshi tickers are always in Eastern Time, end-time format:
        # KXBTC15M-{YY}{MON}{DD}{end_HHMM}-{end_min}
        ticker = _ticker_from_end(close_dt)
        mkt, status = _kalshi_get_market(ticker)
        if not mkt:
            return None
        result = mkt.get("result", "")
        if result in ("yes", "no"):
            print(f"[kalshi] official settlement {ticker} = {result.upper()}")
            return result
        return None
    except Exception as e:
        print(f"[kalshi] settlement fetch failed: {e}")
        return None


STATS_FILE    = "stats.json"
SIGNALS_FILE  = "signals.json"
STAGED_FILE   = "staged.json"
MAX_SIGNALS   = 96  # 4 windows/hour x 24h = a full day of signal history


def save_staged(key: str, text: str) -> None:
    """Persist the latest 6-min ("early") or 2-min ("late") staged message to
    staged.json so monitor.py's GUI can display the most recent one sent,
    even though the GUI can't trigger these itself (they fire on a timer
    inside the main loop, tied to the live window countdown)."""
    try:
        try:
            with open(STAGED_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        data[key] = {"text": text, "ts": datetime.now(ET).isoformat()}
        with open(STAGED_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[{now()}] Could not save staged.json: {e}")


# ── Session stats + result tracker ───────────────────────────────────────────

# ── Session stats + result tracker ───────────────────────────────────────────
# DATA_DIR is computed once so every entry point (core.py run directly,
# streamlit_app.py, desktop_app.py, webgui_app.py, or a PyInstaller-frozen
# exe) writes/reads stats_*.json, signals_*.json, staged_*.json in the SAME
# place — next to the actual script/exe, not whatever the current working
# directory happens to be when launched (double-clicking an exe on Windows,
# for instance, can have an unrelated cwd).
if getattr(sys, "frozen", False):
    DATA_DIR = os.path.dirname(sys.executable)
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))


class Stats:
    def __init__(self, asset_code: str = DEFAULT_ASSET, data_dir: str | None = None):
        data_dir = data_dir if data_dir is not None else DATA_DIR
        self.asset_code = asset_code
        self.stats_file = os.path.join(data_dir, f"stats_{asset_code}.json")
        self.signals_file = os.path.join(data_dir, f"signals_{asset_code}.json")
        self.staged_file = os.path.join(data_dir, f"staged_{asset_code}.json")
        self.wins = 0
        self.losses = 0
        self.skips = 0
        self.streak = 0          # positive = win streak, negative = loss streak
        self.pending: dict | None = None
        # Independent QQE-only tracker (what QQE alone would have scored each
        # window, regardless of the bot's actual bet). Checked via /qqewl.
        self.qqe_wins = 0
        self.qqe_losses = 0
        self.qqe_streak = 0
        self.qqe_pending: dict | None = None
        # Independent /predict-only tracker — separate from both the main
        # bet record and the QQE-only record. /predict calls the NEXT
        # window's direction on demand; this tracks how often that specific
        # on-demand call was right, resolved the same way pending bets are.
        self.predict_wins = 0
        self.predict_losses = 0
        self.predict_streak = 0
        self.predict_pending: dict | None = None
        # Independent Bridge-model tracker — records the Bridge tab's blended
        # directional call (ABOVE/BELOW) each window and scores it against
        # the actual settlement, exactly like the predict tracker.
        self.bridge_wins = 0
        self.bridge_losses = 0
        self.bridge_streak = 0
        self.bridge_pending: dict | None = None
        # Independent Physics-model tracker — same pattern for the
        # Langevin/Fokker-Planck model's directional call (ABOVE/BELOW).
        self.physics_wins = 0
        self.physics_losses = 0
        self.physics_streak = 0
        self.physics_pending: dict | None = None
        # Independent Polymarket-crowd-odds tracker — records what Polymarket's
        # own up/down price alone would have called each window, scored
        # against the same settlement as everything else. This is the
        # benchmark: if this consistently beats the blended bet, the model
        # isn't adding value over just reading the other crowd's price.
        self.poly_wins = 0
        self.poly_losses = 0
        self.poly_streak = 0
        self.poly_pending: dict | None = None
        self.last_why: list[str] = []   # vote lines from last signal (not persisted)
        self.signals: list[dict] = []   # recent signal history (persisted separately)
        self._load()
        self._load_signals()

    def _load(self) -> None:
        try:
            with open(self.stats_file) as f:
                d = json.load(f)
            self.wins    = d.get("wins", 0)
            self.losses  = d.get("losses", 0)
            self.skips   = d.get("skips", 0)
            self.streak  = d.get("streak", 0)
            self.pending = d.get("pending")
            self.qqe_wins    = d.get("qqe_wins", 0)
            self.qqe_losses  = d.get("qqe_losses", 0)
            self.qqe_streak  = d.get("qqe_streak", 0)
            self.qqe_pending = d.get("qqe_pending")
            self.predict_wins    = d.get("predict_wins", 0)
            self.predict_losses  = d.get("predict_losses", 0)
            self.predict_streak  = d.get("predict_streak", 0)
            self.predict_pending = d.get("predict_pending")
            self.bridge_wins    = d.get("bridge_wins", 0)
            self.bridge_losses  = d.get("bridge_losses", 0)
            self.bridge_streak  = d.get("bridge_streak", 0)
            self.bridge_pending = d.get("bridge_pending")
            self.physics_wins    = d.get("physics_wins", 0)
            self.physics_losses  = d.get("physics_losses", 0)
            self.physics_streak  = d.get("physics_streak", 0)
            self.physics_pending = d.get("physics_pending")
            self.poly_wins    = d.get("poly_wins", 0)
            self.poly_losses  = d.get("poly_losses", 0)
            self.poly_streak  = d.get("poly_streak", 0)
            self.poly_pending = d.get("poly_pending")
            print(f"[stats] Loaded from disk: {self.wins}W/{self.losses}L/{self.skips}S")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[stats] Could not load {self.stats_file}: {e}")

    def _save(self) -> None:
        try:
            with open(self.stats_file, "w") as f:
                json.dump({
                    "wins": self.wins,
                    "losses": self.losses,
                    "skips": self.skips,
                    "streak": self.streak,
                    "pending": self.pending,
                    "qqe_wins": self.qqe_wins,
                    "qqe_losses": self.qqe_losses,
                    "qqe_streak": self.qqe_streak,
                    "qqe_pending": self.qqe_pending,
                    "predict_wins": self.predict_wins,
                    "predict_losses": self.predict_losses,
                    "predict_streak": self.predict_streak,
                    "predict_pending": self.predict_pending,
                    "bridge_wins": self.bridge_wins,
                    "bridge_losses": self.bridge_losses,
                    "bridge_streak": self.bridge_streak,
                    "bridge_pending": self.bridge_pending,
                    "physics_wins": self.physics_wins,
                    "physics_losses": self.physics_losses,
                    "physics_streak": self.physics_streak,
                    "physics_pending": self.physics_pending,
                    "poly_wins": self.poly_wins,
                    "poly_losses": self.poly_losses,
                    "poly_streak": self.poly_streak,
                    "poly_pending": self.poly_pending,
                }, f)
        except Exception as e:
            print(f"[stats] Could not save {self.stats_file}: {e}")

    def _load_signals(self) -> None:
        try:
            with open(self.signals_file) as f:
                self.signals = json.load(f)
        except FileNotFoundError:
            self.signals = []
        except Exception as e:
            print(f"[signals] Could not load {self.signals_file}: {e}")
            self.signals = []

    def _save_signals(self) -> None:
        try:
            with open(self.signals_file, "w") as f:
                json.dump(self.signals[-MAX_SIGNALS:], f)
        except Exception as e:
            print(f"[signals] Could not save {self.signals_file}: {e}")

    def reset_all(self) -> str:
        """Wipes EVERY tracked record — main W/L, QQE-only W/L, /predict-only
        W/L, streaks, pending bets, and signal history. Used by the Record
        Reset button in monitor.py (and still available as /reset). Returns
        a short summary of what was wiped, for confirmation messaging.
        Irreversible once called — the caller is responsible for confirming
        with the user first (the GUI button does this via a dialog)."""
        old_summary = self.summary()
        self.wins = 0
        self.losses = 0
        self.skips = 0
        self.streak = 0
        self.pending = None
        self.qqe_wins = 0
        self.qqe_losses = 0
        self.qqe_streak = 0
        self.qqe_pending = None
        self.predict_wins = 0
        self.predict_losses = 0
        self.predict_streak = 0
        self.predict_pending = None
        self.bridge_wins = 0
        self.bridge_losses = 0
        self.bridge_streak = 0
        self.bridge_pending = None
        self.physics_wins = 0
        self.physics_losses = 0
        self.physics_streak = 0
        self.physics_pending = None
        self.poly_wins = 0
        self.poly_losses = 0
        self.poly_streak = 0
        self.poly_pending = None
        self.last_why = []
        self.signals = []
        self._save()
        self._save_signals()
        return old_summary

    def pending_expired(self) -> bool:
        """True if the pending signal's window has already closed."""
        return self._pending_expired(self.pending)

    def qqe_pending_expired(self) -> bool:
        """True if the QQE-only pending's window has already closed."""
        return self._pending_expired(self.qqe_pending)

    def predict_pending_expired(self) -> bool:
        """True if the /predict-only pending call's window has already closed."""
        return self._pending_expired(self.predict_pending)

    @staticmethod
    def _pending_expired(pending: dict | None) -> bool:
        if not pending:
            return False
        try:
            end_iso = pending.get("window_end_iso")
            if not end_iso:
                return False
            end_dt = datetime.fromisoformat(end_iso)
            return datetime.now(ET) > end_dt
        except Exception:
            return False

    def record_signal(self, direction: str, price: float, window: str,
                      confidence: str, prob: float,
                      strike: float | None = None) -> None:
        if direction == "SKIP":
            self.skips += 1
            self.pending = None
        else:
            # Store when this window closes so we can auto-resolve after restarts
            window_end = datetime.now(ET).replace(second=0, microsecond=0)
            window_end += timedelta(minutes=15 - (window_end.minute % 15))
            self.pending = {
                "direction": direction,
                "entry_price": price,
                "strike": strike,        # Kalshi settlement target — resolve against this
                "window": window,
                "confidence": confidence,
                "prob": prob,
                "window_end_iso": window_end.isoformat(),
            }
            # Append to signal history
            self.signals.append({
                "ts": datetime.now(ET).isoformat(),
                "window": window,
                "direction": direction,
                "confidence": confidence,
                "prob": prob,
                "strike": strike,
                "result": None,
            })
            self._save_signals()
        self._save()

    def set_window_open_bet(self, direction: str, confidence: str, prob: float,
                             price: float, window: str, strike: float | None) -> None:
        """Fires the INSTANT a new 15-min window opens (top of the main
        loop, before any of the staged EARLY READ / :01 sleeps) so the GUI's
        'CURRENT OPEN BET' reflects something immediately rather than
        staying empty for the first ~1-6 minutes of every round. Uses
        whatever candle data is available at that instant — which is mostly
        still PRIOR-window price action, since this window's own candles
        don't exist yet — so this read is necessarily rougher than the
        :01 main signal or the 9-min EARLY READ that follow it and will
        normally supersede it via the same window-match override logic.

        Same safe override pattern as set_late_bet(): updates the existing
        pending/history row in place if one already exists for this exact
        window (so later calls in the same window never create a duplicate
        history row), otherwise creates a fresh one."""
        window_end = datetime.now(ET).replace(second=0, microsecond=0)
        window_end += timedelta(minutes=15 - (window_end.minute % 15))
        window_end_iso = window_end.isoformat()
        same_window = (self.pending is not None
                       and self.pending.get("window_end_iso") == window_end_iso)
        if same_window:
            self.pending["direction"] = direction
            self.pending["confidence"] = confidence
            self.pending["prob"] = prob
            if strike is not None:
                self.pending["strike"] = strike
            self.pending["window_open_read"] = True
            for sig in reversed(self.signals):
                if sig.get("result") is None and sig.get("window") == window:
                    sig.update(direction=direction, confidence=confidence,
                               prob=prob, window_open_read=True)
                    break
        else:
            self.pending = {
                "direction": direction,
                "entry_price": price,
                "strike": strike,
                "window": window,
                "confidence": confidence,
                "prob": prob,
                "window_end_iso": window_end_iso,
                "window_open_read": True,
            }
            self.signals.append({
                "ts": datetime.now(ET).isoformat(),
                "window": window,
                "direction": direction,
                "confidence": confidence,
                "prob": prob,
                "result": None,
                "window_open_read": True,
            })
        self._save_signals()
        self._save()

    def set_late_bet(self, direction: str, confidence: str, prob: float,
                     price: float, window: str, strike: float | None) -> None:
        """The 6-min EARLY READ is the headline bet Jeremy actually places.
        Make it the TRACKED prediction: override the early :01 pending if one
        exists (same window), otherwise create a fresh pending so it gets
        resolved. Resolution always scores against the strike, so this records
        the call Jeremy acts on."""
        window_end = datetime.now(ET).replace(second=0, microsecond=0)
        window_end += timedelta(minutes=15 - (window_end.minute % 15))
        window_end_iso = window_end.isoformat()
        # Override the early pending ONLY if it belongs to THIS window. If the
        # current pending is for a different window it's stale (a resolve was
        # missed), so we must not clobber its history row — record fresh instead.
        same_window = (self.pending is not None
                       and self.pending.get("window_end_iso") == window_end_iso)
        if same_window:
            self.pending["direction"] = direction
            self.pending["confidence"] = confidence
            self.pending["prob"] = prob
            if strike is not None:
                self.pending["strike"] = strike
            self.pending["late"] = True
            # Update the matching unresolved history row for THIS window only.
            for sig in reversed(self.signals):
                if sig.get("result") is None and sig.get("window") == window:
                    sig.update(direction=direction, confidence=confidence,
                               prob=prob, late=True)
                    break
        else:
            self.pending = {
                "direction": direction,
                "entry_price": price,
                "strike": strike,
                "window": window,
                "confidence": confidence,
                "prob": prob,
                "window_end_iso": window_end_iso,
                "late": True,
            }
            self.signals.append({
                "ts": datetime.now(ET).isoformat(),
                "window": window,
                "direction": direction,
                "confidence": confidence,
                "prob": prob,
                "result": None,
                "late": True,
            })
        self._save_signals()
        self._save()

    def streak_label(self) -> str:
        if self.streak >= 3:
            return f"🔥 {self.streak} wins in a row"
        elif self.streak == 2:
            return f"✅ {self.streak} wins in a row"
        elif self.streak <= -3:
            return f"🥶 {abs(self.streak)} losses in a row"
        elif self.streak == -2:
            return f"❌ {abs(self.streak)} losses in a row"
        return ""

    def resolve(self, current_price: float,
               kalshi_result: str | None = None) -> str | None:
        """
        Call at the start of each new window with the latest price.
        If kalshi_result is "yes"/"no", uses Kalshi's official settlement outcome.
        Otherwise falls back to price comparison.
        Returns a formatted result message if there was a pending signal.
        """
        if self.pending is None:
            return None

        p = self.pending
        self.pending = None
        entry = p["entry_price"]
        strike = p.get("strike")
        diff = current_price - entry
        diff_pct = diff / entry * 100

        # Official Kalshi result takes precedence over price comparison.
        # Otherwise compare settlement price vs the STRIKE (Kalshi YES resolves
        # YES iff settlement ≥ strike), NOT vs entry price. A UP signal can
        # close lower than entry but still above strike — that's a WIN.
        actual, verified = self._settlement_actual(
            current_price, entry, strike, kalshi_result)

        correct = actual == p["direction"]

        if correct:
            self.wins += 1
            result_icon = "✅ WIN"
            self.streak = self.streak + 1 if self.streak >= 0 else 1
        else:
            self.losses += 1
            result_icon = "❌ LOSS"
            self.streak = self.streak - 1 if self.streak <= 0 else -1

        # Update signal history result. MUST match by window, not just "the
        # first unresolved entry found scanning backwards" — the old version
        # of this code did the latter, which silently left orphaned entries
        # stuck at result=None forever if even one signal ever got skipped
        # (e.g. by an exception mid-cycle, a restart, or set_late_bet's
        # "different window" branch appending a fresh row on top of an
        # already-unresolved one). Each resolve() call only ever cleared the
        # NEWEST unresolved row, so an orphaned older one would never get
        # revisited — exactly the "bets from hours ago still show open" bug.
        #
        # Fix: match the row whose window equals THIS pending's window
        # first (the correct, intended match). As a backlog sweep, also
        # clear any OTHER still-unresolved rows that are older than this
        # one — they're orphaned and will never get a dedicated resolve()
        # call of their own, so leaving them at None forever is strictly
        # worse than settling them now against the same outcome data we
        # already have. They're tagged so it's visible they were swept
        # rather than individually resolved.
        matched = False
        this_ts = None
        for sig in self.signals:
            if sig.get("window") == p.get("window") and sig.get("result") is None:
                sig["result"] = "WIN" if correct else "LOSS"
                this_ts = sig.get("ts")
                matched = True
                break
        if not matched:
            # Fallback: pending's window didn't match any history row (older
            # data format, or the row was already swept) — fall back to the
            # old "most recent unresolved" behavior so we still record SOME
            # result rather than silently dropping it.
            for sig in reversed(self.signals):
                if sig.get("result") is None:
                    sig["result"] = "WIN" if correct else "LOSS"
                    this_ts = sig.get("ts")
                    break
        # Backlog sweep: any OTHER unresolved row strictly older than the one
        # just matched is orphaned — settle it against the same result so it
        # doesn't stay stuck at None indefinitely.
        if this_ts:
            for sig in self.signals:
                if sig.get("result") is None and sig.get("ts") and sig["ts"] < this_ts:
                    sig["result"] = "WIN" if correct else "LOSS"
                    sig["swept"] = True
        self._save_signals()

        self._save()
        total = self.wins + self.losses
        win_rate = (self.wins / total * 100) if total > 0 else 0

        return (
            f"{result_icon}\n"
            f"<b>BTC 15min @ ${entry:,.2f}  •  {p['direction']}</b>\n"
            f"{p['window']}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🏁 Settlement: <b>${current_price:,.2f}</b>  ({diff_pct:+.2f}%){verified}\n"
            f"📊 Session: <b>{self.wins}W / {self.losses}L</b>  |  Win rate: <b>{win_rate:.0f}%</b>\n"
            f"📲 @ChillLobsterBot"
        )

    @staticmethod
    def _settlement_actual(current_price: float, entry: float,
                           strike: float | None,
                           kalshi_result: str | None) -> tuple[str, str]:
        """Determine the window's actual outcome (UP/DOWN) + a label.
        Official Kalshi result wins; else settlement vs strike; else vs entry."""
        if kalshi_result == "yes":
            return "UP", "  ✓ Kalshi official"
        elif kalshi_result == "no":
            return "DOWN", "  ✓ Kalshi official"
        elif strike is not None:
            actual = "UP" if current_price >= strike else "DOWN"
            return actual, f"  vs strike ${strike:,.2f}"
        else:
            return ("UP" if (current_price - entry) > 0 else "DOWN"), "  (no strike)"

    def record_qqe(self, direction: str, price: float, strike: float | None) -> None:
        """Record QQE's standalone call for the current window (UP/DOWN).
        Independent of the bot's bet — resolved silently at settlement."""
        window_end = datetime.now(ET).replace(second=0, microsecond=0)
        window_end += timedelta(minutes=15 - (window_end.minute % 15))
        self.qqe_pending = {
            "direction": direction,
            "entry_price": price,
            "strike": strike,
            "window_end_iso": window_end.isoformat(),
        }
        self._save()

    def resolve_qqe(self, current_price: float,
                    kalshi_result: str | None = None) -> None:
        """Silently score QQE's standalone pending call against settlement.
        Updates the QQE-only W/L counters used by /qqewl. No Telegram message.
        No-op unless the pending window has actually closed — guards against
        resolving the current (still-open) window's call against a stale price."""
        if self.qqe_pending is None or not self.qqe_pending_expired():
            return
        q = self.qqe_pending
        self.qqe_pending = None
        actual, _ = self._settlement_actual(
            current_price, q["entry_price"], q.get("strike"), kalshi_result)
        if actual == q["direction"]:
            self.qqe_wins += 1
            self.qqe_streak = self.qqe_streak + 1 if self.qqe_streak >= 0 else 1
        else:
            self.qqe_losses += 1
            self.qqe_streak = self.qqe_streak - 1 if self.qqe_streak <= 0 else -1
        self._save()
        print(f"[{now()}] QQE-only resolved: {q['direction']} vs {actual} "
              f"| {self.qqe_wins}W/{self.qqe_losses}L")

    def qqe_summary(self) -> str:
        total = self.qqe_wins + self.qqe_losses
        rate = f"{self.qqe_wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.qqe_wins}W/{self.qqe_losses}L  WR:{rate}"

    def record_predict(self, direction: str, price: float, strike: float | None,
                        window_end_iso: str) -> None:
        """Record an on-demand /predict call for the NEXT window it targeted.
        Independent of both the main bet record and the QQE-only record —
        this tracks specifically how often /predict's own on-demand call was
        right, resolved the same way QQE's pending call is. window_end_iso
        must be the END of the window /predict was calling (NOT the current
        window), since /predict always targets the next one."""
        self.predict_pending = {
            "direction": direction,
            "entry_price": price,
            "strike": strike,
            "window_end_iso": window_end_iso,
        }
        self._save()

    def resolve_predict(self, current_price: float,
                         kalshi_result: str | None = None) -> None:
        """Silently score /predict's pending call against settlement once its
        target window has closed. Updates predict_wins/predict_losses used by
        the GUI's Predict W/L card. No-op unless the pending window has
        actually closed yet."""
        if self.predict_pending is None or not self.predict_pending_expired():
            return
        p = self.predict_pending
        self.predict_pending = None
        actual, _ = self._settlement_actual(
            current_price, p["entry_price"], p.get("strike"), kalshi_result)
        if actual == p["direction"]:
            self.predict_wins += 1
            self.predict_streak = self.predict_streak + 1 if self.predict_streak >= 0 else 1
        else:
            self.predict_losses += 1
            self.predict_streak = self.predict_streak - 1 if self.predict_streak <= 0 else -1
        self._save()
        print(f"[{now()}] /predict resolved: {p['direction']} vs {actual} "
              f"| {self.predict_wins}W/{self.predict_losses}L")

    def predict_summary(self) -> str:
        total = self.predict_wins + self.predict_losses
        rate = f"{self.predict_wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.predict_wins}W/{self.predict_losses}L  WR:{rate}"

    def record_bridge(self, direction: str, strike: float | None,
                       window_end_iso: str) -> None:
        """Record the Bridge model's ABOVE/BELOW call for the current window.
        Called from the main loop whenever compute_bridge fires a non-NEUTRAL
        signal. Resolved at the next window close."""
        if direction not in ("ABOVE", "DOWN", "UP", "BELOW"):
            return
        # Normalize: Bridge uses ABOVE/BELOW, settlement comparison uses UP/DOWN
        self.bridge_pending = {
            "direction": "UP" if direction == "ABOVE" else "DOWN",
            "strike": strike,
            "window_end_iso": window_end_iso,
        }
        self._save()

    def resolve_bridge(self, current_price: float,
                        kalshi_result: str | None = None) -> None:
        """Score Bridge's pending call against settlement. No-op unless the
        window has closed."""
        if self.bridge_pending is None:
            return
        p = self.bridge_pending
        # Check expiry manually using the same helper predict uses
        if not self._pending_expired(p):
            return
        self.bridge_pending = None
        actual, _ = self._settlement_actual(
            current_price, current_price, p.get("strike"), kalshi_result)
        if actual == p["direction"]:
            self.bridge_wins += 1
            self.bridge_streak = self.bridge_streak + 1 if self.bridge_streak >= 0 else 1
        else:
            self.bridge_losses += 1
            self.bridge_streak = self.bridge_streak - 1 if self.bridge_streak <= 0 else -1
        self._save()
        print(f"[{now()}] Bridge resolved: {p['direction']} vs {actual} "
              f"| {self.bridge_wins}W/{self.bridge_losses}L")

    def bridge_summary(self) -> str:
        total = self.bridge_wins + self.bridge_losses
        rate = f"{self.bridge_wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.bridge_wins}W/{self.bridge_losses}L  WR:{rate}"

    def record_physics(self, direction: str, strike: float | None,
                        window_end_iso: str) -> None:
        """Record the Physics model's ABOVE/BELOW call for the current window."""
        if direction not in ("ABOVE", "DOWN", "UP", "BELOW"):
            return
        self.physics_pending = {
            "direction": "UP" if direction == "ABOVE" else "DOWN",
            "strike": strike,
            "window_end_iso": window_end_iso,
        }
        self._save()

    def resolve_physics(self, current_price: float,
                         kalshi_result: str | None = None) -> None:
        """Score Physics's pending call against settlement."""
        if self.physics_pending is None:
            return
        p = self.physics_pending
        if not self._pending_expired(p):
            return
        self.physics_pending = None
        actual, _ = self._settlement_actual(
            current_price, current_price, p.get("strike"), kalshi_result)
        if actual == p["direction"]:
            self.physics_wins += 1
            self.physics_streak = self.physics_streak + 1 if self.physics_streak >= 0 else 1
        else:
            self.physics_losses += 1
            self.physics_streak = self.physics_streak - 1 if self.physics_streak <= 0 else -1
        self._save()
        print(f"[{now()}] Physics resolved: {p['direction']} vs {actual} "
              f"| {self.physics_wins}W/{self.physics_losses}L")

    def physics_summary(self) -> str:
        total = self.physics_wins + self.physics_losses
        rate = f"{self.physics_wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.physics_wins}W/{self.physics_losses}L  WR:{rate}"

    def record_poly(self, direction: str, strike: float | None,
                     window_end_iso: str) -> None:
        """Record what Polymarket's own crowd-priced up/down odds alone
        would have called for the current window (>50% up_pct -> UP, else
        DOWN). Resolved against the same settlement as every other tracker
        so it's a fair head-to-head against the blended bet."""
        if direction not in ("UP", "DOWN"):
            return
        self.poly_pending = {
            "direction": direction,
            "strike": strike,
            "window_end_iso": window_end_iso,
        }
        self._save()

    def resolve_poly(self, current_price: float,
                      kalshi_result: str | None = None) -> None:
        if self.poly_pending is None:
            return
        p = self.poly_pending
        if not self._pending_expired(p):
            return
        self.poly_pending = None
        actual, _ = self._settlement_actual(
            current_price, current_price, p.get("strike"), kalshi_result)
        if actual == p["direction"]:
            self.poly_wins += 1
            self.poly_streak = self.poly_streak + 1 if self.poly_streak >= 0 else 1
        else:
            self.poly_losses += 1
            self.poly_streak = self.poly_streak - 1 if self.poly_streak <= 0 else -1
        self._save()
        print(f"[{now()}] Polymarket resolved: {p['direction']} vs {actual} "
              f"| {self.poly_wins}W/{self.poly_losses}L")

    def poly_summary(self) -> str:
        total = self.poly_wins + self.poly_losses
        rate = f"{self.poly_wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.poly_wins}W/{self.poly_losses}L  WR:{rate}"

    def set_why(self, votes: list) -> None:
        """Store formatted vote breakdown for /why command."""
        lines = []
        for v in votes:
            if v.direction == 1:
                arrow = "🟢"
            elif v.direction == -1:
                arrow = "🔴"
            else:
                arrow = "⚪"
            lines.append(f"{arrow} <b>{v.label}</b>: {v.detail}")
        self.last_why = lines

    def manual_result(self, won: bool) -> None:
        """Manually record a win or loss (overrides auto-tracker)."""
        if won:
            self.wins += 1
            self.streak = self.streak + 1 if self.streak >= 0 else 1
        else:
            self.losses += 1
            self.streak = self.streak - 1 if self.streak <= 0 else -1
        self.pending = None
        self._save()

    def summary(self) -> str:
        total = self.wins + self.losses
        rate = f"{self.wins / total * 100:.0f}%" if total > 0 else "n/a"
        return f"{self.wins}W/{self.losses}L  WR:{rate}"

    def stuck_unresolved_count(self) -> int:
        """Diagnostic only — counts signals.json entries still at
        result=None. Does NOT touch or resolve them (a backlog from before
        this version's resolve() fix would need real historical settlement
        prices to resolve correctly, which aren't recoverable after the
        fact — silently guessing with a CURRENT price would corrupt win/loss
        history with wrong data, so this only reports the count)."""
        return sum(1 for s in (self.signals or []) if s.get("result") is None)


# ── Command poller ────────────────────────────────────────────────────────────

class CommandPoller(threading.Thread):
    """
    Background thread. Originally polled Telegram for incoming commands;
    Telegram polling has been removed since monitor.py's GUI is the only
    command surface now. The thread itself (run()) is effectively unused —
    monitor.py calls the _handle_* methods directly — but is kept startable
    as a no-op in case bot.py is ever run standalone again.
    """

    def __init__(self, stats: Stats, exchanges: dict, symbol: str):
        super().__init__(daemon=True)
        self.stats = stats
        self.exchanges = exchanges
        self.symbol = symbol
        # Backward-compat single-exchange shim for any code/tools that still
        # expect self.exchange / self.exchange.fetch_ohlcv directly.
        self.exchange = next(iter(exchanges.values()))[0] if exchanges else None

    def _fetch_ohlcv(self, timeframe: str, limit: int) -> list:
        return fetch_blended_ohlcv(self.exchanges, self.symbol, timeframe, limit)

    def _handle_help(self) -> None:
        notify(
            f"🤖 <b>@ChillLobsterBot — Commands</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>/bet</b>     — on-demand trade signal now (incl. QQE)\n"
            f"🔭 <b>/predict</b> — predict the NEXT window's direction\n"
            f"🔮 <b>/guess</b>   — directional lean for current window\n"
            f"📊 <b>/stats</b>   — session W/L/streak/win rate\n"
            f"💡 <b>/why</b>     — indicator breakdown of last signal\n"
            f"📈 <b>/qqe</b>     — what QQE (core momentum) is saying now\n"
            f"🏆 <b>/qqewl</b>   — QQE-only win/loss record\n"
            f"💰 <b>/price</b>   — current BTC CF Benchmarks price\n"
            f"⏰ <b>/next</b>    — time until next signal\n"
            f"✅ <b>/win</b>     — manually record a win\n"
            f"❌ <b>/loss</b>    — manually record a loss\n"
            f"🔄 <b>/reset</b>   — wipe session stats and start fresh\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"Signals fire automatically at :01 :16 :31 :46"
        )

    def _handle_price(self) -> None:
        try:
            ohlcv = self._fetch_ohlcv("5m", 2)
            blended_price = ohlcv[-1][4]
            cf_price = get_cf_price(blended_price)
            change = ohlcv[-1][4] - ohlcv[-2][4]
            arrow = "▲" if change >= 0 else "▼"
            notify(
                f"💰 <b>BTC Price (CF Benchmarks)</b>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"<b>${cf_price:,.2f}</b>  {arrow} {change:+.2f}\n"
                f"🕐 {now()}"
            )
        except Exception as e:
            notify(f"⚠️ Could not fetch price: {e}")

    def _handle_qqe(self) -> None:
        """Report what QQE (the core momentum indicator) is currently saying."""
        try:
            ohlcv_5m = self._fetch_ohlcv("5m", 100)
            closes = [c[4] for c in ohlcv_5m]
            q_trend, q_rsima, q_line = qqe(closes)
            if q_trend == 0 or q_rsima is None or q_line is None:
                notify(
                    f"📈 <b>QQE — Core Momentum</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Insufficient data to read QQE right now.\n"
                    f"🕐 {now()}"
                )
                return
            if q_trend == 1:
                icon, side, rel = "🟢", "BULLISH", ">"
            else:
                icon, side, rel = "🔴", "BEARISH", "<"
            dist = abs(q_rsima - q_line)
            notify(
                f"📈 <b>QQE — Core Momentum</b>  (5m)\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{icon} <b>{side}</b>\n"
                f"RsiMa <b>{q_rsima:.1f}</b> {rel} stop <b>{q_line:.1f}</b>\n"
                f"📏 Distance from stop: {dist:.1f}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"QQE is the weighted x2 core vote — leans <b>{'UP' if q_trend == 1 else 'DOWN'}</b>\n"
                f"🕐 {now()}"
            )
        except Exception as e:
            notify(f"⚠️ Could not read QQE: {e}")

    def _handle_qqewl(self) -> None:
        """Report the QQE-only standalone win/loss record."""
        s = self.stats
        total = s.qqe_wins + s.qqe_losses
        rate = f"{s.qqe_wins / total * 100:.0f}%" if total > 0 else "n/a"
        if s.qqe_streak >= 2:
            streak = f"🔥 {s.qqe_streak} QQE wins in a row\n"
        elif s.qqe_streak <= -2:
            streak = f"🥶 {abs(s.qqe_streak)} QQE losses in a row\n"
        else:
            streak = ""
        pending = ""
        if s.qqe_pending:
            pending = f"⏳ This window: QQE leans <b>{s.qqe_pending['direction']}</b>\n"
        notify(
            f"📈 <b>QQE-only Record</b>\n"
            f"<i>How QQE alone would have scored</i>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"✅ Wins:   <b>{s.qqe_wins}</b>\n"
            f"❌ Losses: <b>{s.qqe_losses}</b>\n"
            f"🎯 Win rate: <b>{rate}</b>\n"
            + streak
            + pending
            + f"━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now()}"
        )

    def _handle_why(self) -> None:
        if not self.stats.last_why:
            notify("⚠️ No signal has been generated yet this session.\nSend /bet to get one.")
            return
        lines = "\n".join(self.stats.last_why)
        pending = self.stats.pending
        direction = f"<b>{pending['direction']}</b> ({pending['confidence']})" if pending else "n/a"
        notify(
            f"💡 <b>Why — Last Signal Breakdown</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"{lines}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 Call: {direction}\n"
            f"🕐 {now()}"
        )

    def _handle_reset(self) -> None:
        old = self.stats.summary()
        self.stats.wins = 0
        self.stats.losses = 0
        self.stats.skips = 0
        self.stats.streak = 0
        self.stats.pending = None
        self.stats.last_why = []
        self.stats._save()
        notify(
            f"🔄 <b>Session reset</b>\n"
            f"Previous: {old}\n"
            f"Stats wiped — starting fresh.\n"
            f"🕐 {now()}"
        )

    def _handle_win(self) -> None:
        self.stats.manual_result(won=True)
        notify(
            f"✅ <b>Win recorded manually</b>\n"
            f"📊 Session: {self.stats.summary()}\n"
            f"{self.stats.streak_label() or ''}\n"
            f"🕐 {now()}"
        )

    def _handle_loss(self) -> None:
        self.stats.manual_result(won=False)
        notify(
            f"❌ <b>Loss recorded manually</b>\n"
            f"📊 Session: {self.stats.summary()}\n"
            f"{self.stats.streak_label() or ''}\n"
            f"🕐 {now()}"
        )

    def _handle_stats(self) -> None:
        total = self.stats.wins + self.stats.losses
        rate = f"{self.stats.wins / total * 100:.0f}%" if total > 0 else "n/a"
        streak = self.stats.streak_label()
        notify(
            f"📊 <b>@ChillLobsterBot — Session Stats</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"✅ Wins:    <b>{self.stats.wins}</b>\n"
            f"❌ Losses:  <b>{self.stats.losses}</b>\n"
            f"🎯 Win rate: <b>{rate}</b>\n"
            + (f"{streak}\n" if streak else "")
            + f"━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now()}"
        )

    def _handle_next(self) -> None:
        secs = seconds_to_window_close() + 60
        mins, rem = divmod(secs, 60)
        t_str = f"{mins}m {rem}s" if mins > 0 else f"{rem}s"
        notify(
            f"⏰ <b>Next signal in ~{t_str}</b>\n"
            f"📲 @ChillLobsterBot  |  🕐 {now()}"
        )

    def _handle_predict(self) -> dict | None:
        """Predict the direction of the NEXT 15-minute Kalshi window."""
        try:
            import re as _re
            ohlcv_5m  = self._fetch_ohlcv("5m", 100)
            ohlcv_15m = self._fetch_ohlcv("15m", 30)
            ohlcv_1h  = self._fetch_ohlcv("1h", 30)
            ohlcv_1m  = self._fetch_ohlcv("1m", 10)
            closes  = [c[4] for c in ohlcv_5m]
            volumes = [c[5] for c in ohlcv_5m]
            cf_price = get_cf_price(closes[-1])
            kalshi_yes, kalshi_strike = fetch_kalshi_yes_price()
            funding_rate = fetch_funding_rate()
            votes = analyze(closes, volumes, ohlcv_5m, ohlcv_15m, ohlcv_1h, ohlcv_1m,
                            kalshi_yes, funding_rate, kalshi_strike)
            s = score(votes)
            n_up   = sum(1 for v in votes if v.direction > 0)
            n_down = sum(1 for v in votes if v.direction < 0)
            n_neut = sum(1 for v in votes if v.direction == 0)
            if s > 0 or (s == 0 and n_up >= n_down):
                pred_icon, pred_txt = "⬆️", "UP"
            else:
                pred_icon, pred_txt = "⬇️", "DOWN"
            # Strength label based on score magnitude
            abs_s = abs(s)
            if abs_s >= 5:
                strength = "Strong"
            elif abs_s >= 3:
                strength = "Moderate"
            else:
                strength = "Weak"
            # Time until next window
            secs_left = seconds_to_window_close()
            mins_left, rem_secs = divmod(secs_left, 60)
            time_str = f"{mins_left}m {rem_secs}s" if mins_left > 0 else f"{rem_secs}s"
            nxt_window = next_window_label()
            nxt_ticker = next_ticker_label()

            # Est. strike for the NEXT window — real Kalshi value if already
            # listed, otherwise an at-the-money + damped-drift estimate.
            next_strike_val, next_strike_is_real, next_strike_note = resolve_next_strike(
                cf_price, ohlcv_5m, kalshi_strike)
            if next_strike_is_real:
                next_strike_line = f"🎯 Next strike: <b>${next_strike_val:,.2f}</b>  (confirmed)\n"
            else:
                next_strike_line = f"🎯 Next strike (est.): <b>${next_strike_val:,.2f}</b>  <i>(forecast, not yet listed)</i>\n"

            # Record this call so it can be scored later against the NEXT
            # window's actual settlement — tracked separately as the
            # /predict-only W/L shown in the GUI's Predict card. Re-calling
            # /predict before that window closes just overwrites the pending
            # call with the latest read (only the most recent call before
            # close counts), matching how QQE's pending tracking works.
            next_end_iso = _window_end_utc(1).astimezone(ET).isoformat()
            self.stats.record_predict(pred_txt, cf_price, next_strike_val if next_strike_is_real else None,
                                       next_end_iso)

            notify(
                f"🔭 <b>Predict — Next Candle</b>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"Window: <b>{nxt_window}</b>\n"
                f"Call: {pred_icon} <b>{pred_txt}</b>  ({strength})\n"
                f"Score: {s:+d}  |  ▲{n_up} ▼{n_down} ─{n_neut} of {len(votes)} indicators\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"💰 BTC now (CF): <b>${cf_price:,.2f}</b>\n"
                f"{next_strike_line}"
                f"📈 Predict track record: <b>{self.stats.predict_summary()}</b>\n"
                f"⏰ Next window opens in <b>~{time_str}</b>\n"
                f"🔖 <code>{nxt_ticker}</code>\n"
                f"📲 @ChillLobsterBot  |  🕐 {now()}"
            )

            # Structured return, ADDITIVE to the notify() text above.
            return {
                "direction": pred_txt, "strength": strength, "score": s,
                "n_up": n_up, "n_down": n_down, "n_neut": n_neut, "n_total": len(votes),
                "cf_price": cf_price, "window": nxt_window, "ticker": nxt_ticker,
                "next_strike": next_strike_val, "next_strike_is_real": next_strike_is_real,
                "next_strike_note": next_strike_note,
                "predict_track_record": self.stats.predict_summary(),
                "time_until_open": time_str,
            }
        except Exception as e:
            notify(f"⚠️ Could not generate prediction: {e}")
            return None

    def _handle_guess(self) -> None:
        """Send the soft-read lean for the current window on demand."""
        try:
            import re as _re
            ohlcv_5m  = self._fetch_ohlcv("5m", 100)
            ohlcv_15m = self._fetch_ohlcv("15m", 30)
            ohlcv_1h  = self._fetch_ohlcv("1h", 30)
            ohlcv_1m  = self._fetch_ohlcv("1m", 10)
            closes  = [c[4] for c in ohlcv_5m]
            volumes = [c[5] for c in ohlcv_5m]
            cf_price = get_cf_price(closes[-1])
            kalshi_yes, kalshi_strike = fetch_kalshi_yes_price()
            funding_rate = fetch_funding_rate()
            votes = analyze(closes, volumes, ohlcv_5m, ohlcv_15m, ohlcv_1h, ohlcv_1m,
                            kalshi_yes, funding_rate, kalshi_strike)
            s = score(votes)
            direction, action, confidence, prob = recommendation(votes, cf_price)
            n_up   = sum(1 for v in votes if v.direction > 0)
            n_down = sum(1 for v in votes if v.direction < 0)
            n_neut = sum(1 for v in votes if v.direction == 0)
            if s > 0 or (s == 0 and n_up >= n_down):
                lean_icon, lean_txt = "⬆️", "UP"
            else:
                lean_icon, lean_txt = "⬇️", "DOWN"
            window = kalshi_window_label()
            ticker = kalshi_ticker_label()
            kalshi_line = (f"🏦 Kalshi: <b>YES @ {kalshi_yes:.0%}</b>\n"
                           if kalshi_yes is not None else "")
            if kalshi_strike is not None:
                gap = cf_price - kalshi_strike
                gap_tag = f"+${gap:,.2f} ABOVE ✅" if gap >= 0 else f"-${abs(gap):,.2f} BELOW ❌"
                target_line = f"📌 Target: <b>${kalshi_strike:,.2f}</b>  →  {gap_tag}\n"
            else:
                target_line = ""
            if confidence == "LEAN":
                note = "weak edge — <b>LEAN</b> (low-confidence best guess)"
            else:
                note = f"indicators agree — <b>{confidence}</b> signal"
            notify(
                f"🔮 <b>Guess — {window}</b>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"Guess: {lean_icon} <b>{lean_txt}</b>  "
                f"(score {s:+d} | ▲{n_up} ▼{n_down} ─{n_neut})\n"
                f"ℹ️ <i>{note}</i>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"💰 BTC (CF): <b>${cf_price:,.2f}</b>\n"
                f"{target_line}"
                f"{kalshi_line}"
                f"🔖 <code>{ticker}</code>\n"
                f"📲 @ChillLobsterBot  |  🕐 {now()}"
            )
        except Exception as e:
            notify(f"⚠️ Could not fetch guess: {e}")

    def _handle_signal(self) -> dict | None:
        try:
            ohlcv_5m  = self._fetch_ohlcv("5m", 100)
            ohlcv_15m = self._fetch_ohlcv("15m", 30)
            ohlcv_1h  = self._fetch_ohlcv("1h", 30)
            ohlcv_1m  = self._fetch_ohlcv("1m", 20)
            closes = [c[4] for c in ohlcv_5m]
            volumes = [c[5] for c in ohlcv_5m]
            cf_price = get_cf_price(closes[-1])
            kalshi_yes, kalshi_strike = fetch_kalshi_yes_price()
            funding_rate = fetch_funding_rate()
            votes = analyze(closes, volumes, ohlcv_5m, ohlcv_15m, ohlcv_1h, ohlcv_1m, kalshi_yes, funding_rate, kalshi_strike, weight_overrides=SESSION_STATE.get_weights())
            direction, action, confidence, prob = recommendation(votes, cf_price)

            # Kalshi crowd gate removed — see main loop. The crowd's YES price
            # doesn't decide the 15-min winner, so it no longer gates signals.

            # ATR flat-market filter: below 0.08% ATR there's no real edge — keep
            # the direction but downgrade to a low-confidence LEAN (no SKIP).
            atr_val = calc_atr(ohlcv_5m, 14)
            atr_pct = atr_val / cf_price * 100 if cf_price > 0 else 0
            # ATR threshold scaled by session vol multiplier: quiet session → lower threshold
            _atr_threshold = 0.08 / get_effective_vol_multiplier()
            if atr_pct < _atr_threshold and confidence != "LEAN":
                confidence, prob = "LEAN", 53.0
                action = lean_action(direction, "⚠️ flat market")

            # Circuit breaker: 3+ loss streak → stay directional but downgrade
            # confident calls to a low-confidence LEAN (cooldown, no SKIP).
            if confidence in ("MEDIUM", "HIGH", "STRONG") and self.stats.streak <= -3:
                confidence, prob = "LEAN", 54.0
                action = lean_action(direction, f"⚠️ cooldown ({abs(self.stats.streak)}-loss streak)")

            # Reversion Detector GATE — same logic and rationale as the main
            # loop's auto-fired signal (see there for the full explanation):
            # trend-following votes are most likely to be wrong exactly when
            # price is stretched (snap-back risk) or the bot's own calls have
            # been flip-flopping (read instability). Computed here BEFORE
            # set_why()/display so /bet's confidence matches what the main
            # loop would actually do with the same conditions, rather than
            # /bet showing confident while the next auto-fired signal (same
            # market conditions, moments later) gets silently downgraded.
            price_rev = detect_price_reversion(closes)
            signal_rev = detect_signal_reversal(self.stats, direction)
            if (price_rev["flagged"] or signal_rev["flagged"]) and confidence != "LEAN":
                confidence, prob = "LEAN", 52.0
                action = lean_action(direction, "⚠️ reversion risk")

            # Volatility Spike GATE — same logic as the main loop (see there
            # for full rationale): current ATR spiked well above its own
            # recent baseline means trend-following votes are most likely
            # chasing a move that's already reversing.
            vol_spike = detect_volatility_spike(ohlcv_5m)
            if vol_spike["flagged"] and confidence != "LEAN":
                confidence, prob = "LEAN", 52.0
                action = lean_action(direction, "⚠️ volatility spike")

            self.stats.set_why(votes)
            window = kalshi_window_label()
            streak = self.stats.streak_label()
            bet = bet_recommendation(confidence)
            ticker = kalshi_ticker_label()

            # ── QQE block — kept visually and structurally SEPARATE from the
            # overall bet call above. QQE is a single weighted x2 vote that
            # FEEDS the overall call, it is not the same thing as the call
            # itself, so this section explicitly states whether QQE agrees or
            # disagrees with `direction` rather than just printing its own
            # read next to the bet and letting the two run together.
            q_trend, q_rsima, q_line = qqe(closes)
            if q_trend != 0 and q_rsima is not None and q_line is not None:
                q_lean = "UP" if q_trend == 1 else "DOWN"
                q_icon = "🟢" if q_trend == 1 else "🔴"
                q_side = "BULLISH" if q_trend == 1 else "BEARISH"
                q_rel  = ">" if q_trend == 1 else "<"
                agree = (q_lean == direction)
                agree_tag = "✅ AGREES with overall call" if agree else "⚠️ DISAGREES with overall call"
                qqe_block = (
                    f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ QQE (separate read) ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    f"📈 QQE core momentum: {q_icon} <b>{q_side}</b> → leans <b>{q_lean}</b>  [{agree_tag}]\n"
                    f"RsiMa <b>{q_rsima:.1f}</b> {q_rel} stop <b>{q_line:.1f}</b>\n"
                    f"<i>(QQE is one weighted x2 input INTO the overall call above — not the call itself)</i>\n"
                    f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                )
            else:
                qqe_block = ""

            # ── Reversion Detector display — same flags computed above for
            # the gate, now formatted for the message. The gate already
            # downgraded confidence/action if either is flagged; this block
            # just shows WHY, same as the ATR/circuit-breaker notes embedded
            # in their respective lean_action() calls.
            rev_lines = []
            if price_rev["flagged"]:
                rev_lines.append(f"🔁 Price reversion: {price_rev['note']}")
            if signal_rev["flagged"]:
                rev_lines.append(f"🔁 Signal reversal: {signal_rev['note']}")
            reversion_block = (
                ("⚠️ <b>Reversion Detector</b>\n" + "\n".join(rev_lines) + "\n"
                 "━━━━━━━━━━━━━━━━━\n") if rev_lines else ""
            )

            notify(
                f"⚡ <b>ChillLobsterBTC — {window} [ON DEMAND]</b>\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{action}\n"
                f"🎯 Confidence: <b>{confidence}</b>  |  Prob: <b>{prob}%</b>\n"
                f"{bet}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{qqe_block}"
                f"{reversion_block}"
                + (f"{streak}\n" if streak else "")
                + f"📊 Session: {self.stats.summary()}\n"
                f"🔖 <code>{ticker}</code>\n"
                f"📲 @ChillLobsterBot  |  🕐 {now()}"
            )

            # Structured return, ADDITIVE to the notify() text above (no
            # change to existing behavior) — lets monitor.py's GUI build a
            # proper panel layout instead of parsing the HTML-tagged text.
            return {
                "direction": direction, "confidence": confidence, "prob": prob,
                "cf_price": cf_price, "kalshi_strike": kalshi_strike, "kalshi_yes": kalshi_yes,
                "window": window, "ticker": ticker,
                "qqe_agrees": (q_lean == direction) if q_trend != 0 else None,
                "qqe_lean": q_lean if q_trend != 0 else None,
                "price_reversion_flagged": price_rev["flagged"],
                "price_reversion_note": price_rev["note"],
                "signal_reversal_flagged": signal_rev["flagged"],
                "signal_reversal_note": signal_rev["note"],
                "volatility_spike_flagged": vol_spike["flagged"],
                "volatility_spike_note": vol_spike["note"],
                "streak": self.stats.streak,
                "session_summary": self.stats.summary(),
            }
        except Exception as e:
            notify(f"⚠️ Could not fetch signal: {e}")
            return None

    def run(self) -> None:
        # Telegram command polling has been removed — monitor.py's GUI calls
        # the _handle_* methods directly instead of going through here. This
        # loop is kept only so .start() (called from main()) still works.
        print(f"[{now()}] Command poller started (idle — GUI drives commands now)")
        while True:
            time.sleep(60)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _record_fallback_signal_if_needed(stats: "Stats", last_known_price: float | None,
                                        error: Exception) -> None:
    """
    Safety net for the main loop's broad except handlers: if this cycle's
    signal-firing block threw an exception BEFORE reaching record_signal(),
    stats.pending stays None (resolve() already cleared it earlier in the
    same cycle) for the rest of that 15-minute window — the GUI then shows
    "No open bet" even though a Kalshi window is genuinely live the whole
    time. This produced exactly that symptom in practice.

    Rather than leave pending blank, record a clearly-labeled DEGRADED
    fallback signal so the GUI always reflects an active call during a live
    window, using whatever data survived the failure:
      - direction: the LAST RECORDED signal's direction if one exists (a
        real prior signal, not invented data) — defaults to "UP" only if
        there's no history at all to fall back on, and that default is
        explicitly flagged in the recorded confidence/window text so it's
        never mistaken for an actual analyzed call.
      - price: last_known_price if available, else 0.0 (resolve() will
        still settle this against the Kalshi strike when one is available,
        same as any other pending signal).
    This does NOT run analyze() or any indicator — it deliberately makes no
    claim about market direction beyond "repeat the last known call", since
    fabricating a fresh analytical read from missing data would be worse
    than honestly flagging degraded data.
    """
    if stats.pending is not None:
        return  # a real signal already got recorded this cycle — nothing to do

    last_signal = (stats.signals or [])[-1] if stats.signals else None
    direction = last_signal["direction"] if last_signal else "UP"
    price = last_known_price if last_known_price is not None else 0.0
    window = kalshi_window_label()

    print(f"[{now()}] Fallback signal: recording DEGRADED {direction} call for {window} "
          f"after cycle failure ({error}) — pending would otherwise stay empty all window")
    stats.record_signal(direction, price, window, "LEAN", 50.0, None)
    # Tag this entry as degraded in both pending and the just-appended
    # signals-history row, so anything reading it (GUI, /why, History tab)
    # can tell it apart from a normal analyzed call rather than silently
    # presenting fabricated-looking confidence.
    if stats.pending is not None:
        stats.pending["degraded"] = True
        stats.pending["degraded_reason"] = str(error)
    if stats.signals:
        stats.signals[-1]["degraded"] = True
        stats._save_signals()


# ── Unified signal wrapper (V0.1) ────────────────────────────────────────────
# Single entry point both streamlit_app.py and desktop_app.py call. Bundles
# exchange connect + blended OHLCV + Kalshi market data + the confluence /
# Bridge / Physics / Synthesis models into one structured dict, generalized
# across every asset in SYMBOLS and across both the live 15-min Kalshi window
# and a 1-hour-ahead horizon.
#
# For the 1-hour horizon there is no Kalshi strike market to bet against, so
# the "strike" fed into Bridge/Physics/Synthesis is simply CURRENT spot —
# i.e. the question becomes "will price in 1h be above right now's price",
# which is exactly the kind of binary-threshold question those models are
# built to answer, just reusing spot as the threshold instead of a Kalshi
# floor_strike. Same applies to assets with has_kalshi=False on the 15m tab.
_EXCHANGE_CACHE: dict[str, tuple] = {}


def _get_exchanges(asset_code: str) -> tuple[dict, str]:
    if asset_code not in _EXCHANGE_CACHE:
        _EXCHANGE_CACHE[asset_code] = build_exchanges(asset_code)
    return _EXCHANGE_CACHE[asset_code]


# ── Final Call stability (hysteresis / debounce) ────────────────────────────
# get_signal() is polled roughly once a second. The raw confluence/synthesis
# read can flicker tick to tick on noise alone — flipping the headline "Final
# Call" every few seconds is actively harmful (it's what the user bets on,
# not a live readout). This layer sits between the raw candidate call and
# what actually gets displayed/recorded as the Final Call, and only accepts
# a DIRECTION CHANGE when three things all agree:
#   1. Minimum hold time has passed since the last change (no instant flaps).
#   2. The underlying spot-vs-strike gap actually supports the new direction,
#      by a margin that scales with TIME DECAY — early in the window (lots
#      of time left) a flip needs a bigger, more convincing move, since
#      there's plenty of time for it to reverse again; late in the window
#      (little time left) a smaller move is enough to flip, since there's
#      little time left for it to un-happen — same logic as why an option's
#      price converges to intrinsic value as expiry approaches.
#   3. The recent rolling average of spot is trending the same way as the
#      candidate flip, not just one noisy tick against the grain.
# If a flip is rejected, the PREVIOUS Final Call is returned unchanged along
# with held=True so the UI can show "holding" instead of silently freezing.
WINDOW_TOTAL_SECONDS = {"15m": 900, "1h": 3600}
FINAL_CALL_MIN_HOLD_SECONDS = 30      # shortest time between accepted flips
FINAL_CALL_WARMUP_SECONDS = 60        # first minute of every window: track freely, no flip-gating yet
FINAL_CALL_BASE_THRESHOLD_PCT = 0.05  # required |gap%| to flip with a full window remaining
FINAL_CALL_FLOOR_THRESHOLD_PCT = 0.01  # never go below this even at the very end of the window
FINAL_CALL_HISTORY_LEN = 8            # how many recent spot samples feed the rolling-average check

_final_call_state: dict[tuple, dict] = {}  # key = (asset, horizon, window_label)


# ── Ensemble: Confluence+Synthesis consensus, blended with Polymarket ──────
# IMPORTANT: Synthesis is NOT independent of Confluence/Bridge/Physics — its
# own internal weights (SYNTHESIS_WEIGHT_*, just above) already blend
# Averaging=Confluence(30%), Distribution=Physics(20%), and
# Momentum+Supporting=Bridge's sub-models(40%+10%). An earlier version of
# this ensemble treated Confluence/Synthesis/Bridge/Physics as 4 independent
# votes, which silently double- and triple-counted Confluence, Bridge, and
# Physics (each counted once directly AND again inside Synthesis).
#
# The correct top-level blend, matching this project's own documented
# "Guess tab" spec (Bet 60% / Synthesis 40%), has only ONE consensus term:
#   model_consensus = 0.60 * Confluence + 0.40 * Synthesis
# Bridge and Physics remain fully visible in the UI and keep their own live
# win-rate trackers — but as BENCHMARKS (like Polymarket-alone), not as
# additional top-level votes, since they're already inside Synthesis.
# Polymarket is the one genuinely independent addition: a different crowd's
# money on the same window, so it earns a real seat in the blend.
CONSENSUS_SPLIT = {"confluence": 0.60, "synthesis": 0.40}  # matches documented Guess-tab ratio
ENSEMBLE_BASE_WEIGHTS = {"model_consensus": 0.80, "polymarket": 0.20}
TRACK_RECORD_MIN_SAMPLES = 8   # below this many resolved calls, stay neutral (1.0x)


def _track_record_multiplier(wins: int, losses: int) -> float:
    """Maps a model's measured win rate to a weight multiplier: 50% win rate
    -> 1.0x (neutral), up to 1.5x at 100%, down to 0.5x at 0%. Not enough
    resolved samples yet -> 1.0x, since one good/bad streak shouldn't swing
    real money decisions before there's enough data to trust it."""
    n = wins + losses
    if n < TRACK_RECORD_MIN_SAMPLES:
        return 1.0
    win_rate = wins / n
    return max(0.5, min(1.5, 0.5 + win_rate))


def combine_model_probabilities(asset_code: str, conf_direction: str, conf_prob_pct: float,
                                 synth: dict | None, bridge: dict | None,
                                 physics: dict | None, poly: dict | None) -> dict:
    """Returns {"prob_up": float (0-100), "direction": "UP"/"DOWN",
    "confidence": tier str, "weights_used": dict, "components": dict}.
    weights_used/components report CONFLUENCE and SYNTHESIS at their
    effective top-level share (model_consensus weight x their 60/40 split)
    purely for UI transparency — Bridge/Physics are intentionally absent
    from this dict since they don't carry a separate top-level vote."""
    stats = Stats(asset_code)

    conf_prob_up = conf_prob_pct if conf_direction == "UP" else (100 - conf_prob_pct)
    if synth and isinstance(synth.get("blended_prob"), (int, float)):
        synth_prob_up = synth["blended_prob"] * 100
        consensus_prob_up = (CONSENSUS_SPLIT["confluence"] * conf_prob_up
                              + CONSENSUS_SPLIT["synthesis"] * synth_prob_up)
        synth_available = True
    else:
        consensus_prob_up = conf_prob_up
        synth_available = False

    consensus_mult = _track_record_multiplier(stats.wins, stats.losses)
    weights = {"model_consensus": ENSEMBLE_BASE_WEIGHTS["model_consensus"] * consensus_mult}
    components = {"model_consensus": consensus_prob_up}

    if poly and poly.get("available") and isinstance(poly.get("up_pct"), (int, float)):
        poly_mult = _track_record_multiplier(stats.poly_wins, stats.poly_losses)
        weights["polymarket"] = ENSEMBLE_BASE_WEIGHTS["polymarket"] * poly_mult
        components["polymarket"] = poly["up_pct"]

    total_weight = sum(weights.values())
    prob_up = (sum(components[k] * weights[k] for k in components) / total_weight) if total_weight > 0 else conf_prob_up

    direction = "UP" if prob_up >= 50 else "DOWN"
    dist = abs(prob_up - 50)
    if dist >= 18:
        confidence = "STRONG"
    elif dist >= 14:
        confidence = "HIGH"
    elif dist >= 10:
        confidence = "MEDIUM"
    else:
        confidence = "LEAN"

    # Expand model_consensus back into its Confluence/Synthesis shares for
    # display only, so the UI's "influence" ranking still shows each by name.
    consensus_w = weights["model_consensus"]
    display_weights = {"confluence": round(consensus_w * CONSENSUS_SPLIT["confluence"], 4)}
    display_components = {"confluence": round(conf_prob_up, 2)}
    if synth_available:
        display_weights["synthesis"] = round(consensus_w * CONSENSUS_SPLIT["synthesis"], 4)
        display_components["synthesis"] = round(synth_prob_up, 2)
    if "polymarket" in weights:
        display_weights["polymarket"] = round(weights["polymarket"], 4)
        display_components["polymarket"] = round(components["polymarket"], 2)

    return {"prob_up": round(prob_up, 2), "direction": direction, "confidence": confidence,
            "weights_used": display_weights, "components": display_components}


def _final_call_required_threshold_pct(seconds_remaining: int, window_total: int) -> float:
    """Time-decay-scaled price-move requirement to accept a direction flip.
    Shrinks toward FINAL_CALL_FLOOR_THRESHOLD_PCT as the window runs out."""
    frac_remaining = max(0.0, min(1.0, seconds_remaining / window_total))
    return max(FINAL_CALL_FLOOR_THRESHOLD_PCT, FINAL_CALL_BASE_THRESHOLD_PCT * frac_remaining)


def apply_final_call_stability(asset_code: str, horizon: str, window_label: str,
                                candidate_direction: str, candidate_confidence: str,
                                candidate_prob: float, spot: float, strike: float,
                                seconds_remaining: int) -> dict:
    """Returns {"direction", "confidence", "prob", "held", "reason", "locked_since_iso", "warmup"}.

    First FINAL_CALL_WARMUP_SECONDS of every window: tracks the live candidate
    directly (no flip-gating) since there isn't enough in-window data yet for
    a real "Final Call" — this is the noisy/static period. Once warmup ends,
    whatever the candidate is AT THAT MOMENT becomes the locked solid pick,
    and every direction change after that has to clear the full stability
    bar (min hold + time-decay-scaled price move + trend confirmation)."""
    key = (asset_code, horizon, window_label)
    window_total = WINDOW_TOTAL_SECONDS.get(horizon, 900)
    seconds_into_window = max(0, window_total - seconds_remaining)
    now_ts = time.time()

    state = _final_call_state.get(key)
    if state is None:
        state = {
            "direction": candidate_direction, "confidence": candidate_confidence,
            "prob": candidate_prob, "locked_at": now_ts, "spots": [(now_ts, spot)],
            "warmup": seconds_into_window < FINAL_CALL_WARMUP_SECONDS,
        }
        _final_call_state[key] = state
        # Drop any stale keys for old windows of this same asset/horizon so the
        # cache doesn't grow forever across a long-running process.
        for k in [k for k in _final_call_state if k[0] == asset_code and k[1] == horizon and k != key]:
            del _final_call_state[k]
        reason = "gathering first-minute data" if state["warmup"] else "first read this window"
        return {"direction": candidate_direction, "confidence": candidate_confidence,
                "prob": candidate_prob, "held": False, "reason": reason, "warmup": state["warmup"],
                "locked_since_iso": datetime.fromtimestamp(now_ts, ET).isoformat()}

    state["spots"].append((now_ts, spot))
    state["spots"] = state["spots"][-FINAL_CALL_HISTORY_LEN:]

    if state.get("warmup"):
        if seconds_into_window < FINAL_CALL_WARMUP_SECONDS:
            # Still gathering data: track the live candidate directly, no
            # flip-gating yet -- nothing to "hold" against during warmup.
            state["direction"] = candidate_direction
            state["confidence"] = candidate_confidence
            state["prob"] = candidate_prob
            return {"direction": state["direction"], "confidence": state["confidence"],
                    "prob": state["prob"], "held": False, "reason": "gathering first-minute data",
                    "warmup": True, "locked_since_iso": datetime.fromtimestamp(now_ts, ET).isoformat()}
        # Warmup just ended: lock in a solid pick from the current candidate
        # and restart the min-hold clock from right now.
        state["warmup"] = False
        state["direction"] = candidate_direction
        state["confidence"] = candidate_confidence
        state["prob"] = candidate_prob
        state["locked_at"] = now_ts
        return {"direction": state["direction"], "confidence": state["confidence"],
                "prob": state["prob"], "held": False,
                "reason": "solid pick locked in after first-minute warmup", "warmup": False,
                "locked_since_iso": datetime.fromtimestamp(now_ts, ET).isoformat()}

    if candidate_direction == state["direction"]:
        # Same direction: let the displayed probability/confidence track the
        # live read directly (lightly smoothed) — no stability concern when
        # the call itself isn't changing.
        state["prob"] = round(state["prob"] * 0.5 + candidate_prob * 0.5, 4)
        state["confidence"] = candidate_confidence
        return {"direction": state["direction"], "confidence": state["confidence"],
                "prob": state["prob"], "held": False, "reason": "unchanged", "warmup": False,
                "locked_since_iso": datetime.fromtimestamp(state["locked_at"], ET).isoformat()}

    # Candidate disagrees with the locked direction -> evaluate whether to flip.
    held_seconds = now_ts - state["locked_at"]
    if held_seconds < FINAL_CALL_MIN_HOLD_SECONDS:
        return {"direction": state["direction"], "confidence": state["confidence"],
                "prob": state["prob"], "held": True, "warmup": False,
                "reason": f"min hold not met ({held_seconds:.0f}s/{FINAL_CALL_MIN_HOLD_SECONDS}s)",
                "locked_since_iso": datetime.fromtimestamp(state["locked_at"], ET).isoformat()}

    gap_pct = ((spot - strike) / strike * 100) if strike else 0.0
    required = _final_call_required_threshold_pct(seconds_remaining, window_total)
    gap_supports_flip = (gap_pct > 0) == (candidate_direction == "UP")
    if not gap_supports_flip or abs(gap_pct) < required:
        return {"direction": state["direction"], "confidence": state["confidence"],
                "prob": state["prob"], "held": True, "warmup": False,
                "reason": f"market gap {gap_pct:+.3f}% doesn't yet clear the "
                          f"{required:.3f}% bar needed with {seconds_remaining}s left",
                "locked_since_iso": datetime.fromtimestamp(state["locked_at"], ET).isoformat()}

    # Rolling-average sanity check: the recent trend itself should already be
    # leaning the new way, not just the single latest tick.
    spots = state["spots"]
    if len(spots) >= 3:
        half = len(spots) // 2
        avg_first = sum(p for _, p in spots[:half]) / half
        avg_second = sum(p for _, p in spots[half:]) / (len(spots) - half)
        trend_supports_flip = (avg_second > avg_first) == (candidate_direction == "UP")
        if not trend_supports_flip:
            return {"direction": state["direction"], "confidence": state["confidence"],
                    "prob": state["prob"], "held": True, "warmup": False,
                    "reason": "recent average trend doesn't confirm the flip yet",
                    "locked_since_iso": datetime.fromtimestamp(state["locked_at"], ET).isoformat()}

    # All checks passed: accept the flip.
    state["direction"] = candidate_direction
    state["confidence"] = candidate_confidence
    state["prob"] = candidate_prob
    state["locked_at"] = now_ts
    return {"direction": state["direction"], "confidence": state["confidence"],
            "prob": state["prob"], "held": False, "warmup": False,
            "reason": f"flip confirmed: gap {gap_pct:+.3f}% cleared {required:.3f}% bar",
            "locked_since_iso": datetime.fromtimestamp(now_ts, ET).isoformat()}


def get_signal(asset_code: str = DEFAULT_ASSET, horizon: str = "15m") -> dict:
    """
    Returns a structured dict describing the current best call for
    `asset_code` over `horizon` ("15m" or "1h"). Never raises — fills
    "error" on failure so the UI can show a clean message instead of
    crashing the refresh loop.
    """
    info = SYMBOLS.get(asset_code, SYMBOLS[DEFAULT_ASSET])
    out: dict = {"asset": asset_code, "name": info["name"], "horizon": horizon,
                 "error": None, "ts": datetime.now(ET).isoformat()}
    try:
        exchanges, symbol = _get_exchanges(asset_code)
    except SystemExit:
        out["error"] = "No exchange reachable for this asset right now."
        return out

    try:
        ohlcv_5m = fetch_blended_ohlcv(exchanges, symbol, "5m", 100)
        ohlcv_15m = fetch_blended_ohlcv(exchanges, symbol, "15m", 60)
        ohlcv_1h = fetch_blended_ohlcv(exchanges, symbol, "1h", 60)
        # 1m candles power the "Window Body" indicator's primary signal (the
        # current window's first-minute direction) — without this it was
        # silently falling back to a weaker 3-candle 5m check every time.
        try:
            ohlcv_1m = fetch_blended_ohlcv(exchanges, symbol, "1m", 20)
        except Exception:
            ohlcv_1m = None
    except Exception as e:
        out["error"] = f"Could not fetch candles: {e}"
        return out

    closes_5m = [c[4] for c in ohlcv_5m]
    volumes_5m = [c[5] for c in ohlcv_5m]
    last_close = closes_5m[-1] if closes_5m else 0.0
    spot = get_cf_price(last_close, asset_code)

    out["price"] = spot

    kalshi_yes, strike, ticker = None, None, None
    if horizon == "15m" and info["has_kalshi"]:
        mkt = fetch_kalshi_market_for_asset(info["kalshi_code"])
        if mkt["available"]:
            kalshi_yes, strike, ticker = mkt["yes_mid"], mkt["strike"], mkt["ticker"]
    if strike is None:
        # No real Kalshi strike (unsupported asset, or 1h horizon): use spot
        # itself as the binary threshold so Bridge/Physics/Synthesis still
        # answer a coherent "above/below this reference price" question.
        strike = spot

    # Order book imbalance: prefer Kalshi's OWN order book for this exact
    # market (one reliable call, directly relevant to this window) over
    # scanning 4 separate crypto exchanges (4x the network calls, 4x the
    # chance a single timeout/rate-limit blanks the whole reading). Large-
    # trade flow still comes from the crypto exchanges since Kalshi doesn't
    # expose a comparable public trade tape for this.
    whale = None
    kalshi_book = None
    try:
        whale = detect_whale_activity(exchanges, symbol)
        out["whale"] = {"state": whale["state"], "confidence": whale["confidence"], "note": whale["note"]}
    except Exception as e:
        out["whale_error"] = str(e)
    if ticker:
        try:
            kalshi_book = fetch_kalshi_orderbook_imbalance(ticker)
        except Exception as e:
            out["kalshi_book_error"] = str(e)

    book_score_for_votes = kalshi_book["book_score"] if kalshi_book else (whale.get("book_score") if whale else None)
    if kalshi_book:
        out["order_book_source"] = "kalshi"
    elif whale and whale.get("book_score") is not None:
        out["order_book_source"] = "exchanges"

    # Independent second opinion: Polymarket's own crowd-priced odds for the
    # SAME 15-minute window (currently BTC-only product). This is never used
    # to influence our own confluence/votes — it's tracked side-by-side purely
    # as a benchmark to tell us whether our model is beating a different
    # crowd's price, or just restating it.
    poly = None
    if horizon == "15m" and asset_code == "BTC":
        poly = fetch_polymarket_15m()
        if poly.get("available"):
            out["polymarket"] = {
                "up_pct": poly["up_pct"], "down_pct": poly["down_pct"],
                "volume": poly.get("volume"),
            }

    seconds_remaining = seconds_to_window_close() if horizon == "15m" else 3600
    primary_closes = closes_5m if horizon == "15m" else [c[4] for c in ohlcv_1h]
    primary_volumes = volumes_5m if horizon == "15m" else [c[5] for c in ohlcv_1h]

    votes = analyze(primary_closes, primary_volumes,
                     ohlcv_5m=ohlcv_5m, ohlcv_15m=ohlcv_15m, ohlcv_1h=ohlcv_1h, ohlcv_1m=ohlcv_1m,
                     kalshi_yes=kalshi_yes, kalshi_strike=strike if horizon == "15m" else None,
                     whale_book_score=book_score_for_votes,
                     whale_trade_score=(whale.get("trade_score") if whale else None))
    direction, action, confidence, prob = recommendation(votes, spot)
    out.update({
        "direction": direction, "action": strip_tags(action),
        "confidence": confidence, "confluence_prob": prob,
        "votes": [(v.label, v.direction, v.detail) for v in votes],
        "kalshi_yes": kalshi_yes, "strike": strike if (horizon == "15m" and info["has_kalshi"]) else None,
        "ticker": ticker,
    })

    whale_trade = whale.get("trade_score", 0.0) if whale else 0.0
    whale_book = book_score_for_votes if book_score_for_votes is not None else 0.0

    try:
        synth = compute_synthesis(spot, strike, kalshi_yes, ohlcv_5m, ohlcv_15m,
                                   ohlcv_1h, ohlcv_1m, seconds_remaining, kalshi_yes=kalshi_yes,
                                   whale_trade_score=whale_trade, whale_book_score=whale_book)
        out["blended_prob"] = synth.get("blended_prob")
        out["synthesis_signal"] = synth.get("signal")
        out["synthesis_confidence"] = synth.get("confidence")
        out["synthesis_components"] = synth.get("component_probs")
    except Exception as e:
        out["synthesis_error"] = str(e)

    if out["asset"] in ("BTC", "ETH") or strike != spot:
        try:
            out["bridge"] = compute_bridge(spot, strike, kalshi_yes, ohlcv_5m, seconds_remaining,
                                            kalshi_yes=kalshi_yes)
        except Exception as e:
            out["bridge_error"] = str(e)
        try:
            out["physics"] = compute_physics(spot, strike, ohlcv_5m, seconds_remaining,
                                              whale_trade_score=whale_trade, whale_book_score=whale_book)
        except Exception as e:
            out["physics_error"] = str(e)

    out["window_label"] = kalshi_window_label() if horizon == "15m" else "next 60 minutes"
    out["bet_size"] = bet_recommendation(confidence)

    # ── Ensemble: blend Confluence + Synthesis + Bridge + Physics + Polymarket
    # weighted by each model's own measured win rate (see combine_model_
    # probabilities' docstring). This combined read — not the raw confluence-
    # only vote — is what becomes the Final Call candidate below.
    synth_dict = {"blended_prob": out.get("blended_prob"), "signal": out.get("synthesis_signal")} \
        if out.get("blended_prob") is not None else None
    ensemble = combine_model_probabilities(asset_code, direction, prob, synth_dict,
                                            out.get("bridge"), out.get("physics"), poly)
    out["ensemble"] = ensemble

    # ── Final Call (stabilized) ─────────────────────────────────────────
    # This is what actually gets shown as the headline pick + recorded for
    # W/L. It only changes direction when the move clears the time-decay-
    # scaled bar above — see apply_final_call_stability's docstring.
    # IMPORTANT: ensemble["prob_up"] is P(UP) specifically — when the picked
    # direction is DOWN that number is necessarily < 50, so it must be
    # flipped to P(DOWN) = 100 - prob_up before being treated as "confidence
    # in the picked direction" everywhere downstream. Passing prob_up
    # through unconverted was exactly the "29% DOWN next to a 71% UP bar"
    # bug — the direction was right, but its confidence number was the
    # OTHER direction's probability.
    candidate_prob_of_direction = ensemble["prob_up"] if ensemble["direction"] == "UP" else (100 - ensemble["prob_up"])
    fc = apply_final_call_stability(asset_code, horizon, out["window_label"],
                                     ensemble["direction"], ensemble["confidence"], candidate_prob_of_direction,
                                     spot, out.get("strike") or strike, seconds_remaining)
    out["final_call"] = {
        "direction": fc["direction"], "confidence": fc["confidence"],
        "prob": fc["prob"], "held": fc["held"], "reason": fc["reason"], "warmup": fc.get("warmup", False),
        "locked_since_iso": fc["locked_since_iso"],
    }

    # How many of the confluence indicators actually agree with the Final
    # Call being shown (not just the raw confluence-only direction) — gives
    # an at-a-glance sense of how unanimous this pick really is.
    decisive_votes = [v for v in votes if v.direction != 0]
    fc_dir_sign = 1 if fc["direction"] == "UP" else -1
    agree_count = sum(1 for v in decisive_votes if v.direction == fc_dir_sign)
    out["indicator_agreement"] = {
        "agree": agree_count, "total_decisive": len(decisive_votes), "total_registered": len(votes),
    }

    if horizon == "15m":
        try:
            poly_stats = Stats(asset_code)
            out["poly_record"] = poly_stats.poly_summary()
            out["bridge_record"] = poly_stats.bridge_summary()
            out["physics_record"] = poly_stats.physics_summary()
        except Exception:
            pass

    if horizon == "15m":
        try:
            # Record the STABILIZED final call, not the raw tick-to-tick
            # candidate, so W/L tracking matches what the user actually saw.
            _track_stats(asset_code, fc["direction"], spot, out["window_label"],
                         fc["confidence"], fc["prob"], out.get("strike"))
        except Exception as e:
            out["stats_error"] = str(e)
        if poly and poly.get("available"):
            try:
                _track_poly_stats(asset_code, poly, out["window_label"], out.get("strike"), spot)
            except Exception as e:
                out["poly_stats_error"] = str(e)
        if out.get("bridge"):
            try:
                _track_model_stats(asset_code, out["bridge"].get("signal"), out.get("strike"), spot,
                                    "record_bridge", "resolve_bridge", "bridge_pending")
            except Exception as e:
                out["bridge_stats_error"] = str(e)
        if out.get("physics"):
            try:
                _track_model_stats(asset_code, out["physics"].get("signal"), out.get("strike"), spot,
                                    "record_physics", "resolve_physics", "physics_pending")
            except Exception as e:
                out["physics_stats_error"] = str(e)

    return out


def _track_stats(asset_code: str, direction: str, price: float, window_label: str,
                  confidence: str, prob: float, strike: float | None) -> None:
    """Lightweight win/loss tracker driven purely by polling get_signal() —
    no separate always-on bot process required. Each call:
      - if there's no pending call yet, records this window's call;
      - if the window has rolled over since the last recorded call, resolves
        the PREVIOUS window's call against the current spot price (a same-
        instant proxy for Kalshi's 60s settlement average — close enough
        immediately after rollover, though not bit-for-bit identical to the
        bot's original settlement capture) and records the new window's call.
      - if we're still inside the same window as the last recorded call,
        does nothing (avoids re-recording on every poll).
    """
    stats = Stats(asset_code)
    if stats.pending is None:
        stats.record_signal(direction, price, window_label, confidence, prob, strike)
    elif stats.pending.get("window") != window_label:
        stats.resolve(price)
        stats.record_signal(direction, price, window_label, confidence, prob, strike)
    # else: same window as last recorded call — nothing to do yet.


def _track_poly_stats(asset_code: str, poly: dict, window_label: str,
                       strike: float | None, spot: float) -> None:
    """Mirrors _track_stats but for Polymarket's independent call, so its
    win/loss record accumulates purely from polling — no extra process
    needed, same as everything else in this app."""
    stats = Stats(asset_code)
    poly_direction = "UP" if poly.get("up_pct", 0) > 50 else "DOWN"
    if stats.poly_pending is None:
        window_end = datetime.now(ET).replace(second=0, microsecond=0)
        window_end += timedelta(minutes=15 - (window_end.minute % 15))
        stats.record_poly(poly_direction, strike, window_end.isoformat())
    elif stats.poly_pending.get("window_end_iso") and stats._pending_expired(stats.poly_pending):
        stats.resolve_poly(spot)
        window_end = datetime.now(ET).replace(second=0, microsecond=0)
        window_end += timedelta(minutes=15 - (window_end.minute % 15))
        stats.record_poly(poly_direction, strike, window_end.isoformat())


def _track_model_stats(asset_code: str, model_signal: str | None, strike: float | None,
                        spot: float, record_fn_name: str, resolve_fn_name: str,
                        pending_attr: str) -> None:
    """Generic poll-driven tracker for any model that already has
    record_*/resolve_* methods on Stats (Bridge, Physics) — same lightweight
    pattern as _track_poly_stats, just parameterized so it isn't copy-pasted
    per model. NEUTRAL signals are skipped (nothing to score)."""
    if model_signal not in ("ABOVE", "BELOW", "UP", "DOWN"):
        return
    stats = Stats(asset_code)
    record_fn = getattr(stats, record_fn_name)
    resolve_fn = getattr(stats, resolve_fn_name)
    pending = getattr(stats, pending_attr)
    window_end = datetime.now(ET).replace(second=0, microsecond=0)
    window_end += timedelta(minutes=15 - (window_end.minute % 15))
    if pending is None:
        record_fn(model_signal, strike, window_end.isoformat())
    elif pending.get("window_end_iso") and stats._pending_expired(pending):
        resolve_fn(spot)
        record_fn(model_signal, strike, window_end.isoformat())


def format_signal_for_discord(sig: dict) -> str:
    """Turns a get_signal() dict into the 'bet you should place' Discord
    message — confluence call + size + probability + price + (strike if any).
    Uses the STABILIZED Final Call, not the raw tick-to-tick read, so Discord
    never alerts on a flip the dashboard itself didn't accept."""
    if sig.get("error"):
        return f"⚠️ {sig['name']} ({sig['horizon']}): {sig['error']}"
    fc = sig.get("final_call") or {
        "direction": sig["direction"], "confidence": sig["confidence"],
        "prob": sig["blended_prob"] * 100 if isinstance(sig.get("blended_prob"), float) else sig["confluence_prob"],
    }
    icon = "🟢" if fc["direction"] == "UP" else "🔴"
    prob_pct = fc["prob"]
    lines = [
        f"⚡ **{sig['name']} ({sig['asset']}) — {sig['window_label']}**",
        f"{icon} **{fc['direction']}**  ·  Confidence: **{fc['confidence']}**  ·  Prob: **{prob_pct:.0f}%**",
        f"{sig['bet_size']}",
        f"💰 Price: ${sig['price']:,.2f}" if sig.get("price") else "",
    ]
    if sig.get("strike") is not None:
        gap = sig["price"] - sig["strike"]
        lines.append(f"📌 Strike: ${sig['strike']:,.2f}  (gap {gap:+,.2f})")
    if sig.get("ticker"):
        lines.append(f"🔖 {sig['ticker']}")
    if sig.get("polymarket"):
        pm = sig["polymarket"]
        agree = "✅ agrees" if (pm["up_pct"] > 50) == (sig["direction"] == "UP") else "⚠️ disagrees"
        lines.append(f"🔮 Polymarket: UP {pm['up_pct']:.0f}% / DOWN {pm['down_pct']:.0f}%  ({agree})")
    if sig.get("poly_record"):
        lines.append(f"📊 Polymarket-alone record: {sig['poly_record']}")
    return "\n".join(l for l in lines if l)


def main(asset_code: str = DEFAULT_ASSET) -> None:
    print(f"[{now()}] @ChillLobsterBot starting — Kalshi 15-min {asset_code} mode")
    exchanges, symbol = build_exchanges(asset_code)

    weight_summary = ", ".join(f"{n} {w:.0%}" for n, (_, w) in exchanges.items())
    notify(
        f"🤖 <b>@ChillLobsterBot online</b>\n"
        f"📡 Kalshi 15-min BTC mode\n"
        f"⚖️ Feed: {weight_summary}\n"
        f"🕐 :01 lean · 6-min PLACE BET · 2-min confirm\n"
        f"🖥️ Use monitor.py for the live GUI\n"
        f"⏰ {now()}"
    )

    stats = Stats(asset_code)

    # ── Auto-resolve any pending signal that expired while the bot was offline ──
    if stats.pending_expired() or stats.qqe_pending_expired() or stats.predict_pending_expired():
        print(f"[{now()}] Pending signal expired while offline — resolving now")
        try:
            _ohlcv_15m = fetch_blended_ohlcv(exchanges, symbol, "15m", 5)
            # Use the most recently closed 15m candle's close as settlement price.
            # If the pending window has expired, ohlcv_15m[-1] may still be forming
            # so use [-2] (last fully closed candle) to match Kalshi settlement.
            if _ohlcv_15m and len(_ohlcv_15m) >= 2:
                _settlement = _ohlcv_15m[-2][4]
            else:
                _ohlcv_5m = fetch_blended_ohlcv(exchanges, symbol, "5m", 2)
                _settlement = get_cf_price(_ohlcv_5m[-1][4])
            result_msg = stats.resolve(_settlement) if stats.pending_expired() else None
            if stats.qqe_pending_expired():
                stats.resolve_qqe(_settlement)
            if stats.predict_pending_expired():
                stats.resolve_predict(_settlement)
            if stats._pending_expired(stats.bridge_pending):
                stats.resolve_bridge(_settlement)
            if stats._pending_expired(stats.physics_pending):
                stats.resolve_physics(_settlement)
            if result_msg:
                notify(result_msg)
                print(f"[{now()}] Catch-up result sent | {stats.summary()}")
        except Exception as e:
            print(f"[{now()}] Could not resolve catch-up: {e}")

    # The command poller runs in its own daemon thread. ccxt exchange instances
    # are NOT thread-safe, so give the poller dedicated instances instead of
    # sharing the main loop's — concurrent fetch_ohlcv on shared objects
    # intermittently fails (worst on /bet's 4 fetches and /qqe). We instantiate
    # fresh instances of the same already-validated ccxt classes (no network
    # call) rather than falling back to the shared objects, which would
    # silently reintroduce the race.
    poller_exchanges = {name: (getattr(ccxt, ex.id)(), weight)
                         for name, (ex, weight) in exchanges.items()}
    CommandPoller(stats, poller_exchanges, symbol).start()
    # Start the session auto-detector + retraining scheduler
    _retrain_sched = RetrainScheduler(stats)
    _retrain_sched.start()
    print(f"[{now()}] RetrainScheduler started — session auto-detect every 60s, retrain every {RETRAIN_HOURS}h")
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 3600

    # settlement_price and close_dt captured at :00 before signal fires at :01
    settlement_price: float | None = None
    close_dt: datetime | None = None

    # Tracks the most recent successfully-fetched price across loop
    # iterations, so the no-active-bet fallback (see the except block below)
    # has SOMETHING to record a degraded signal against even if this cycle's
    # own price fetch never got far enough to succeed.
    last_known_price: float | None = None

    while True:
        # ── Phase 1: sleep until window close (:00/:15/:30/:45) ──────────────
        wait_close = seconds_to_window_close()
        # Absolute (monotonic) deadline for this window's close. Every staged
        # sleep targets this deadline, so the API/Telegram work done between
        # signals can't make the settlement capture drift past the boundary.
        close_target = time.monotonic() + wait_close
        print(f"[{now()}] Window closes in {wait_close}s — capturing settlement then")

        # Two staged signals fire before close:
        #   • EARLY READ at 9 min out (:06) — the BET Jeremy places (tracked)
        #   • 2-min check at 2 min out (:13) — confirm/heads-up only (not tracked)
        MID_OFFSET = 540   # seconds before close to fire the EARLY READ (the bet) — was 360 (6min), now 540 (9min)
        LATE_OFFSET = 120  # seconds before close to fire the confirmation check

        # Settlement capture fires this many seconds BEFORE the true window
        # boundary, not at it. BRTI ticks land ~1/sec, so pulling the 60-tick
        # average 1s early shifts the sampled window by about one tick —
        # immaterial to a 60-sample average — while guaranteeing the bot has
        # finished processing and is idle/ready in time for the next window's
        # Phase 1 sleep to start cleanly, instead of racing the boundary.
        SETTLEMENT_LEAD_SECONDS = 1

        # The EARLY READ at 9 min out is the BET Jeremy actually places, so it is
        # the TRACKED prediction. It now runs the SAME 5-indicator analyze() +
        # recommendation() used by the main :01 signal — previously this block
        # used a separate gap-only (price vs strike) calculation, which is why
        # the early bet could disagree with the main overview/soft-read. Using
        # one shared model end to end removes that mismatch.
        early_dir: str | None = None

        # ── WINDOW-OPEN instant signal — fires immediately, before any of the
        # staged sleeps below. Without this, "CURRENT OPEN BET" in the GUI
        # stays empty for the first ~1-6 minutes of every round (until the
        # :01 main signal or the 9-min EARLY READ). Necessarily a rougher
        # read than those — this window's own candles barely exist yet, so
        # it's mostly reading prior-window price action — but a same-window
        # override (set_window_open_bet, same safe pattern as the EARLY
        # READ/late-bet override) means it gets superseded by the :01 signal
        # and EARLY READ a few minutes later without ever creating a
        # duplicate history row. Wrapped in its own try/except so a failure
        # here can't delay or break the rest of this carefully-timed loop.
        try:
            _wo_ohlcv_5m = fetch_blended_ohlcv(exchanges, symbol, "5m", 40)
            _wo_closes = [c[4] for c in _wo_ohlcv_5m]
            _wo_volumes = [c[5] for c in _wo_ohlcv_5m]
            _wo_price = get_cf_price(_wo_closes[-1])
            last_known_price = _wo_price
            _wo_kalshi_yes, _wo_strike = fetch_kalshi_yes_price()
            _wo_window = kalshi_window_label()
            _wo_votes = analyze(_wo_closes, _wo_volumes, _wo_ohlcv_5m, None, None, None,
                                 _wo_kalshi_yes, None, _wo_strike)
            _wo_dir, _wo_action, _wo_conf, _wo_prob = recommendation(_wo_votes, _wo_price)
            stats.set_window_open_bet(_wo_dir, _wo_conf, _wo_prob, _wo_price, _wo_window, _wo_strike)
            print(f"[{now()}] Window-open instant read: {_wo_dir} ({_wo_conf}) for {_wo_window}")
        except Exception as e:
            print(f"[{now()}] Window-open instant read failed (non-fatal, EARLY READ/:01 signal will follow): {e}")

        # ── EARLY READ (mid-window) — the bet to place ────────────────────────
        if wait_close > MID_OFFSET + 30:
            time.sleep(max(0, close_target - MID_OFFSET - time.monotonic()))
            try:
                _m_ohlcv_5m  = fetch_blended_ohlcv(exchanges, symbol, "5m", 100)
                _m_ohlcv_15m = fetch_blended_ohlcv(exchanges, symbol, "15m", 30)
                _m_ohlcv_1h  = fetch_blended_ohlcv(exchanges, symbol, "1h", 30)
                _m_ohlcv_1m  = fetch_blended_ohlcv(exchanges, symbol, "1m", 20)
                _m_closes  = [c[4] for c in _m_ohlcv_5m]
                _m_volumes = [c[5] for c in _m_ohlcv_5m]
                _m_price = get_cf_price(_m_closes[-1])
                _m_kalshi_yes, _m_strike = fetch_kalshi_yes_price()
                _m_funding = fetch_funding_rate()
                _m_window = kalshi_window_label()
                _m_ticker = kalshi_ticker_label()

                _m_votes = analyze(_m_closes, _m_volumes, _m_ohlcv_5m, _m_ohlcv_15m,
                                   _m_ohlcv_1h, _m_ohlcv_1m, _m_kalshi_yes, _m_funding, _m_strike)
                _m_dir, _m_action, _m_conf, _m_pct = recommendation(_m_votes, _m_price)

                if _m_strike is not None:
                    _m_gap = _m_price - _m_strike
                    _m_abs = abs(_m_gap)
                else:
                    _m_gap, _m_abs = 0.0, 0.0

                _m_dir_txt = "UP ✅" if _m_dir == "UP" else "DOWN ❌"
                _m_icon = "⬆️" if _m_dir == "UP" else "⬇️"
                _m_bet = bet_recommendation(_m_conf)

                # This is the bet Jeremy places — record it as the tracked
                # prediction (overrides the noisy :01 lean for this window).
                early_dir = _m_dir
                stats.set_late_bet(_m_dir, _m_conf, float(_m_pct),
                                   _m_price, _m_window, _m_strike)
                _strike_line = (
                    f"📌 Strike:   <b>${_m_strike:,.2f}</b>  "
                    f"({_m_gap:+,.2f} — {_m_abs:,.0f} away)\n"
                    if _m_strike is not None else ""
                )
                _early_msg = (
                    f"🎯 <b>PLACE THIS BET — 9 min to close</b> 🎯\n"
                    f"<i>This is your call to act on now.</i>\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"{_m_icon} <b>{_m_dir_txt}</b>  [{_m_conf}  ~{_m_pct}%]\n"
                    f"{_m_bet}\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"💰 BTC (CF): <b>${_m_price:,.2f}</b>\n"
                    f"{_strike_line}"
                    f"🔖 <code>{_m_ticker}</code>\n"
                    f"📲 @ChillLobsterBot  |  🕐 {now()}"
                )
                notify(_early_msg)
                save_staged("early", _early_msg)
                print(f"[{now()}] EARLY READ (bet): {_m_dir_txt} | gap={_m_gap:+.2f} | {_m_conf} (tracked, full analyze())")
            except Exception as e:
                print(f"[{now()}] Early read error: {e}")

        # Confirmation check 2 minutes before close. Jeremy has usually already
        # placed his bet off the EARLY READ, so this does NOT change the tracked
        # prediction — it only tells him whether the call still holds or flipped.
        if close_target - time.monotonic() > LATE_OFFSET + 30:
            time.sleep(max(0, close_target - LATE_OFFSET - time.monotonic()))
            try:
                _snap_1m = fetch_blended_ohlcv(exchanges, symbol, "1m", 2)
                _late_price = get_cf_price(_snap_1m[-1][4])
                _late_kalshi_yes, _late_strike = fetch_kalshi_yes_price()
                if _late_strike is not None:
                    _gap = _late_price - _late_strike
                    _abs_gap = abs(_gap)
                    _direction = "UP" if _gap >= 0 else "DOWN"
                    _direction_txt = "UP ✅" if _gap >= 0 else "DOWN ❌"
                    _dir_icon = "⬆️" if _gap >= 0 else "⬇️"
                    if early_dir is None:
                        # No bet was placed this window (early read was too close).
                        _late_msg = (
                            f"👀 <b>2-min check — no bet this window</b>\n"
                            f"<i>The 6-min read was too close to call.</i>\n"
                            f"━━━━━━━━━━━━━━━━━\n"
                            f"{_dir_icon} Now leaning <b>{_direction_txt}</b>  "
                            f"(gap {_abs_gap:,.0f})\n"
                            f"📲 @ChillLobsterBot  |  🕐 {now()}"
                        )
                        notify(_late_msg)
                        save_staged("late", _late_msg)
                        print(f"[{now()}] 2-min check: no early bet | now {_direction_txt} gap={_gap:+.2f}")
                    elif _direction == early_dir:
                        _late_msg = (
                            f"✅ <b>CONFIRMED — your bet still looks right</b>\n"
                            f"━━━━━━━━━━━━━━━━━\n"
                            f"{_dir_icon} <b>{_direction_txt}</b> holding  "
                            f"(gap now {_abs_gap:,.0f})\n"
                            f"💰 BTC (CF): <b>${_late_price:,.2f}</b>  "
                            f"📌 Strike: <b>${_late_strike:,.2f}</b>\n"
                            f"📲 @ChillLobsterBot  |  🕐 {now()}"
                        )
                        notify(_late_msg)
                        save_staged("late", _late_msg)
                        print(f"[{now()}] 2-min check: CONFIRMED {_direction_txt} gap={_gap:+.2f}")
                    else:
                        _late_msg = (
                            f"⚠️ <b>HEADS UP — it flipped since your bet</b>\n"
                            f"<i>Now leaning the other way. Only act if you can still get in.</i>\n"
                            f"━━━━━━━━━━━━━━━━━\n"
                            f"You bet <b>{early_dir}</b> → now <b>{_direction_txt}</b>  "
                            f"(gap {_abs_gap:,.0f})\n"
                            f"💰 BTC (CF): <b>${_late_price:,.2f}</b>  "
                            f"📌 Strike: <b>${_late_strike:,.2f}</b>\n"
                            f"📲 @ChillLobsterBot  |  🕐 {now()}"
                        )
                        notify(_late_msg)
                        save_staged("late", _late_msg)
                        print(f"[{now()}] 2-min check: FLIPPED bet {early_dir} -> {_direction_txt} gap={_gap:+.2f}")
                else:
                    print(f"[{now()}] 2-min check: no Kalshi strike available")
            except Exception as e:
                print(f"[{now()}] 2-min check error: {e}")

        # Sleep to SETTLEMENT_LEAD_SECONDS before the close deadline (not all
        # the way to it) — absorbs any drift introduced by the signal
        # processing above, while leaving a 1s buffer before the true
        # boundary so the bot is guaranteed ready for the next window.
        time.sleep(max(0, close_target - SETTLEMENT_LEAD_SECONDS - time.monotonic()))

        # Record the exact ET close time — used to look up Kalshi's official result.
        # Still labeled as the true boundary (second=0) for settlement lookups,
        # since Kalshi's own ticker/result keys are minute-aligned regardless
        # of our 1s-early capture.
        close_dt = datetime.now(ET).replace(second=0, microsecond=0)

        # Capture CF Benchmarks BRTI ~1s BEFORE the window boundary (see
        # SETTLEMENT_LEAD_SECONDS above). Kalshi doesn't settle on a single
        # snapshot — it collects the last 60 one-second BRTI ticks (the final
        # minute before close) and averages them. get_cf_price_avg() replicates
        # that, so this tracks Kalshi's actual settlement value far more
        # closely than a single point-in-time read — the 1s-early pull doesn't
        # change which 60-tick window is meaningfully sampled, it just buys
        # the loop breathing room to be ready for Phase 1 of the next window.
        try:
            _snap = fetch_blended_ohlcv(exchanges, symbol, "1m", 2)
            settlement_price = get_cf_price_avg(_snap[-1][4], lead_seconds=SETTLEMENT_LEAD_SECONDS)
            print(f"[{now()}] Settlement captured (1-min avg, {SETTLEMENT_LEAD_SECONDS}s early): ${settlement_price:,.2f}")
        except Exception as e:
            print(f"[{now()}] Settlement capture failed: {e} — will use 15m close fallback")
            settlement_price = None

        # Burn the remaining lead-second(s) so Phase 2's "sleep 60s then fire
        # at :01/:16/:31/:46" still lands on the real :01/:16/:31/:46 marks.
        time.sleep(max(0, close_target - time.monotonic()))

        # ── Phase 2: sleep 60s then fire at :01/:16/:31/:46 ──────────────────
        time.sleep(60)

        try:
            # 100 × 5m + 30 × 15m + 30 × 1h + 20 × 1m for full multi-timeframe analysis
            ohlcv_5m  = fetch_blended_ohlcv(exchanges, symbol, "5m", 100)
            ohlcv_15m = fetch_blended_ohlcv(exchanges, symbol, "15m", 30)
            ohlcv_1h  = fetch_blended_ohlcv(exchanges, symbol, "1h", 30)
            ohlcv_1m  = fetch_blended_ohlcv(exchanges, symbol, "1m", 20)
            closes  = [c[4] for c in ohlcv_5m]
            volumes = [c[5] for c in ohlcv_5m]
            # CF price for the new signal's entry (current live price)
            cf_price = get_cf_price(closes[-1])
            last_known_price = cf_price  # update the cross-iteration fallback price

            # ── Resolve previous window result ────────────────────────────────
            # Try Kalshi's official API result first (most accurate).
            # Fall back to CF price at :00, then to the 15m candle close.
            kalshi_result = fetch_kalshi_settlement(close_dt) if close_dt else None
            if settlement_price is not None:
                resolve_price = settlement_price
            elif ohlcv_15m and len(ohlcv_15m) >= 2:
                resolve_price = ohlcv_15m[-2][4]
                print(f"[{now()}] Using 15m close as settlement fallback: ${resolve_price:,.2f}")
            else:
                resolve_price = cf_price
            result_msg = stats.resolve(resolve_price, kalshi_result)
            stats.resolve_qqe(resolve_price, kalshi_result)
            stats.resolve_predict(resolve_price, kalshi_result)
            stats.resolve_bridge(resolve_price, kalshi_result)
            stats.resolve_physics(resolve_price, kalshi_result)
            if result_msg:
                notify(result_msg)
                print(f"[{now()}] Result sent | {stats.summary()}")

            # ── Fire new signal for current window ───────────────────────────
            kalshi_yes, kalshi_strike = fetch_kalshi_yes_price()
            funding_rate = fetch_funding_rate()
            votes = analyze(closes, volumes, ohlcv_5m, ohlcv_15m, ohlcv_1h, ohlcv_1m, kalshi_yes, funding_rate, kalshi_strike, weight_overrides=SESSION_STATE.get_weights())
            s = score(votes)
            direction, action, confidence, prob = recommendation(votes, cf_price)

            # ATR flat-market filter: raised to 0.08% (was 0.06%).
            # Below 0.08% ATR the market is dead-flat — no edge for any direction.
            atr_val = calc_atr(ohlcv_5m, 14)
            atr_pct = atr_val / cf_price * 100 if cf_price > 0 else 0
            # ATR threshold scaled by session vol multiplier: quiet session → lower threshold
            _atr_threshold = 0.08 / get_effective_vol_multiplier()
            if atr_pct < _atr_threshold and confidence != "LEAN":
                print(f"[{now()}] ATR filter: market too flat ({atr_pct:.3f}%) — downgrading to LEAN")
                confidence, prob = "LEAN", 53.0
                action = lean_action(direction, "⚠️ flat market")

            # Kalshi crowd gate removed — the crowd's YES price doesn't determine
            # the 15-min winner (BTC vs strike does), so it was only adding noise
            # and over-suppressing valid technical/QQE signals. We now rely on the
            # strike-based Target Gap, QQE, and the technical confluence instead.

            # Circuit breaker: 3+ loss streak → stay directional but downgrade
            # confident calls to a low-confidence LEAN (cooldown, no SKIP).
            if confidence in ("MEDIUM", "HIGH", "STRONG") and stats.streak <= -3:
                print(f"[{now()}] Circuit breaker: {stats.streak} streak — downgrading to LEAN")
                confidence, prob = "LEAN", 54.0
                action = lean_action(direction, f"⚠️ cooldown ({abs(stats.streak)}-loss streak)")

            # Reversion Detector GATE: this is the actual decision-affecting use
            # of detect_price_reversion/detect_signal_reversal, not just display
            # text. Trend-following indicators (most of analyze()'s votes) are
            # most likely to be WRONG exactly when price is already stretched
            # (about to snap back) or when the bot's own calls have been
            # flip-flopping (read instability) — both are leading indicators
            # that a reversal may be imminent, which is precisely the failure
            # mode of confidently following a trend that's about to end.
            # Must run BEFORE record_signal() below, since detect_signal_reversal
            # compares against stats.signals history and record_signal() is
            # about to append this call to it.
            price_rev = detect_price_reversion(closes)
            signal_rev = detect_signal_reversal(stats, direction)
            if (price_rev["flagged"] or signal_rev["flagged"]) and confidence != "LEAN":
                reason = price_rev["note"] if price_rev["flagged"] else signal_rev["note"]
                print(f"[{now()}] Reversion gate: {reason} — downgrading to LEAN")
                confidence, prob = "LEAN", 52.0
                action = lean_action(direction, "⚠️ reversion risk")

            # Volatility Spike GATE — purely relative to the bot's own recent
            # ATR history (see detect_volatility_spike for full rationale).
            # When current volatility has spiked well above its recent
            # baseline, trend-following votes are most likely to be chasing
            # a move that's already reversing — same underlying risk as the
            # Reversion gate above, different detection method (volatility
            # regime change vs. price/signal-level stretch).
            vol_spike = detect_volatility_spike(ohlcv_5m)
            if vol_spike["flagged"] and confidence != "LEAN":
                print(f"[{now()}] Volatility gate: {vol_spike['note']} — downgrading to LEAN")
                confidence, prob = "LEAN", 52.0
                action = lean_action(direction, "⚠️ volatility spike")

            stats.set_why(votes)
            window = kalshi_window_label()

            stats.record_signal(direction, cf_price, window, confidence, prob, kalshi_strike)

            # Track QQE on its own (independent of the bot's bet/SKIP) — record
            # whichever way QQE leans this window; resolved silently at close.
            _qqe_vote = next((v for v in votes if v.label == "QQE"), None)
            if _qqe_vote is not None and _qqe_vote.direction != 0:
                stats.record_qqe("UP" if _qqe_vote.direction == 1 else "DOWN",
                                 cf_price, kalshi_strike)

            # Track Bridge and Physics models independently, exactly like QQE
            # and Predict — record their directional calls each window so they
            # can be scored against settlement at close. Both use the same
            # candle data already fetched this cycle, so no extra API calls.
            try:
                _window_end = datetime.now(ET).replace(second=0, microsecond=0)
                _window_end += timedelta(minutes=15 - (_window_end.minute % 15))
                _window_end_iso = _window_end.isoformat()
                _bridge = compute_bridge(cf_price, kalshi_strike or cf_price,
                                          kalshi_yes, ohlcv_5m, seconds_to_window_close())
                if _bridge["signal"] != "NEUTRAL":
                    stats.record_bridge(_bridge["signal"], kalshi_strike, _window_end_iso)
            except Exception as _e:
                print(f"[{now()}] Bridge record failed (non-fatal): {_e}")
            try:
                _physics = compute_physics(cf_price, kalshi_strike or cf_price,
                                            ohlcv_5m, seconds_to_window_close())
                if _physics["signal"] != "NEUTRAL":
                    stats.record_physics(_physics["signal"], kalshi_strike, _window_end_iso)
            except Exception as _e:
                print(f"[{now()}] Physics record failed (non-fatal): {_e}")

            streak = stats.streak_label()
            bet = bet_recommendation(confidence)
            kalshi_line = (f"🏦 Kalshi: <b>YES @ {kalshi_yes:.0%}</b>\n"
                           if kalshi_yes is not None else "")
            if kalshi_strike is not None:
                gap = cf_price - kalshi_strike
                gap_tag = f"+${gap:,.2f} ABOVE ✅" if gap >= 0 else f"-${abs(gap):,.2f} BELOW ❌"
                target_line = f"📌 Target: <b>${kalshi_strike:,.2f}</b>  →  {gap_tag}\n"
            else:
                target_line = ""
            ticker = kalshi_ticker_label()
            print(f"[{now()}] Signal: {direction} | Score: {s} | Kalshi YES: {kalshi_yes} | Prob: {prob}% | {stats.summary()}")

            # ── QQE block — separate section, explicitly states agree/disagree
            # with the overall call (see _handle_signal for the rationale).
            _qqe_v = next((v for v in votes if v.label == "QQE"), None)
            if _qqe_v is not None and _qqe_v.direction != 0:
                q_lean = "UP" if _qqe_v.direction == 1 else "DOWN"
                q_icon = "🟢" if _qqe_v.direction == 1 else "🔴"
                q_side = "BULLISH" if _qqe_v.direction == 1 else "BEARISH"
                agree = (q_lean == direction)
                agree_tag = "✅ AGREES with overall call" if agree else "⚠️ DISAGREES with overall call"
                qqe_block = (
                    f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ QQE (separate read) ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    f"📈 QQE core momentum: {q_icon} <b>{q_side}</b> → leans <b>{q_lean}</b>  [{agree_tag}]\n"
                    f"<i>(QQE is one weighted x2 input INTO the overall call above — not the call itself)</i>\n"
                    f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                )
            else:
                qqe_block = ""

            rev_lines = []
            if price_rev["flagged"]:
                rev_lines.append(f"🔁 Price reversion: {price_rev['note']}")
            if signal_rev["flagged"]:
                rev_lines.append(f"🔁 Signal reversal: {signal_rev['note']}")
            reversion_block = (
                ("⚠️ <b>Reversion Detector</b>\n" + "\n".join(rev_lines) + "\n"
                 "━━━━━━━━━━━━━━━━━\n") if rev_lines else ""
            )

            if direction == "SKIP":
                # Send a soft "best guess" read so Jeremy still knows which way
                # the indicators lean, even when the edge isn't strong enough to bet.
                n_up   = sum(1 for v in votes if v.direction > 0)
                n_down = sum(1 for v in votes if v.direction < 0)
                n_neut = sum(1 for v in votes if v.direction == 0)
                if s > 0 or (s == 0 and n_up >= n_down):
                    lean_icon, lean_txt = "⬆️", "UP"
                else:
                    lean_icon, lean_txt = "⬇️", "DOWN"
                # Strip HTML tags from action to get plain skip reason
                import re as _re
                skip_reason = _re.sub(r"<[^>]+>", "", action).replace("⚪ SKIP — ", "").strip()
                soft_msg = (
                    f"🔮 <b>Soft Read — {window}</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"Guess: {lean_icon} <b>{lean_txt}</b>  "
                    f"(score {s:+d} | ▲{n_up} ▼{n_down} ─{n_neut})\n"
                    f"⚠️ <i>No bet — {skip_reason}</i>\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"💰 BTC (CF): <b>${cf_price:,.2f}</b>\n"
                    f"{target_line}"
                    f"{kalshi_line}"
                    f"🔖 <code>{ticker}</code>"
                )
                notify(soft_msg)
                print(f"[{now()}] Soft read sent: {lean_txt} | {skip_reason}")
            else:
                msg = (
                    f"⚡ <b>ChillLobsterBTC — {window}</b>\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"{action}\n"
                    f"🎯 Confidence: <b>{confidence}</b>  |  Prob: <b>{prob}%</b>\n"
                    f"{bet}\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"💰 BTC (CF): <b>${cf_price:,.2f}</b>\n"
                    f"{target_line}"
                    f"{kalshi_line}"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"{qqe_block}"
                    f"{reversion_block}"
                    + (f"{streak}\n" if streak else "")
                    + f"📊 Session: {stats.summary()}\n"
                    f"🔖 <code>{ticker}</code>\n"
                    f"📲 @ChillLobsterBot  |  🕐 {now()}"
                )
                notify(msg)

        except ccxt.NetworkError as e:
            print(f"[{now()}] Network error: {e}")
            _record_fallback_signal_if_needed(stats, last_known_price, e)
        except ccxt.ExchangeError as e:
            print(f"[{now()}] Exchange error: {e}")
            _record_fallback_signal_if_needed(stats, last_known_price, e)
        except Exception as e:
            print(f"[{now()}] Unexpected error: {e}")
            _record_fallback_signal_if_needed(stats, last_known_price, e)

        # Hourly heartbeat
        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
            notify(
                f"💓 <b>@ChillLobsterBot — still running</b>\n"
                f"📊 Session: {stats.summary()}\n"
                f"🕐 {now()}"
            )
            last_heartbeat = time.time()


if __name__ == "__main__":
    _asset = sys.argv[1].upper() if len(sys.argv) > 1 else DEFAULT_ASSET
    main(_asset)
