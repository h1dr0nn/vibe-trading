"""Bridge between btc trade CLI and vibe-trading agent.

Flow:
  1. Check agent is configured (LANGCHAIN_PROVIDER + LANGCHAIN_MODEL_NAME + GEMINI_API_KEY)
  2. Build prompt with pre-fetched market snapshot embedded
  3. Call agent/cli.py as subprocess with -f <prompt_file> --json --no-rich
  4. Read trace.jsonl from run_dir → extract last answer
  5. Parse JSON block from agent answer → TradeSignal
  6. Validate prices (sanity check vs current price)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HERE       = Path(__file__).resolve().parent
_BTC_DIR    = _HERE.parent
_REPO_ROOT  = _BTC_DIR.parent
_AGENT_DIR  = _REPO_ROOT / "agent"
_AGENT_CLI  = _AGENT_DIR / "cli.py"

AGENT_TIMEOUT = 240  # seconds — Gemini can be slow on cold start

_JSON_BLOCK_RE  = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_DIRECTION_RE   = re.compile(r'"direction"\s*:\s*"(LONG|SHORT|NO TRADE)"', re.IGNORECASE)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    direction: int            # 1=LONG, -1=SHORT, 0=NO TRADE
    direction_label: str      # "LONG" | "SHORT" | "NO TRADE"
    entry: float
    sl: float
    tp1: float
    tp2: float
    confidence: str           # "HIGH" | "MEDIUM" | "LOW"
    agent_reasoning: str      # summary sentence from agent
    source: str               # "agent" | "local_fallback"


# ── Agent config check ────────────────────────────────────────────────────────

def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict without touching os.environ."""
    result = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return result


def _load_env_vars() -> dict[str, str]:
    """Merge .env candidates + os.environ. os.environ wins."""
    candidates = [
        _REPO_ROOT / ".env",
        _AGENT_DIR / ".env",
        Path.home() / ".vibe-trading" / ".env",
    ]
    merged: dict[str, str] = {}
    for c in reversed(candidates):  # lower priority first
        if c.exists():
            merged.update(_parse_env_file(c))
    merged.update(os.environ)  # os.environ wins
    return merged


def check_agent_configured() -> tuple[bool, str]:
    """Return (ready, human_readable_reason)."""
    env = _load_env_vars()

    if not _AGENT_CLI.exists():
        return False, f"agent/cli.py not found at {_AGENT_CLI}"

    provider = env.get("LANGCHAIN_PROVIDER", "").lower()
    if not provider:
        return False, "LANGCHAIN_PROVIDER not set in .env (e.g. LANGCHAIN_PROVIDER=gemini)"

    model = env.get("LANGCHAIN_MODEL_NAME", "")
    if not model:
        gemini_model = env.get("GEMINI_MODEL", "")
        if gemini_model:
            return False, (
                f"LANGCHAIN_MODEL_NAME not set. Found GEMINI_MODEL={gemini_model!r} "
                "but agent uses LANGCHAIN_MODEL_NAME. "
                "Add: LANGCHAIN_MODEL_NAME=gemini-2.5-flash-preview-04-17"
            )
        return False, "LANGCHAIN_MODEL_NAME not set in .env"

    if provider == "gemini":
        if not env.get("GEMINI_API_KEY"):
            return False, "GEMINI_API_KEY not set in .env"
        if not env.get("GEMINI_BASE_URL"):
            return False, (
                "GEMINI_BASE_URL not set. "
                "Add: GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/"
            )

    return True, ""


# ── Prompt building ───────────────────────────────────────────────────────────

def build_market_snapshot(
    tf_scores: dict,
    confluence: dict,
    funding_data: dict,
    oi_data: dict,
    price: float,
    mark_price: float,
    change_24h: float,
) -> str:
    s15 = tf_scores.get("15m", {})
    s1h = tf_scores.get("1H", {})
    s4h = tf_scores.get("4H", {})
    s1d = tf_scores.get("1D", {})

    def sl(s: dict) -> str:
        v = s.get("signal", 0)
        return "L" if v == 1 else ("S" if v == -1 else "F")

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    r8h = funding_data.get("rate_8h", 0)
    ann = funding_data.get("ann_pct", 0)
    avg = funding_data.get("avg_7d", 0)
    reg = funding_data.get("regime", "?")
    oi  = oi_data.get("oi_usd_b", 0)
    a4h = tf_scores.get("4H", {}).get("atr", 0)
    a1h = tf_scores.get("1H", {}).get("atr", 0)
    a15 = tf_scores.get("15m", {}).get("atr", 0)

    return (
        f"BTC-USDT-SWAP {ts}\n"
        f"Price={price:,.0f} Mark={mark_price:,.0f} 24h={change_24h:+.1f}%\n"
        f"Fund8h={r8h:+.5f} Ann={ann:+.1f}% Avg7d={avg:+.5f} Regime={reg}\n"
        f"OI={oi:.2f}B\n"
        f"TF   sig str  RSI  ADX  EMA  BB%  score\n"
        f"15m* {sl(s15)}   {s15.get('strength',0):3d}%  {s15.get('rsi',0):.0f}  {s15.get('adx',0):.0f}  {s15.get('ema_cross','?')[:4]}  {s15.get('bb_pct',0.5):.2f}  {s15.get('score',0):+.2f}\n"
        f"1H   {sl(s1h)}   {s1h.get('strength',0):3d}%  {s1h.get('rsi',0):.0f}  {s1h.get('adx',0):.0f}  {s1h.get('ema_cross','?')[:4]}  {s1h.get('bb_pct',0.5):.2f}  {s1h.get('score',0):+.2f}\n"
        f"4H   {sl(s4h)}   {s4h.get('strength',0):3d}%  {s4h.get('rsi',0):.0f}  {s4h.get('adx',0):.0f}  {s4h.get('ema_cross','?')[:4]}  {s4h.get('bb_pct',0.5):.2f}  {s4h.get('score',0):+.2f}\n"
        f"1D   {sl(s1d)}   {s1d.get('strength',0):3d}%  {s1d.get('rsi',0):.0f}  {s1d.get('adx',0):.0f}  {s1d.get('ema_cross','?')[:4]}  {s1d.get('bb_pct',0.5):.2f}  {s1d.get('score',0):+.2f}\n"
        f"(*15m=entry timing only, weight=0.5)\n"
        f"Net={confluence.get('net_score',0):+.2f} Conf={confluence.get('confidence','?')} Agree={confluence.get('agreeing_tfs',0)}/3\n"
        f"ATR4H={a4h:,.0f} ATR1H={a1h:,.0f} ATR15m={a15:,.0f}"
    )


_PROMPT_TEMPLATE = """\
BTC-USDT-SWAP live trade signal. Market data (pre-fetched, do not re-fetch):
{snapshot}

Steps:
1. load_skill("perp-funding-basis") - classify funding regime, crowding risk
2. load_skill("liquidation-heatmap") - estimate liq clusters near {price:,.0f}
3. load_skill("onchain-analysis") - MVRV/SOPR at {price:,.0f}
4. load_skill("smc") - BOS/ChoCH/FVG/OB near {price:,.0f}
5. load_skill("technical-basic") - validate TF scores above
6. Synthesize: SMC 30% + Technical 25% + Funding 20% + Liq 15% + Onchain 10%. If 3+ conflict: NO TRADE.

Output ONLY this JSON (nothing after closing backtick):
```json
{{"direction":"LONG","entry":0,"tp1":0,"tp2":0,"sl":0,"confidence":"HIGH","reasoning_summary":"..."}}
```
direction=LONG|SHORT|NO TRADE. Prices=integers. confidence=HIGH(4+agree)/MEDIUM(3)/LOW(2). NO TRADE=all zeros.\
"""


def build_prompt(snapshot: str, price: float) -> str:
    return _PROMPT_TEMPLATE.format(snapshot=snapshot, price=price)


# ── Subprocess execution ──────────────────────────────────────────────────────

def _inject_env_to_subprocess() -> dict[str, str]:
    """Merge root .env into env dict for subprocess so agent picks up API keys."""
    env = dict(os.environ)
    root_env = _parse_env_file(_REPO_ROOT / ".env")
    for k, v in root_env.items():
        env.setdefault(k, v)  # don't override already-set vars
    return env


def run_agent_subprocess(prompt: str) -> dict:
    """Write prompt to temp file, call agent/cli.py, return result dict."""
    # Write prompt to a temp file inside agent/runs/ (agent has write access)
    runs_dir = _AGENT_DIR / "runs"
    runs_dir.mkdir(exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    prompt_file = runs_dir / f"_btc_signal_{ts}.txt"

    try:
        prompt_file.write_text(prompt, encoding="utf-8")

        sub_env = _inject_env_to_subprocess()

        result = subprocess.run(
            [sys.executable, str(_AGENT_CLI), "-f", str(prompt_file), "--json", "--no-rich"],
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT,
            cwd=str(_AGENT_DIR),
            env=sub_env,
        )

        # Parse the JSON line from stdout
        stdout = result.stdout.strip()
        agent_meta = {}
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    agent_meta = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

        agent_status = agent_meta.get("status", "unknown")
        run_dir = Path(agent_meta["run_dir"]) if agent_meta.get("run_dir") else None

        # Always try to read trace content — even on "failed" status the model
        # may have written a partial answer before failing (e.g. empty Gemini response)
        content = _read_agent_content(run_dir) if run_dir else ""

        if result.returncode != 0 or agent_status not in ("success", "failed"):
            stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
            return {
                "status": "failed",
                "error": agent_meta.get("reason") or stderr_tail or f"exit code {result.returncode}",
            }

        if agent_status == "failed" and not content:
            reason = agent_meta.get("reason") or "model returned empty response"
            return {"status": "failed", "error": reason}

        return {
            "status": "success",
            "run_id": agent_meta.get("run_id", ""),
            "run_dir": str(run_dir) if run_dir else "",
            "content": content,
        }

    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": f"Agent timed out after {AGENT_TIMEOUT}s"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        try:
            prompt_file.unlink(missing_ok=True)
        except Exception:
            pass


def _read_agent_content(run_dir: Path) -> str:
    """Extract the last 'answer' entry from trace.jsonl."""
    trace_file = run_dir / "trace.jsonl"
    if not trace_file.exists():
        # Fallback: read final_answer.md or any .md in run_dir
        for md in run_dir.glob("*.md"):
            try:
                return md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        return ""

    last_answer = ""
    try:
        for line in trace_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "answer" and entry.get("content"):
                    last_answer = entry["content"]
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return last_answer


# ── Output parsing ────────────────────────────────────────────────────────────

def _parse_json_block(text: str) -> dict | None:
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    # Tolerate trailing comma before closing brace
    raw = re.sub(r",\s*}", "}", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _parse_regex_fallback(text: str) -> dict | None:
    """Last-resort regex scan for direction + price fields."""
    d_match = _DIRECTION_RE.search(text)
    if not d_match:
        return None
    direction = d_match.group(1).upper()

    def find_price(label: str) -> float | None:
        m = re.search(rf'"{label}"\s*:\s*(\d[\d,]*(?:\.\d+)?)', text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    return {
        "direction": direction,
        "entry": find_price("entry") or 0,
        "sl":    find_price("sl")    or 0,
        "tp1":   find_price("tp1")   or 0,
        "tp2":   find_price("tp2")   or 0,
        "confidence": "LOW",
        "reasoning_summary": "(parsed via regex fallback)",
    }


def _validate(d: dict, current_price: float) -> bool:
    """Sanity: prices within 20% of current price, direction valid, SL not too tight."""
    if d.get("direction") not in ("LONG", "SHORT", "NO TRADE"):
        return False
    if d["direction"] == "NO TRADE":
        return True
    for key in ("entry", "sl", "tp1", "tp2"):
        val = d.get(key, 0)
        try:
            val = float(val)
        except (TypeError, ValueError):
            return False
        if val <= 0:
            return False
        if abs(val - current_price) / current_price > 0.25:
            return False
    # SL must be at least 0.3% away from entry (prevents near-zero SL from agent)
    entry = float(d.get("entry", current_price))
    sl = float(d.get("sl", 0))
    if abs(entry - sl) / entry < 0.003:
        return False
    return True


def parse_agent_output(content: str, current_price: float) -> TradeSignal | None:
    """Three-layer parse: JSON block → regex fallback → None (triggers local fallback)."""
    for d in [_parse_json_block(content), _parse_regex_fallback(content)]:
        if d and _validate(d, current_price):
            direction_map = {"LONG": 1, "SHORT": -1, "NO TRADE": 0}
            direction = direction_map.get(d["direction"].upper(), 0)
            return TradeSignal(
                direction=direction,
                direction_label=d["direction"],
                entry=float(d.get("entry", current_price)),
                sl=float(d.get("sl", 0)),
                tp1=float(d.get("tp1", 0)),
                tp2=float(d.get("tp2", 0)),
                confidence=d.get("confidence", "MEDIUM"),
                agent_reasoning=d.get("reasoning_summary", content[:400]),
                source="agent",
            )
    return None


def local_fallback_signal(
    direction: int,
    confluence: dict,
    levels: dict | None,
    funding_regime: str,
    price: float,
) -> TradeSignal:
    """Wrap local analysis result into TradeSignal."""
    labels = {1: "LONG", -1: "SHORT", 0: "NO TRADE"}
    return TradeSignal(
        direction=direction,
        direction_label=labels.get(direction, "NO TRADE"),
        entry=levels["entry"] if levels else price,
        sl=levels["sl"] if levels else 0.0,
        tp1=levels["tp1"] if levels else 0.0,
        tp2=levels["tp2"] if levels else 0.0,
        confidence=confluence.get("confidence", "LOW"),
        agent_reasoning=f"Local multi-TF analysis. Regime: {funding_regime}.",
        source="local_fallback",
    )
