"""Generate the Track 3 architecture diagrams via google/gemini-3.1-flash-image-preview.

One shared STYLE preamble keeps the design language identical across every
diagram (editorial print aesthetic matching the product UI: warm paper, ink
hairlines, Spectral-style serif titles, monospace technical labels, emerald
agents / coral data plane / Google-blue cloud accents).

Usage:
  OPENROUTER_API_KEY=sk-or-... python3 generate_diagrams.py [name ...]
  (no args = all diagrams; names: hero, team, a2a, grounding)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

MODEL = "google/gemini-3.1-flash-image-preview"
OUT = Path(__file__).resolve().parent

STYLE = """\
Editorial technical diagram in a refined print-magazine style, like a hand-set
figure from a beautifully typeset engineering journal.
Canvas: warm off-white paper background, hex #F6F5F1, completely flat.
Linework: thin near-black ink (#1A1A1A) strokes, hairline rules; arrows are
thin ink lines with small solid triangular heads.
Nodes: rounded-rectangle cards with 1.5px ink outlines and very subtle
paper-tone fill; generous padding and whitespace; grid-aligned layout.
Typography: an elegant high-contrast serif typeface for the title and node
names; small uppercase monospace typeface for technical labels. All text
horizontal, crisp, perfectly spelled, EXACTLY as quoted - render every quoted
string verbatim and render NOTHING that is not quoted: no font names, no
watermarks, no captions, no labels on arrows unless explicitly quoted.
Accent palette, used sparingly as thin left-border bars on cards and small
filled dots: emerald green #1A8F55 = Manthan agents; coral orange #FF6F61 =
the Coral data plane; Google blue #4285F4 = Google Cloud elements; ink
everything else. No gradients, no shadows, no 3D, no photos, no icons of
people, no skeuomorphism, no clutter. Flat, minimal, precise.
"""

DIAGRAMS: dict[str, dict] = {
    "hero": {
        "aspect_ratio": "16:9",
        "prompt": STYLE + """
Title at top-left in serif: "Manthan — three agents are the system"
Subtitle beneath in small monospace: "GOOGLE ADK · GEMINI · A2A · CLOUD RUN"

Layout, left to right in one clean horizontal flow:
1) A small ink card labeled "Stripe webhook" with monospace sublabel
   "5 DISPUTE EVENT TYPES".
2) Arrow right to an emerald-accented card "Triage agent" with sublabel
   "gemini-3.1-flash-lite".
3) Arrow right labeled "A2A" in monospace, to a larger emerald-accented card
   "Investigator agent" with sublabel "gemini-3.1-pro-preview" and inside it a
   row of five tiny rounded chips labeled "payments", "customer",
   "reliability", "policy", "network rules".
4) From the investigator, an arrow down labeled "writes events" to a
   Google-blue-accented card "Case store" with sublabel "CLOUD SQL POSTGRES".
5) From the case store, arrow right labeled "human approval" to an ink card
   "Deterministic actor" with sublabel "REFUNDS · EMAILS · NOTES".
Below the investigator, a coral-accented card "Coral - 9 SaaS systems as SQL"
connected up to the investigator by a line labeled "MCP".
At bottom-left, a small ink card "External agents" with an arrow pointing
RIGHT from it INTO a separate emerald-accented card "Advisor agent" (sublabel
"gemini-3.5-flash"); the arrowhead touches the Advisor card and the arrow is
labeled "A2A skills" in monospace. No other text anywhere on the canvas.
""",
    },
    "team": {
        "aspect_ratio": "3:2",
        "prompt": STYLE + """
Title at top-left in serif: "The investigator is a team"
Subtitle in small monospace: "COORDINATOR + FIVE PARALLEL SPECIALISTS · ONE EVIDENCE SET"

Center: a large emerald-accented card "Coordinator" with sublabel
"gemini-3.1-pro-preview · pacer-governed".
Fanning out from it with thin arrows, five evenly spaced emerald-accented
cards in an arc above and beside it:
"Payments analyst" with monospace sublabel "stripe.*",
"Customer context" with sublabel "CRM + SUPPORT",
"Reliability analyst" with sublabel "INCIDENTS + USAGE",
"Policy analyst" with sublabel "NOTION SOP RAG",
"Network rules" with sublabel "GOOGLE SEARCH" and a small Google-blue dot.
Below the coordinator: a wide thin ink tray labeled "Shared evidence set"
with sublabel "GLOBALLY INDEXED CITATIONS", connected to all five specialists
by faint hairlines.
Bottom: a coral-accented bar "Coral SQL over MCP" connected up to the tray.
A small ink stamp note at bottom-right in monospace: "SPECIALIST FAILURE
DEGRADES, NEVER ABORTS".
""",
    },
    "a2a": {
        "aspect_ratio": "3:2",
        "prompt": STYLE + """
Title at top-left in serif: "Any agent can work with Manthan"
Subtitle in small monospace: "A2A · AGENT CARD + JSON-RPC · 12 SKILLS"

Center: a large emerald-accented card "Manthan" with two small monospace
lines inside: "/.well-known/agent-card.json" and "POST /a2a".
Around it, four ink cards connected to the center by thin arrows, each arrow
carrying one monospace skill label:
Left top: card "CS agent", arrow toward center labeled "precheck_refund".
Left bottom: card "CFO agent", arrow toward center labeled "dispute_exposure".
Right top: card "Logistics agent", arrow toward center labeled
"contribute_evidence".
Right bottom: card "Any enterprise agent", arrow toward center labeled
"ask · investigate_dispute".
At the bottom, a thin hairline footnote row in small monospace:
"EVERY CASE ARTIFACT IS READABLE: case · brief · findings · actions · audit".
""",
    },
    "grounding": {
        "aspect_ratio": "3:2",
        "prompt": STYLE + """
Title at top-left in serif: "Three grounding surfaces"
Subtitle in small monospace: "PRIVATE DATA · MERCHANT POLICY RAG · LIVE WEB"

Three column cards side by side, equal width:
1) Coral-orange-accented card titled "Coral SQL" with sublabel "PRIVATE-DATA
   GROUNDING" and three small monospace lines: "9 SaaS systems", "one wide
   JOIN", "row-level citations".
2) Ink card titled "Notion SOP retrieval" with sublabel "RAG OVER MERCHANT
   POLICY" and two small lines: "authoritative refund policy", "formula
   quoted + cited".
3) Google-blue-accented card titled "Google Search" with sublabel
   "NETWORK RULES GROUNDING" and two small lines: "Visa CE 3.0 evidence
   rules", "fresh at decision time".
All three cards point with plain thin arrows down into a single wide
emerald-accented bar at the bottom titled "Cited decision brief" with
monospace sublabel "IF IT IS IN THE BRIEF, IT IS IN A SOURCE". The arrows
themselves carry no text at all.
""",
    },
}


def generate(name: str, spec: dict, key: str) -> Path:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": spec["prompt"]}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": spec["aspect_ratio"]},
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Miny-Labs/manthan",
            "X-Title": "Manthan Track3 diagrams",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    msg = data["choices"][0]["message"]
    images = msg.get("images") or []
    if not images:
        raise RuntimeError(f"{name}: no image in response: {json.dumps(data)[:400]}")
    url = images[0]["image_url"]["url"]
    b64 = url.split("base64,", 1)[1]
    out = OUT / f"{name}.png"
    out.write_bytes(base64.b64decode(b64))
    return out


if __name__ == "__main__":
    key = os.environ["OPENROUTER_API_KEY"]
    names = sys.argv[1:] or list(DIAGRAMS)
    for n in names:
        path = generate(n, DIAGRAMS[n], key)
        print(f"{n}: {path} ({path.stat().st_size // 1024} KB)")
