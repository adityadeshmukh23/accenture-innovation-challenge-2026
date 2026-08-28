# -*- coding: utf-8 -*-
"""Build the AEGIS Round 2 business-proposal deck on the R1 Accenture 2025 template.

Every shape is emitted in the exact idiom extracted from R1's own content slides
(slide3 / slide4): roundRect adj=6000 cards, A100FF headers, Arial body, the
0.395in / 12.335in grid. New slides reuse slideLayout25 -- the same
'1_Standard slide_no bullets' family -- so the deck reads as one document.
"""
from xml.sax.saxutils import escape

EMU = 914400
def E(inches): return int(round(inches * EMU))

# --- palette, lifted from the template -------------------------------------
PURPLE   = "A100FF"   # header / accent, as used in R1 runs
TINT     = "F5EBFF"   # tinted card fill
WHITE    = "FFFFFF"
INK      = "2B2B2B"   # body dark
MUTED    = "666666"
GREEN    = "1E9E4A"
AMBER    = "D69500"
RED      = "D0342C"

# --- grid ------------------------------------------------------------------
ML       = 0.395              # left margin
CW       = 12.335             # content width
COL3     = [0.395, 4.595, 8.795]
COL3W    = 3.93
TITLE_Y, TITLE_H = 0.4225, 0.515
LEDE_Y,  LEDE_H  = 1.028, 0.612

_uid = [100]
def nid():
    _uid[0] += 1
    return _uid[0]

def run(text, sz=1000, b=False, color=None, i=False):
    return {"t": text, "sz": sz, "b": b, "color": color, "i": i}

def para(runs, align=None, space_after=500, line_pct=None):
    return {"runs": runs, "align": align, "sa": space_after, "lnpct": line_pct}

def _run_xml(r):
    props = f' sz="{r["sz"]}"'
    if r["b"]: props += ' b="1"'
    if r["i"]: props += ' i="1"'
    fill = f'<a:solidFill><a:srgbClr val="{r["color"]}"/></a:solidFill>' if r["color"] else ""
    return (f'<a:r><a:rPr lang="en-IN"{props}>{fill}'
            f'<a:latin typeface="Arial" panose="020B0604020202020204" pitchFamily="34" charset="0"/>'
            f'<a:cs typeface="Arial" panose="020B0604020202020204" pitchFamily="34" charset="0"/>'
            f'</a:rPr><a:t>{escape(r["t"])}</a:t></a:r>')

def _para_xml(p):
    algn = f' algn="{p["align"]}"' if p["align"] else ""
    ln = f'<a:lnSpc><a:spcPct val="{p["lnpct"]}"/></a:lnSpc>' if p.get("lnpct") else ""
    ppr = f'<a:pPr{algn}>{ln}<a:spcAft><a:spcPts val="{p["sa"]}"/></a:spcAft></a:pPr>'
    return f'<a:p>{ppr}{"".join(_run_xml(r) for r in p["runs"])}</a:p>'

def _body(paras, anchor=None, ins=None):
    a = f' anchor="{anchor}"' if anchor else ""
    m = ""
    if ins:
        m = (f' lIns="{E(ins[0])}" tIns="{E(ins[1])}"'
             f' rIns="{E(ins[2])}" bIns="{E(ins[3])}"')
    return (f'<p:txBody><a:bodyPr{a}{m}><a:normAutofit/></a:bodyPr><a:lstStyle/>'
            f'{"".join(_para_xml(p) for p in paras)}</p:txBody>')

def shape(name, x, y, w, h, paras, geom="rect", fill=None, line=None,
          line_w=15875, anchor=None, ins=None, adj=6000):
    i = nid()
    if geom == "roundRect":
        g = (f'<a:prstGeom prst="roundRect"><a:avLst>'
             f'<a:gd name="adj" fmla="val {adj}"/></a:avLst></a:prstGeom>')
    else:
        g = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
    f = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else '<a:noFill/>'
    l = (f'<a:ln w="{line_w}"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>'
         if line else '<a:ln><a:noFill/></a:ln>')
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="{name}"/>'
            f'<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{E(x)}" y="{E(y)}"/>'
            f'<a:ext cx="{E(w)}" cy="{E(h)}"/></a:xfrm>{g}{f}{l}</p:spPr>'
            f'{_body(paras, anchor, ins)}</p:sp>')

def rule(name, x, y, w, color=PURPLE, wt=12700):
    i = nid()
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(w)}" cy="0"/></a:xfrm>'
            f'<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
            f'<a:ln w="{wt}"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:ln>'
            f'</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>')

def dot(name, x, y, d=0.17, color=PURPLE):
    i = nid()
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{E(x)}" y="{E(y)}"/><a:ext cx="{E(d)}" cy="{E(d)}"/></a:xfrm>'
            f'<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln>'
            f'</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>')

def title(text):
    i = nid()
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="Title 3"/>'
            f'<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr><p:ph type="title" idx="4294967295"/></p:nvPr></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{E(ML)}" y="{E(TITLE_Y)}"/>'
            f'<a:ext cx="{E(CW)}" cy="{E(TITLE_H)}"/></a:xfrm>'
            f'<a:solidFill><a:srgbClr val="{PURPLE}"/></a:solidFill></p:spPr>'
            f'<p:txBody><a:bodyPr anchor="ctr"><a:normAutofit/></a:bodyPr><a:lstStyle/>'
            f'<a:p><a:r><a:rPr lang="en-IN" sz="2000" b="1">'
            f'<a:solidFill><a:schemeClr val="bg1"/></a:solidFill>'
            f'<a:latin typeface="Graphik"/><a:cs typeface="Arial"/></a:rPr>'
            f'<a:t>{escape(text)}</a:t></a:r></a:p></p:txBody></p:sp>')

def lede(runs, y=LEDE_Y, h=LEDE_H):
    return shape("lede", ML, y, CW, h, [para(runs, space_after=0, line_pct="112000")])

def card(x, y, w, h, header, body_runs, tinted=True, hsz=1100, ins=(0.13, 0.10, 0.13, 0.08)):
    """The template's two card styles: tinted fill, or white with a purple rule."""
    paras = [para([run(header, sz=hsz, b=True, color=PURPLE)], space_after=400)]
    paras.append(para(body_runs, space_after=0, line_pct="107000"))
    if tinted:
        return shape("card", x, y, w, h, paras, geom="roundRect", fill=TINT, ins=ins)
    return shape("card", x, y, w, h, paras, geom="roundRect", fill=WHITE,
                 line=PURPLE, ins=ins)

def slide(shapes):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
            '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
            '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>'
            '</p:grpSpPr>' + "".join(shapes) +
            '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
