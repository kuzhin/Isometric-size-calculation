
import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

import pymupdf


NUMBER_RE = re.compile(r"^\d{2,5}$")
EXCLUDED_VALUES = {4116}

# Geometry tolerances are in PDF points.
TEXT_TO_LINE_MAX = 18.0
PARALLEL_ANGLE_TOL = 3.0
NESTING_OFFSET_MAX = 25.0
MIN_OVERLAP = 10.0
MIN_CHILD_OVERLAP_FRACTION = 0.60
MIN_PARENT_CHILD_LENGTH_RATIO = 1.50


def clean_text(s):
    return " ".join(s.replace("\xa0", " ").split()).strip()


def is_numeric_dimension(text):
    s = clean_text(text)
    if not NUMBER_RE.fullmatch(s):
        return False
    value = int(s)
    return 20 <= value <= 99999 and value not in EXCLUDED_VALUES


def midpoint(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def segment_length(L):
    return math.hypot(L["x2"] - L["x1"], L["y2"] - L["y1"])


def segment_angle(L):
    return math.degrees(math.atan2(L["y2"] - L["y1"], L["x2"] - L["x1"])) % 180.0


def orientation(a):
    if min(a, 180.0 - a) <= 8.0:
        return "horizontal"
    if abs(a - 90.0) <= 8.0:
        return "vertical"
    return "diagonal"


def point_segment_distance(px, py, L):
    dx = L["x2"] - L["x1"]
    dy = L["y2"] - L["y1"]
    den = dx * dx + dy * dy
    if den == 0:
        return math.hypot(px - L["x1"], py - L["y1"]), 0.0
    t = ((px - L["x1"]) * dx + (py - L["y1"]) * dy) / den
    t = max(0.0, min(1.0, t))
    qx = L["x1"] + t * dx
    qy = L["y1"] + t * dy
    return math.hypot(px - qx, py - qy), t


def angle_parallel(a, b, tol=PARALLEL_ANGLE_TOL):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) <= tol


def projection_interval(child, parent):
    """
    Project child endpoints onto parent's direction.
    Returns [min,max] in parent's local coordinate system.
    """
    dx = parent["x2"] - parent["x1"]
    dy = parent["y2"] - parent["y1"]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    x0, y0 = parent["x1"], parent["y1"]

    vals = [
        (child["x1"] - x0) * ux + (child["y1"] - y0) * uy,
        (child["x2"] - x0) * ux + (child["y2"] - y0) * uy,
    ]
    return sorted(vals)


def perpendicular_offset(child, parent):
    dx = parent["x2"] - parent["x1"]
    dy = parent["y2"] - parent["y1"]
    L = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L, dy / L
    mx = (child["x1"] + child["x2"]) / 2.0
    my = (child["y1"] + child["y2"]) / 2.0
    px = mx - parent["x1"]
    py = my - parent["y1"]
    return abs(px * (-uy) + py * ux)


def interval_overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def interval_length(a):
    return max(0.0, a[1] - a[0])


def line_features(page):
    lines = []
    for drawing_id, drawing in enumerate(page.get_drawings()):
        items = drawing.get("items", [])
        for item_id, item in enumerate(items):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            L = {
                "id": len(lines),
                "drawing_id": drawing_id,
                "item_id": item_id,
                "x1": float(p1.x),
                "y1": float(p1.y),
                "x2": float(p2.x),
                "y2": float(p2.y),
                "stroke_width": float(drawing.get("width") or 0),
                "item_count_in_drawing": len(items),
            }
            L["length"] = segment_length(L)
            if L["length"] < 1.5:
                continue
            L["angle"] = segment_angle(L)
            L["orientation"] = orientation(L["angle"])
            lines.append(L)
    return lines


def text_objects(page):
    texts = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = clean_text(span.get("text", ""))
                if t:
                    texts.append({
                        "text": t,
                        "bbox": list(span["bbox"]),
                        "size": float(span.get("size") or 0),
                    })
    return texts


def find_candidates(texts, lines):
    candidates = []
    for t in texts:
        if not is_numeric_dimension(t["text"]):
            continue

        value = int(t["text"])
        cx, cy = midpoint(t["bbox"])

        nearest = []
        for L in lines:
            d, proj = point_segment_distance(cx, cy, L)
            if d <= TEXT_TO_LINE_MAX:
                nearest.append((d, L["id"], proj))
        nearest.sort()

        if not nearest:
            continue

        best = nearest[0]
        candidates.append({
            "id": len(candidates),
            "value": value,
            "text": t["text"],
            "bbox": t["bbox"],
            "center": [cx, cy],
            "text_size": t["size"],
            "associated_line_id": best[1],
            "association_distance": round(best[0], 3),
            "nearest_lines": [
                {"line_id": lid, "distance": round(d, 3), "projection": round(p, 4)}
                for d, lid, p in nearest[:8]
            ],
            "relations": [],
            "classification": "candidate",
            "classification_confidence": 0.0,
            "classification_evidence": [],
            "selected_for_calculation": False,
            "selection_reason": None,
            "parent_dimension_id": None,
        })
    return candidates


def build_relations(candidates, lines):
    by_id = {c["id"]: c for c in candidates}

    # Pairwise geometry relations between dimension-line candidates.
    for i, a in enumerate(candidates):
        la = lines[a["associated_line_id"]]
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            lb = lines[b["associated_line_id"]]

            if not angle_parallel(la["angle"], lb["angle"]):
                continue

            # Endpoint-to-endpoint proximity.
            endpoints_a = [(la["x1"], la["y1"]), (la["x2"], la["y2"])]
            endpoints_b = [(lb["x1"], lb["y1"]), (lb["x2"], lb["y2"])]
            endpoint_dist = min(
                math.hypot(xa-xb, ya-yb)
                for xa, ya in endpoints_a
                for xb, yb in endpoints_b
            )
            if endpoint_dist <= 3.0:
                rel = {
                    "other_id": b["id"],
                    "other_value": b["value"],
                    "relation": "endpoint_connected",
                    "distance": round(endpoint_dist, 3),
                }
                a["relations"].append(rel)
                b["relations"].append({
                    "other_id": a["id"],
                    "other_value": a["value"],
                    "relation": "endpoint_connected",
                    "distance": round(endpoint_dist, 3),
                })

            # Parallel overlap / nesting evidence.
            off = perpendicular_offset(lb, la)
            if off <= NESTING_OFFSET_MAX:
                ia = projection_interval(la, la)
                ib = projection_interval(lb, la)
                overlap = interval_overlap(ia, ib)

                if overlap >= MIN_OVERLAP:
                    rel = {
                        "other_id": b["id"],
                        "other_value": b["value"],
                        "relation": "parallel_overlap",
                        "offset": round(off, 3),
                        "overlap": round(overlap, 3),
                    }
                    a["relations"].append(rel)
                    b["relations"].append({
                        "other_id": a["id"],
                        "other_value": a["value"],
                        "relation": "parallel_overlap",
                        "offset": round(off, 3),
                        "overlap": round(overlap, 3),
                    })

    # Explicit parent/child nesting detection.
    #
    # A child is considered an internal detail when:
    # - its line is parallel to a longer candidate;
    # - it is sufficiently close to that line;
    # - most of the child interval overlaps the longer line;
    # - the longer line is substantially longer.
    #
    # This is geometry evidence, not an assertion that the dimensions should
    # be added/subtracted.
    for child in candidates:
        lc = lines[child["associated_line_id"]]
        child_len = lc["length"]

        best_parent = None
        best_score = -1.0

        for parent in candidates:
            if parent["id"] == child["id"]:
                continue
            lp = lines[parent["associated_line_id"]]
            parent_len = lp["length"]

            if parent_len < child_len * MIN_PARENT_CHILD_LENGTH_RATIO:
                continue
            if not angle_parallel(lc["angle"], lp["angle"]):
                continue

            off = perpendicular_offset(lc, lp)
            if off > NESTING_OFFSET_MAX:
                continue

            child_interval = projection_interval(lc, lp)
            parent_interval = projection_interval(lp, lp)
            overlap = interval_overlap(child_interval, parent_interval)
            frac = overlap / max(interval_length(child_interval), 1e-9)

            if frac < MIN_CHILD_OVERLAP_FRACTION:
                continue

            # Score rewards containment, parallelism and length hierarchy.
            containment = min(1.0, frac)
            offset_score = max(0.0, 1.0 - off / NESTING_OFFSET_MAX)
            ratio_score = min(1.0, (parent_len / child_len - 1.5) / 3.0 + 0.5)
            score = 0.55 * containment + 0.30 * offset_score + 0.15 * ratio_score

            if score > best_score:
                best_score = score
                best_parent = (parent, off, frac, overlap, score)

        if best_parent:
            parent, off, frac, overlap, score = best_parent
            child["classification"] = "internal_detail"
            child["classification_confidence"] = round(score, 3)
            child["classification_evidence"] = [{
                "type": "nested_in_longer_parallel_dimension",
                "parent_id": parent["id"],
                "parent_value": parent["value"],
                "offset": round(off, 3),
                "child_overlap_fraction": round(frac, 3),
                "overlap": round(overlap, 3),
            }]
            child["parent_dimension_id"] = parent["id"]

            if parent["classification"] == "candidate":
                parent["classification"] = "overall_or_parent_candidate"
                parent["classification_confidence"] = round(max(parent["classification_confidence"], score), 3)
                parent["classification_evidence"].append({
                    "type": "contains_internal_dimension",
                    "child_id": child["id"],
                    "child_value": child["value"],
                })

    # Anything not marked internal is a candidate for selection.
    for c in candidates:
        if c["classification"] == "candidate":
            c["classification"] = "independent_candidate"
            c["classification_confidence"] = round(
                max(0.50, 1.0 - min(1.0, c["association_distance"] / TEXT_TO_LINE_MAX)),
                3,
            )
            c["classification_evidence"].append({
                "type": "usable_dimension_line_association"
            })

        # Selection is intentionally explicit and conservative:
        # internal details are never selected automatically.
        if c["classification"] in {"internal_detail", "candidate"}:
            c["selected_for_calculation"] = False
            c["selection_reason"] = "internal/detail or unresolved candidate"
        else:
            c["selected_for_calculation"] = True
            if c["classification"] == "overall_or_parent_candidate":
                c["selection_reason"] = "parent/overall dimension retained; child detail excluded"
            else:
                c["selection_reason"] = "independent dimension candidate"

    return candidates


def calculate(candidates):
    selected = [c for c in candidates if c["selected_for_calculation"]]
    return selected, sum(c["value"] for c in selected)


def analyze_pdf(pdf_path, outdir):
    doc = pymupdf.open(pdf_path)
    report = {
        "source": str(pdf_path),
        "algorithm_version": "0.6",
        "notes": [
            "OCR is not used for vector PDFs.",
            "Classification is based on PDF geometry and text coordinates.",
            "Internal-detail classification is evidence-based and does not itself imply summation/subtraction.",
            "Only selected_for_calculation dimensions are included in total_length_mm.",
        ],
        "pages": [],
        "selected_dimensions": [],
        "total_length_mm": 0,
    }

    for page_no, page in enumerate(doc, 1):
        lines = line_features(page)
        texts = text_objects(page)
        candidates = find_candidates(texts, lines)
        candidates = build_relations(candidates, lines)
        selected, total = calculate(candidates)

        report["pages"].append({
            "page": page_no,
            "text_count": len(texts),
            "vector_line_count": len(lines),
            "dimension_candidates": candidates,
        })

        for c in selected:
            report["selected_dimensions"].append({
                "page": page_no,
                "id": c["id"],
                "value": c["value"],
                "classification": c["classification"],
                "classification_confidence": c["classification_confidence"],
                "selection_reason": c["selection_reason"],
            })

    report["total_length_mm"] = sum(x["value"] for x in report["selected_dimensions"])

    (outdir / "geometry_analysis_v06.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    with (outdir / "all_dimension_candidates_v06.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "page", "id", "value_mm", "classification", "confidence",
            "selected_for_calculation", "parent_dimension_id", "selection_reason"
        ])
        for p in report["pages"]:
            for c in p["dimension_candidates"]:
                w.writerow([
                    p["page"], c["id"], c["value"], c["classification"],
                    c["classification_confidence"], c["selected_for_calculation"],
                    c["parent_dimension_id"], c["selection_reason"]
                ])

    with (outdir / "selected_dimensions_v06.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["page", "id", "value_mm", "classification", "confidence"])
        for c in report["selected_dimensions"]:
            w.writerow([
                c["page"], c["id"], c["value"],
                c["classification"], c["classification_confidence"]
            ])

    doc.close()
    return report


def main(pdf):
    outdir = Path("result_v06")
    outdir.mkdir(parents=True, exist_ok=True)

    report = analyze_pdf(pdf, outdir)

    print(f"PDF: {pdf}")

    for page in report["pages"]:
        print(
            f"PAGE {page['page']}: "
            f"lines={page['vector_line_count']} "
            f"candidates={len(page['dimension_candidates'])}"
        )

    print("\nSELECTED DIMENSIONS")
    print("-------------------")

    values = [x["value"] for x in report["selected_dimensions"]]

    print(" + ".join(map(str, values)) if values else "(none)")
    print(f"\nTOTAL = {sum(values)} mm")


if __name__ == "__main__":
    main("1.pdf")
