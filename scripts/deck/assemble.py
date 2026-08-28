# -*- coding: utf-8 -*-
"""Assemble the R2 deck: 9 generated slides + 5 carried forward from R1."""
import os, re, shutil, zipfile
from pathlib import Path
import gen
from content_a import s_architecture, s_users, s_quality, s_efficiency, s_rigor
from content_b import s_roi, s_diff, s_roadmap, s_risks

ROOT = Path(__file__).resolve().parent
U = ROOT / "unpacked"
SL = U / "ppt" / "slides"

# --- 1. generate the nine new slides ---------------------------------------
builders = [("slide7.xml",  s_architecture),  # deck pos 5
            ("slide8.xml",  s_users),         # 6
            ("slide9.xml",  s_quality),       # 7
            ("slide10.xml", s_efficiency),    # 8
            ("slide11.xml", s_rigor),         # 9
            ("slide12.xml", s_roi),           # 10
            ("slide13.xml", s_diff),          # 11
            ("slide14.xml", s_roadmap),       # 12
            ("slide15.xml", s_risks)]         # 13
for fn, fx in builders:
    gen._uid[0] = 100                      # unique ids per slide
    (SL / fn).write_text(fx(), encoding="utf-8")
    shutil.copy(SL / "_rels" / "slide3.xml.rels", SL / "_rels" / f"{fn}.rels")
print(f"generated {len(builders)} slides")

# --- 2. cover: retitle for Round 2 ------------------------------------------
p = SL / "slide1.xml"; x = p.read_text(encoding="utf-8")
# The cover's boxes are part of a fixed composition: the title box holds exactly
# one line and R1's own title already filled 95% of it. Fit the words to the
# boxes rather than resizing artwork we cannot see.
x = x.replace("<a:t>AI reinvention made real</a:t>", "<a:t>AEGIS</a:t>")
x = x.replace("<a:t>Accenture Innovation Challenge 2026</a:t>",
              "<a:t>Accenture Innovation Challenge 2026  ·  Round 2</a:t>")
# Drop the subtitle to 14pt: the added "Round 2" wraps to a second line at 18pt,
# and the box holds exactly one. Scope the size change to that run only.
_i = x.find("Accenture Innovation Challenge")
_s = x.rfind("<p:sp>", 0, _i); _e = x.find("</p:sp>", _i)
x = x[:_s] + x[_s:_e].replace('sz="1800"', 'sz="1400"') + x[_e:]
p.write_text(x, encoding="utf-8"); print("cover retitled")

# --- 3. problem slide: relabel as section 1 ---------------------------------
p = SL / "slide3.xml"; x = p.read_text(encoding="utf-8")
x = x.replace("Problem statement - ControlPlane.ai: AI failures are found out, not caught first",
              "Problem: AI failures are found out, not caught first")
p.write_text(x, encoding="utf-8")

# --- 4. solution slide: drop the competitor card (now its own slide) and the
#        video reference (no recording exists yet) ---------------------------
p = SL / "slide4.xml"; x = p.read_text(encoding="utf-8")

def drop_shape_containing(xml, needle):
    i = xml.find(needle)
    if i == -1: raise SystemExit(f"not found: {needle}")
    s = xml.rfind("<p:sp>", 0, i)
    e = xml.find("</p:sp>", i) + len("</p:sp>")
    return xml[:s] + xml[e:]

x = drop_shape_containing(x, "VS. TODAY’S TOOLS")
# widen the DEPLOYMENT card across the freed row and drop its video sentence
x = x.replace("<a:t>The video shows a seeded failure flagged, held and rerouted live.</a:t>",
              "<a:t>Four use-case policies ship, with deliberately different risk and latency "
              "profiles. Competitive positioning is on its own slide later in this deck.</a:t>")
x = x.replace("<a:t>SEE IT RUN</a:t>", "<a:t>WHAT SHIPS TODAY</a:t>")
x = re.sub(r'(<p:cNvPr id="238" name="card238"/>.*?<a:off x=")\d+(" y=")\d+(")',
           lambda m: m.group(1) + str(gen.E(gen.ML)) + m.group(2) + "4550000" + m.group(3),
           x, flags=re.S)
x = re.sub(r'(<p:cNvPr id="238" name="card238"/>.*?<a:ext cx=")\d+(" cy=")\d+(")',
           lambda m: m.group(1) + str(gen.E(gen.CW)) + m.group(2) + "1250000" + m.group(3),
           x, flags=re.S)
# The R1 title carries a non-breaking space after the dash, so match on the
# stable tail rather than the whole string.
x = re.sub(r"Proposed solution[^<]*for every AI response",
           "Solution design: three stages, and why their order is the design", x)
# R1 gave its own slide 4 a shorter title bar than slide 3 (0.46in vs 0.52in).
# Normalise it to the grid every other content slide uses.
x = re.sub(r'(<p:cNvPr id="4" name="Title 3"/>.*?<a:off x=")\d+(" y=")\d+("/><a:ext cx=")\d+(" cy=")\d+(")',
           lambda m: (m.group(1) + str(gen.E(gen.ML)) + m.group(2) + str(gen.E(gen.TITLE_Y))
                      + m.group(3) + str(gen.E(gen.CW)) + m.group(4)
                      + str(gen.E(gen.TITLE_H)) + m.group(5)),
           x, count=1, flags=re.S)
p.write_text(x, encoding="utf-8"); print("solution slide adapted")

# --- 5. content types -------------------------------------------------------
ct = U / "[Content_Types].xml"; c = ct.read_text(encoding="utf-8")
for fn, _ in builders:
    if f"/ppt/slides/{fn}" not in c:
        c = c.replace("</Types>",
                      f'<Override PartName="/ppt/slides/{fn}" ContentType="application/'
                      f'vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>')
ct.write_text(c, encoding="utf-8")

# --- 6. presentation rels ---------------------------------------------------
pr = U / "ppt" / "_rels" / "presentation.xml.rels"; r = pr.read_text(encoding="utf-8")
used = {int(m) for m in re.findall(r'Id="rId(\d+)"', r)}
nxt = max(used) + 1
rid_for = {}
add = ""
for fn, _ in builders:
    rid_for[fn] = f"rId{nxt}"
    add += (f'<Relationship Id="rId{nxt}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/slide" Target="slides/{fn}"/>')
    nxt += 1
pr.write_text(r.replace("</Relationships>", add + "</Relationships>"), encoding="utf-8")

# existing slide -> rId, from the rels file
existing = dict((t.split("/")[-1], i) for i, t in
                re.findall(r'Id="(rId\d+)"[^>]*Target="(slides/slide\d+\.xml)"', r))
existing = {t.split("/")[-1]: i for i, t in
            re.findall(r'Id="(rId\d+)"[^>]*Target="(slides/slide\d+\.xml)"', r)}

# --- 7. slide order: drop the video slide, interleave the new ones -----------
order = ["slide1.xml", "slide2.xml", "slide3.xml", "slide4.xml"] \
        + [fn for fn, _ in builders] + ["slide6.xml"]
px = U / "ppt" / "presentation.xml"; x = px.read_text(encoding="utf-8")
ids, sid = [], 900
for fn in order:
    rid = existing.get(fn) or rid_for[fn]
    ids.append(f'<p:sldId id="{sid}" r:id="{rid}"/>'); sid += 1
x = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>",
           "<p:sldIdLst>" + "".join(ids) + "</p:sldIdLst>", x, flags=re.S)
px.write_text(x, encoding="utf-8")
print(f"slide order set: {len(order)} slides, video slide dropped")

# --- 8. repack --------------------------------------------------------------
out = ROOT / "AEGIS_R2_Business_Proposal.pptx"
if out.exists(): out.unlink()
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _, files in os.walk(U):
        for f in files:
            full = Path(base) / f
            z.write(full, str(full.relative_to(U)))
print("wrote", out, f"({out.stat().st_size/1e6:.1f} MB)")
