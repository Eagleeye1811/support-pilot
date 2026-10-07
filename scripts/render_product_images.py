"""Draw the product catalog as consistent flat illustrations (SVG) and render them to PNG.

Run once after changing a drawing:  python scripts/render_product_images.py
PNG rendering uses headless Google Chrome; the PNGs are committed so the deployed
app (Telegram photos, email attachments, website) needs no browser.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "products")
SIZE = 600

BG = {  # category → (light, dark) background gradient
    "Electronics": ("#eef4fc", "#d3e3f7"), "Fashion": ("#fdf1ea", "#f7dccd"), "Home": ("#ecf7f1", "#cdeadb"),
    "Books": ("#fcf6e6", "#f2e3bb"), "Beauty": ("#fcedf4", "#f3d3e2"), "Grocery": ("#f1f8e6", "#dcecc2"),
}

DRAW = {
    "EL-200": """
  <path d="M170 335 C170 160 430 160 430 335" fill="none" stroke="#24262c" stroke-width="28" stroke-linecap="round"/>
  <path d="M190 300 C200 190 400 190 410 300" fill="none" stroke="#454952" stroke-width="5" stroke-linecap="round"/>
  <rect x="158" y="282" width="20" height="44" rx="6" fill="#a7adb6"/><rect x="422" y="282" width="20" height="44" rx="6" fill="#a7adb6"/>
  <rect x="122" y="305" width="96" height="160" rx="44" fill="#1d1f24"/><rect x="196" y="322" width="36" height="126" rx="18" fill="#3a3d45"/>
  <rect x="382" y="305" width="96" height="160" rx="44" fill="#1d1f24"/><rect x="368" y="322" width="36" height="126" rx="18" fill="#3a3d45"/>
  <circle cx="160" cy="385" r="14" fill="none" stroke="#5b5f68" stroke-width="3"/><circle cx="440" cy="385" r="14" fill="none" stroke="#5b5f68" stroke-width="3"/>""",
    "EL-110": """
  <rect x="185" y="318" width="230" height="160" rx="72" fill="#f7f8fb" stroke="#d3d9e3" stroke-width="4"/>
  <path d="M190 372 H410" stroke="#d3d9e3" stroke-width="4"/><circle cx="300" cy="420" r="6" fill="#1baf7a"/>
  <g transform="rotate(-18 235 230)"><rect x="222" y="232" width="26" height="92" rx="13" fill="#3d7ddb"/>
    <circle cx="235" cy="228" r="44" fill="#3d7ddb"/><ellipse cx="214" cy="215" rx="16" ry="20" fill="#2a5ea9"/>
    <circle cx="248" cy="214" r="10" fill="#79a8ee"/></g>
  <g transform="rotate(18 365 230)"><rect x="352" y="232" width="26" height="92" rx="13" fill="#3d7ddb"/>
    <circle cx="365" cy="228" r="44" fill="#3d7ddb"/><ellipse cx="386" cy="215" rx="16" ry="20" fill="#2a5ea9"/>
    <circle cx="352" cy="214" r="10" fill="#79a8ee"/></g>""",
    "EL-310": """
  <rect x="252" y="96" width="96" height="140" rx="22" fill="#343b48"/><rect x="252" y="366" width="96" height="140" rx="22" fill="#343b48"/>
  <g fill="#262c37"><rect x="292" y="420" width="16" height="8" rx="4"/><rect x="292" y="444" width="16" height="8" rx="4"/><rect x="292" y="468" width="16" height="8" rx="4"/></g>
  <rect x="204" y="196" width="192" height="210" rx="54" fill="#1c2028"/><rect x="222" y="214" width="156" height="174" rx="40" fill="#0c1726"/>
  <rect x="394" y="270" width="16" height="44" rx="7" fill="#a7adb6"/>
  <path d="M260 262 A52 52 0 1 1 260 340" fill="none" stroke="#1baf7a" stroke-width="8" stroke-linecap="round"/>
  <path d="M275 275 A36 36 0 1 1 275 327" fill="none" stroke="#eda100" stroke-width="8" stroke-linecap="round"/>
  <text x="300" y="312" text-anchor="middle" font-family="Helvetica, Arial" font-weight="700" font-size="30" fill="#ffffff">10:09</text>""",
    "EL-900": """
  <defs><linearGradient id="scr" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f8fe6"/><stop offset="1" stop-color="#7c5cf0"/></linearGradient></defs>
  <rect x="135" y="140" width="330" height="232" rx="16" fill="#2a2e36"/><rect x="151" y="156" width="298" height="200" rx="6" fill="url(#scr)"/>
  <path d="M151 300 C230 250 300 330 449 250 V356 H151 Z" fill="#ffffff" opacity=".18"/>
  <circle cx="300" cy="148" r="3" fill="#6b7280"/>
  <path d="M95 380 H505 L545 422 Q548 434 534 434 H66 Q52 434 55 422 Z" fill="#cfd4dc"/>
  <rect x="150" y="388" width="300" height="12" rx="4" fill="#b4bac4"/><rect x="262" y="410" width="76" height="10" rx="4" fill="#b4bac4"/>
  <path d="M55 422 H545" stroke="#aeb4be" stroke-width="3"/>""",
    "EL-420": """
  <defs><pattern id="dots" width="14" height="14" patternUnits="userSpaceOnUse"><circle cx="7" cy="7" r="3.2" fill="#b9471a"/></pattern></defs>
  <path d="M262 150 Q300 104 338 150" fill="none" stroke="#3a3d45" stroke-width="12" stroke-linecap="round"/>
  <rect x="215" y="150" width="170" height="320" rx="74" fill="#eb6834"/>
  <rect x="230" y="196" width="140" height="228" rx="62" fill="url(#dots)"/>
  <ellipse cx="300" cy="170" rx="66" ry="16" fill="#f39a6f"/>
  <g fill="#ffffff" opacity=".9"><circle cx="270" cy="168" r="5"/><circle cx="300" cy="166" r="5"/><circle cx="330" cy="168" r="5"/></g>
  <rect x="226" y="200" width="18" height="200" rx="9" fill="#ffffff" opacity=".18"/>""",
    "FA-101": """
  <path d="M100 418 Q92 458 140 460 H478 Q520 456 510 418 Z" fill="#ffffff" stroke="#d4d8de" stroke-width="4"/>
  <path d="M104 440 H508" stroke="#e7e9ed" stroke-width="10"/>
  <path d="M124 420 Q132 330 214 300 L292 276 Q328 330 384 340 Q478 352 506 412 L508 420 Z" fill="#2a78d6"/>
  <path d="M292 276 Q328 330 384 340" fill="none" stroke="#1f5fae" stroke-width="10"/>
  <path d="M160 392 C240 360 330 410 470 380" fill="none" stroke="#ffffff" stroke-width="12" stroke-linecap="round"/>
  <g stroke="#ffffff" stroke-width="7" stroke-linecap="round"><path d="M250 300 L282 330"/><path d="M270 292 L302 322"/><path d="M290 286 L318 314"/></g>
  <path d="M124 420 Q118 360 150 330 L170 346 Q148 380 152 420 Z" fill="#1f5fae"/>""",
    "FA-205": """
  <path d="M220 130 L300 160 L380 130 L470 190 L500 420 L440 430 L420 300 L420 480 H180 L180 300 L160 430 L100 420 L130 190 Z" fill="#3f6ca9"/>
  <path d="M220 130 L300 160 L380 130 L360 190 L300 230 L240 190 Z" fill="#2e5385"/>
  <path d="M300 230 V480" stroke="#2e5385" stroke-width="6"/>
  <g fill="#d4a72c"><circle cx="314" cy="270" r="7"/><circle cx="314" cy="330" r="7"/><circle cx="314" cy="390" r="7"/><circle cx="314" cy="450" r="7"/></g>
  <g fill="none" stroke="#e9c46a" stroke-width="3" stroke-dasharray="7 6">
    <rect x="212" y="270" width="64" height="58" rx="6"/><rect x="330" y="270" width="64" height="58" rx="6"/>
    <path d="M184 470 H416"/><path d="M135 200 L160 420"/><path d="M465 200 L440 420"/></g>""",
    "FA-330": """
  <path d="M300 92 Q300 74 316 74 Q332 74 332 90 Q332 104 314 112" fill="none" stroke="#8a8f98" stroke-width="7" stroke-linecap="round"/>
  <path d="M300 112 L180 160 H420 Z" fill="none" stroke="#8a8f98" stroke-width="7" stroke-linejoin="round"/>
  <path d="M232 158 L300 182 L368 158 L432 200 L410 300 L392 290 L392 500 H208 L208 290 L190 300 L168 200 Z" fill="#b5446e"/>
  <path d="M276 166 L300 230 L324 166" fill="none" stroke="#e9c46a" stroke-width="6"/>
  <g fill="#e9c46a"><circle cx="300" cy="250" r="5"/><circle cx="300" cy="276" r="5"/><circle cx="300" cy="302" r="5"/></g>
  <path d="M208 470 H392" stroke="#e9c46a" stroke-width="6"/>
  <g fill="none" stroke="#e9c46a" stroke-width="3"><path d="M230 440 q10 -16 20 0 q10 16 20 0 q10 -16 20 0 q10 16 20 0 q10 -16 20 0 q10 16 20 0 q10 -16 20 0"/></g>""",
    "HM-010": """
  <rect x="320" y="250" width="180" height="150" rx="24" fill="#c94a3a"/><rect x="306" y="236" width="208" height="26" rx="13" fill="#a93b2d"/>
  <rect x="396" y="214" width="28" height="22" rx="8" fill="#2b2d33"/>
  <rect x="290" y="300" width="34" height="16" rx="8" fill="#2b2d33"/><rect x="496" y="300" width="34" height="16" rx="8" fill="#2b2d33"/>
  <circle cx="232" cy="338" r="128" fill="#25272d"/><circle cx="232" cy="338" r="104" fill="#3b3e46"/>
  <circle cx="210" cy="316" r="34" fill="#4a4e57"/>
  <g transform="rotate(38 232 338)"><rect x="350" y="326" width="170" height="26" rx="13" fill="#8b5a2b"/><circle cx="500" cy="339" r="6" fill="#5e3b1a"/></g>""",
    "HM-044": """
  <rect x="185" y="130" width="230" height="340" rx="62" fill="#f6f7f9" stroke="#d6dae1" stroke-width="4"/>
  <rect x="236" y="166" width="128" height="58" rx="14" fill="#1f2126"/>
  <text x="300" y="206" text-anchor="middle" font-family="Helvetica, Arial" font-weight="700" font-size="28" fill="#2ec4b6">200°</text>
  <circle cx="300" cy="262" r="18" fill="#e3e6eb" stroke="#c4c9d1" stroke-width="3"/>
  <rect x="200" y="300" width="200" height="152" rx="34" fill="#2b2d33"/>
  <rect x="254" y="350" width="92" height="32" rx="16" fill="#4a4e57"/>
  <g fill="#3b3e46"><rect x="230" y="408" width="140" height="6" rx="3"/><rect x="230" y="422" width="140" height="6" rx="3"/></g>""",
    "HM-120": """
  <path d="M120 270 Q110 230 150 226 Q300 210 450 226 Q490 230 480 270 Q470 320 480 370 Q490 410 450 414 Q300 430 150 414 Q110 410 120 370 Q130 320 120 270 Z" fill="#e9edf4" stroke="#d3d9e3" stroke-width="4" transform="translate(10 -64)"/>
  <path d="M120 270 Q110 230 150 226 Q300 210 450 226 Q490 230 480 270 Q470 320 480 370 Q490 410 450 414 Q300 430 150 414 Q110 410 120 370 Q130 320 120 270 Z" fill="#ffffff" stroke="#d3d9e3" stroke-width="4" transform="translate(-6 40)"/>
  <g fill="none" stroke="#e1e6ee" stroke-width="4" stroke-linecap="round"><path d="M170 330 Q300 310 430 330"/><path d="M170 420 Q300 440 430 420"/></g>
  <rect x="380" y="380" width="60" height="26" rx="6" fill="#2a78d6" opacity=".85"/>""",
    "BK-007": """
  <path d="M206 116 H418 Q430 116 430 128 V444 Q430 456 418 456 H206 Z" fill="#f6efe1"/>
  <rect x="190" y="112" width="26" height="348" rx="6" fill="#d8c8a6"/>
  <path d="M430 128 V444" stroke="#e2d6bd" stroke-width="6"/>
  <text x="318" y="198" text-anchor="middle" font-family="Georgia, serif" font-weight="700" font-size="38" fill="#1f2937">ATOMIC</text>
  <text x="318" y="242" text-anchor="middle" font-family="Georgia, serif" font-weight="700" font-size="38" fill="#1f2937">HABITS</text>
  <g fill="#c2410c"><circle cx="270" cy="300" r="6"/><circle cx="294" cy="300" r="7"/><circle cx="318" cy="300" r="8"/><circle cx="342" cy="300" r="9"/><circle cx="366" cy="300" r="10"/></g>
  <text x="318" y="400" text-anchor="middle" font-family="Helvetica, Arial" font-size="16" letter-spacing="3" fill="#6b7280">PAPERBACK</text>""",
    "BE-055": """
  <ellipse cx="300" cy="182" rx="34" ry="44" fill="#2b2d33"/><rect x="270" y="214" width="60" height="44" rx="8" fill="#1f2126"/>
  <rect x="242" y="256" width="116" height="214" rx="26" fill="#c98034"/>
  <rect x="252" y="266" width="16" height="190" rx="8" fill="#ffffff" opacity=".22"/>
  <rect x="262" y="318" width="96" height="98" rx="8" fill="#fffaf2"/>
  <text x="310" y="358" text-anchor="middle" font-family="Helvetica, Arial" font-weight="800" font-size="24" fill="#c2410c">VIT C</text>
  <text x="310" y="388" text-anchor="middle" font-family="Helvetica, Arial" font-size="13" fill="#6b7280">30 ml</text>""",
    "GR-001": None,  # drawn below (scattered nuts)
}


def dry_fruits():
    rng = random.Random(7)
    nuts = []
    for _ in range(46):
        x, y = rng.uniform(178, 422), rng.uniform(262, 318)
        kind, rot = rng.random(), rng.uniform(0, 180)
        if kind < 0.45:
            nuts.append(f'<ellipse cx="{x:.0f}" cy="{y:.0f}" rx="15" ry="8" fill="#a8652f" transform="rotate({rot:.0f} {x:.0f} {y:.0f})"/>')
        elif kind < 0.8:
            nuts.append(f'<path d="M{x - 12:.0f} {y:.0f} q12 -18 24 0 q-12 -8 -24 0" fill="#ecd29c" stroke="#d5b878" stroke-width="2" '
                        f'transform="rotate({rot:.0f} {x:.0f} {y:.0f})"/>')
        else:
            nuts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="7" fill="#5a2e2e"/>')
    return f"""
  <path d="M160 290 L190 250 H410 L440 290 Z" fill="#9a6a36"/>
  <g>{''.join(nuts)}</g>
  <rect x="160" y="290" width="280" height="170" rx="10" fill="#c48d50"/>
  <path d="M160 290 H440" stroke="#a8743c" stroke-width="6"/>
  <rect x="210" y="330" width="180" height="90" rx="10" fill="#fff8ec"/>
  <text x="300" y="368" text-anchor="middle" font-family="Georgia, serif" font-weight="700" font-size="26" fill="#7c4a1c">Dry Fruits</text>
  <text x="300" y="400" text-anchor="middle" font-family="Helvetica, Arial" font-size="17" fill="#6b7280">Organic · 1 kg</text>"""


def svg(sku, category):
    light, dark = BG[category]
    body = DRAW[sku] if DRAW.get(sku) else dry_fruits()
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" viewBox="0 0 600 600">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{dark}"/></linearGradient>
    <filter id="soft" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur stdDeviation="10"/></filter>
  </defs>
  <rect width="600" height="600" fill="url(#bg)"/>
  <ellipse cx="300" cy="505" rx="190" ry="20" fill="#000" opacity=".13" filter="url(#soft)"/>{body}
</svg>"""


def chrome():
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome") or "",
              shutil.which("chromium") or ""):
        if c and os.path.exists(c):
            return c
    return None


def main():
    sys.path.insert(0, ROOT)
    from data.generate_data import CATALOG
    os.makedirs(OUT, exist_ok=True)
    exe = chrome()
    for sku, name, category, _ in CATALOG:
        path = os.path.join(OUT, f"{sku}.svg")
        with open(path, "w") as f:
            f.write(svg(sku, category))
        if not exe:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "p.html")
            with open(page, "w") as f:
                f.write(f'<html><body style="margin:0">{svg(sku, category)}</body></html>')
            subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={SIZE},{SIZE}",
                            f"--screenshot={os.path.join(OUT, sku + '.png')}", f"file://{page}"],
                           check=True, capture_output=True, timeout=60)
        print("rendered", sku, name)
    if not exe:
        print("Chrome not found — wrote SVGs only")


if __name__ == "__main__":
    main()
