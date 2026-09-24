
import argparse, json, math, re, csv
from pathlib import Path
import fitz

NUMBER_RE = re.compile(r"^\d{3,5}$")

# Prototype exclusions. These are semantic patterns, not OCR.
EXCLUDED_VALUES = {
    4116,  # instrument/tag value in the sample
}
EXCLUDED_PREFIXES = ("X ", "Y ", "Z+", "DN")

def clean(s):
    return " ".join(s.replace("\xa0", " ").split()).strip()

def midpoint(b):
    return ((b[0]+b[2])/2, (b[1]+b[3])/2)

def point_segment_distance(px, py, x1, y1, x2, y2):
    dx, dy = x2-x1, y2-y1
    if dx == 0 and dy == 0:
        return math.hypot(px-x1, py-y1), (x1,y1), 0.0
    t = ((px-x1)*dx + (py-y1)*dy)/(dx*dx+dy*dy)
    t = max(0.0, min(1.0, t))
    qx, qy = x1+t*dx, y1+t*dy
    return math.hypot(px-qx, py-qy), (qx,qy), t

def angle_deg(x1,y1,x2,y2):
    a = math.degrees(math.atan2(y2-y1,x2-x1))
    a = a % 180
    return a

def length(x1,y1,x2,y2):
    return math.hypot(x2-x1,y2-y1)

def orientation(a):
    # a is 0..180
    if min(a, 180-a) < 8:
        return "horizontal"
    if abs(a-90) < 8:
        return "vertical"
    return "diagonal"

def parallel(a,b,tol=8):
    d=abs(a-b)%180
    return min(d,180-d) <= tol

def endpoints(seg):
    if isinstance(seg, dict):
        return [(seg["x1"],seg["y1"]),(seg["x2"],seg["y2"])]
    return [(seg[0],seg[1]),(seg[2],seg[3])]

def endpoint_gap(sa,sb):
    return min(math.hypot(ax-bx,ay-by) for ax,ay in endpoints(sa) for bx,by in endpoints(sb))

def projection_interval(seg, axis):
    if isinstance(seg, dict):
        x1,y1,x2,y2=seg["x1"],seg["y1"],seg["x2"],seg["y2"]
    else:
        x1,y1,x2,y2=seg[:4]
    if axis=="horizontal":
        return sorted((x1,x2))
    if axis=="vertical":
        return sorted((y1,y2))
    # diagonal: project to segment direction
    dx,dy=x2-x1,y2-y1
    L=math.hypot(dx,dy) or 1
    ux,uy=dx/L,dy/L
    return sorted((x1*ux+y1*uy, x2*ux+y2*uy))

def extract_page(page):
    # 1) Text objects
    texts=[]
    for block in page.get_text("dict").get("blocks",[]):
        for line in block.get("lines",[]):
            for span in line.get("spans",[]):
                t=clean(span.get("text",""))
                if t:
                    texts.append({"text":t, "bbox":list(span["bbox"])})

    # 2) Every vector line from PDF drawings
    lines=[]
    for d in page.get_drawings():
        for item in d.get("items",[]):
            if item[0] != "l":
                continue
            p1,p2=item[1],item[2]
            x1,y1,x2,y2=map(float,(p1.x,p1.y,p2.x,p2.y))
            L=length(x1,y1,x2,y2)
            if L < 1.5:
                continue
            a=angle_deg(x1,y1,x2,y2)
            lines.append({
                "x1":x1,"y1":y1,"x2":x2,"y2":y2,
                "length":L,"angle":a,"orientation":orientation(a),
                "seqno":d.get("seqno")
            })

    # 3) Candidate dimension text
    candidates=[]
    for i,t in enumerate(texts):
        s=t["text"]
        if not NUMBER_RE.fullmatch(s):
            continue
        value=int(s)
        if value in EXCLUDED_VALUES or value < 50 or value > 9999:
            continue
        # exclude single coordinate-like combined objects (e.g. "X 104000")
        if any(s.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        cx,cy=midpoint(t["bbox"])

        # nearest vector segments
        nearby=[]
        for j,L in enumerate(lines):
            d,q,tparam=point_segment_distance(cx,cy,L["x1"],L["y1"],L["x2"],L["y2"])
            nearby.append((d,j,q,tparam))
        nearby.sort(key=lambda z:z[0])

        # keep several candidates so later stages can reason about ambiguity
        nearest=[]
        for d,j,q,tp in nearby[:8]:
            if d <= 18:
                nearest.append({
                    "line_id":j,
                    "distance":round(d,2),
                    "projection":round(tp,3)
                })

        associated = nearest[0] if nearest else None
        item={
            "id":len(candidates),
            "value":value,
            "text":s,
            "bbox":t["bbox"],
            "center":[cx,cy],
            "nearest_lines":nearest,
            "associated_line_id": associated["line_id"] if associated else None,
            "association_distance": associated["distance"] if associated else None
        }
        candidates.append(item)

    # 4) Relations between candidate dimensions based on associated vector lines.
    # This is deliberately conservative: it says "nested/parallel evidence",
    # not "these values must be summed/subtracted".
    for c in candidates:
        c["relations"]=[]

    for i,a in enumerate(candidates):
        if a["associated_line_id"] is None:
            continue
        la = lines[a["associated_line_id"]]
        for j,b in enumerate(candidates):
            if j <= i or b["associated_line_id"] is None:
                continue
            lb = lines[b["associated_line_id"]]

            # A) geometric connection of line endpoints
            gap = endpoint_gap(la, lb)
            if gap <= 15:
                a["relations"].append({"other_id":b["id"],"other_value":b["value"],
                                       "relation":"endpoint_connected","gap":round(gap,2)})
                b["relations"].append({"other_id":a["id"],"other_value":a["value"],
                                       "relation":"endpoint_connected","gap":round(gap,2)})

            # B) parallel dimension evidence
            if not parallel(la["angle"], lb["angle"]):
                continue
            if la["orientation"] == "vertical" and lb["orientation"] == "vertical":
                offset=abs(((la["x1"]+la["x2"])/2)-((lb["x1"]+lb["x2"])/2))
                ia=projection_interval(la,"vertical"); ib=projection_interval(lb,"vertical")
            elif la["orientation"] == "horizontal" and lb["orientation"] == "horizontal":
                offset=abs(((la["y1"]+la["y2"])/2)-((lb["y1"]+lb["y2"])/2))
                ia=projection_interval(la,"horizontal"); ib=projection_interval(lb,"horizontal")
            else:
                continue
            overlap=max(0,min(ia[1],ib[1])-max(ia[0],ib[0]))
            if overlap <= 10:
                continue
            inter_len=max(1e-9, min(ia[1]-ia[0], ib[1]-ib[0]))
            if offset <= 35:
                if ia[0] <= ib[0] and ia[1] >= ib[1]:
                    relation="contains"
                elif ib[0] <= ia[0] and ib[1] >= ia[1]:
                    relation="contained_by"
                else:
                    relation="partial_overlap"
            else:
                relation="parallel"
            a["relations"].append({"other_id":b["id"],"other_value":b["value"],
                                   "relation":relation,"overlap":round(overlap,2),
                                   "overlap_ratio":round(overlap/inter_len,3)})
            b["relations"].append({"other_id":a["id"],"other_value":a["value"],
                                   "relation":("contained_by" if relation=="contains" else "contains" if relation=="contained_by" else relation),
                                   "overlap":round(overlap,2),
                                   "overlap_ratio":round(overlap/inter_len,3)})

    return texts,lines,candidates

def draw_debug(page, candidates, lines):
    # Show all associated dimension lines in one color and boxes around texts.
    for c in candidates:
        if c["associated_line_id"] is not None:
            L=lines[c["associated_line_id"]]
            page.draw_line(
                fitz.Point(L["x1"],L["y1"]),
                fitz.Point(L["x2"],L["y2"]),
                color=(0,0,1), width=1.0
            )
        b=fitz.Rect(*c["bbox"])
        page.draw_rect(b, color=(1,0,0), width=1.3)
        label=fitz.Rect(b.x0,b.y0-10,b.x0+42,b.y0)
        page.insert_textbox(label, f'{c["id"]}:{c["value"]}', fontsize=5.5,
                            color=(1,0,0))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("pdf",type=Path)
    ap.add_argument("--outdir",type=Path,default=Path("result_v03"))
    args=ap.parse_args()
    args.outdir.mkdir(parents=True,exist_ok=True)

    doc=fitz.open(args.pdf)
    report={"source":str(args.pdf),"pages":[]}

    for page_no,page in enumerate(doc,1):
        texts,lines,candidates=extract_page(page)
        draw_debug(page,candidates,lines)
        report["pages"].append({
            "page":page_no,
            "text_count":len(texts),
            "vector_line_count":len(lines),
            "dimension_candidates":candidates,
            "lines":lines
        })

    debug_pdf=args.outdir/"geometry_debug.pdf"
    doc.save(debug_pdf)
    doc.close()

    (args.outdir/"geometry_analysis.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"
    )

    with (args.outdir/"dimension_line_links.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        w.writerow(["id","value","associated_line_id","association_distance","relations"])
        for p in report["pages"]:
            for c in p["dimension_candidates"]:
                w.writerow([
                    c["id"],c["value"],c["associated_line_id"],
                    c["association_distance"],
                    "; ".join(f'{r["relation"]}:{r["other_value"]}' for r in c["relations"])
                ])

    print(f"pages={len(report['pages'])}")
    for p in report["pages"]:
        print(f'page {p["page"]}: {p["vector_line_count"]} vector lines, '
              f'{len(p["dimension_candidates"])} dimension candidates')
        for c in p["dimension_candidates"]:
            print(f'  {c["id"]}: {c["value"]} -> line {c["associated_line_id"]}, '
                  f'd={c["association_distance"]}, relations={c["relations"]}')

if __name__=="__main__":
    main()
