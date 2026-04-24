"""Ask the vibe-trading agent whether to HOLD, CLOSE, or TIGHTEN SL on an
open position. Called each cycle while a position is active (gated by
AGENT_HOLD_CHECK env var to control API cost).

Agent receives:
  - Current position (side, entry, SL, TP1, TP2, PnL)
  - Multi-TF snapshot (tf_scores, confluence)
  - Funding + OI context
  - Time held

Agent returns JSON with one of: HOLD / CLOSE / TIGHTEN_SL (plus optional
new_sl) and a short reasoning.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_BTC_DIR = _HERE.parent
_REPO_ROOT = _BTC_DIR.parent
_AGENT_DIR = _REPO_ROOT / "agent"
_AGENT_CLI = _AGENT_DIR / "cli.py"

AGENT_TIMEOUT = 180

_HOLD_PROMPT_TEMPLATE = """\
BTC-USDT-SWAP open position review. Decide whether to HOLD, CLOSE, or TIGHTEN_SL.

Position:
  side={side} size={size} contracts
  entry={entry:,.0f} current={current:,.0f} pnl={pnl:+.2f} USDT ({pnl_pct:+.2f}% of balance)
  SL={sl:,.0f}  TP1={tp1:,.0f}  TP2={tp2:,.0f}
  held={hold_hours:.1f}h

Market snapshot (pre-fetched — do not re-fetch):
{snapshot}

Rules:
  - HOLD if setup remains valid and higher TFs still support the position.
  - CLOSE if structure broke, BOS/ChoCH against us, funding flipped adversely,
    or confluence collapsed.
  - TIGHTEN_SL if the trade moved favourably but momentum is waning — give
    new_sl (integer, within current SL ↔ entry range).

Use load_skill("smc") for structural breaks, load_skill("perp-funding-basis")
for funding, load_skill("liquidation-heatmap") for liq sweep context.

Output ONLY this JSON block, nothing else:
```json
{{"decision":"HOLD","new_sl":0,"reasoning":"..."}}
```
decision=HOLD|CLOSE|TIGHTEN_SL. new_sl=0 unless TIGHTEN_SL.\
"""


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
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


def _inject_env_to_subprocess() -> dict[str, str]:
    import os
    env = dict(os.environ)
    root_env = _parse_env_file(_REPO_ROOT / ".env")
    for k, v in root_env.items():
        env.setdefault(k, v)
    return env


def build_hold_prompt(
    position: dict[str, Any],
    current_price: float,
    pnl: float,
    pnl_pct: float,
    hold_hours: float,
    snapshot: str,
) -> str:
    return _HOLD_PROMPT_TEMPLATE.format(
        side=position.get("side", "?"),
        size=position.get("size_contracts", 0),
        entry=position.get("entry_price", 0) or 0,
        current=current_price,
        pnl=pnl,
        pnl_pct=pnl_pct,
        sl=position.get("sl_price", 0) or 0,
        tp1=position.get("tp1_price", 0) or 0,
        tp2=position.get("tp2_price", 0) or 0,
        hold_hours=hold_hours,
        snapshot=snapshot,
    )


def ask_agent(prompt: str) -> dict[str, Any] | None:
    """Invoke agent CLI. Returns parsed JSON response or None on failure."""
    if not _AGENT_CLI.exists():
        logger.warning("agent/cli.py missing — skipping hold check")
        return None

    runs_dir = _AGENT_DIR / "runs"
    runs_dir.mkdir(exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    prompt_file = runs_dir / f"_btc_hold_{ts}.txt"

    try:
        prompt_file.write_text(prompt, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_AGENT_CLI), "-f", str(prompt_file), "--json", "--no-rich"],
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT,
            cwd=str(_AGENT_DIR),
            env=_inject_env_to_subprocess(),
        )
        agent_meta: dict[str, Any] = {}
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    agent_meta = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass
        run_dir = Path(agent_meta["run_dir"]) if agent_meta.get("run_dir") else None
        content = _read_agent_answer(run_dir) if run_dir else ""
        return _parse_decision_json(content)
    except subprocess.TimeoutExpired:
        logger.warning("Agent hold check timed out after %ds", AGENT_TIMEOUT)
        return None
    except Exception as exc:
        logger.warning("Agent hold check failed: %s", exc)
        return None
    finally:
        try:
            prompt_file.unlink(missing_ok=True)
        except Exception:
            pass


def _read_agent_answer(run_dir: Path) -> str:
    trace_file = run_dir / "trace.jsonl"
    if not trace_file.exists():
        return ""
    last = ""
    try:
        for line in trace_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "answer" and entry.get("content"):
                    last = entry["content"]
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return last


_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _parse_decision_json(text: str) -> dict[str, Any] | None:
    m = _JSON_RE.search(text)
    if not m:
        return None
    raw = re.sub(r",\s*}", "}", m.group(1))
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    decision = str(d.get("decision", "")).upper()
    if decision not in ("HOLD", "CLOSE", "TIGHTEN_SL"):
        return None
    return {
        "decision": decision,
        "new_sl": float(d.get("new_sl", 0) or 0),
        "reasoning": str(d.get("reasoning", ""))[:400],
    }
