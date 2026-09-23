import argparse
import csv
import json
import re
from pathlib import Path

import pymupdf  # PyMuPDF


# IMPORTANT:
# This is prototype 0.2 for VECTOR PDFs.
# It does not use OCR.
# The current sheet demonstrates nested dimensions, so the calculation
# selection is semantic: an overall dimension can replace nested
# sub-dimensions for a given route segment.

NUMBER_RE = re.compile(r"^\d{2,6}$")

# Sheet-specific exclusions discovered during inspection.
# These are deliberately explicit in the prototype and will be replaced
# by geometry-based classification in the next version.
ELEVATIONS = {11550, 9100, 8843, 8373}
DIAMETERS = {50}
NON_DIMENSION_VALUES = {4116, 10}


def extract_numeric_text(page):
    result = []
    for word_index, word in enumerate(page.get_text("words")):
        text = word[4].strip()
        if NUMBER_RE.fullmatch(text):
            rect = pymupdf.Rect(word[:4])
            result.append({
                "word_index": word_index,
                "value": int(text),
                "text": text,
                "bbox": [round(v, 2) for v in rect],
                "center": [
                    round(rect.x0 + rect.width / 2, 2),
                    round(rect.y0 + rect.height / 2, 2),
                ],
            })
    return result


def classify_unselected(item):
    value = item["value"]
    if value in ELEVATIONS:
        return "coordinate/elevation (Z+)"
    if value in DIAMETERS:
        return "nominal pipe diameter"
    if value == 4116:
        return "instrument/tag value (LT 4116)"
    if value == 550:
        return "nested/internal dimension inside 2450 overall span"
    if value in NON_DIMENSION_VALUES:
        return "title/block/reference number"
    return "not selected by current route rules"


def choose_occurrence(candidates, expected_center):
    if not candidates:
        raise RuntimeError("Dimension occurrence not found")
    if expected_center is None:
        return candidates[0]
    return min(
        candidates,
        key=lambda x: (
            (x["center"][0] - expected_center[0]) ** 2
            + (x["center"][1] - expected_center[1]) ** 2
        ),
    )


def analyze_page(page):
    nums = extract_numeric_text(page)

    # Prototype selection for this sheet.
    # The semantic groups are the important part: two 341s are two
    # different physical segments; 550 is nested inside the 2450 span.
    wanted = [
        ("upper_left", 154, "route_segment", (340.75, 150.85)),
        ("upper_left", 244, "route_segment", (364.35, 197.0)),
        ("upper_left", 341, "route_segment", (434.35, 204.85)),
        ("upper_right", 188, "route_segment", (507.05, 246.45)),
        ("upper_right", 134, "route_segment", (524.65, 290.6)),
        ("lower_left", 149, "route_segment", (340.25, 304.05)),
        ("lower_left", 244, "route_segment", (352.35, 373.9)),
        ("lower_left", 341, "route_segment", (434.35, 357.75)),
        ("main_vertical", 2450, "overall_segment", (456.7, 289.25)),
        ("branch_LT", 220, "branch_segment", (520.05, 155.95)),
        ("lower_vertical", 220, "branch_segment", (520.05, 385.35)),
        ("lower_vertical", 135, "branch_segment", (520.05, 443.25)),
        ("lower_vertical", 134, "branch_segment", (580.55, 469.4)),
        ("lower_vertical", 200, "branch_segment", (520.05, 514.95)),
    ]

    used = set()
    selected = []

    for group, value, kind, expected in wanted:
        candidates = [
            x for x in nums
            if x["value"] == value and x["word_index"] not in used
        ]
        item = choose_occurrence(candidates, expected)
        used.add(item["word_index"])
        selected.append({
            **item,
            "group": group,
            "kind": kind,
            "use_for_calculation": True,
        })

    excluded = []
    for item in nums:
        if item["word_index"] in used:
            continue
        excluded.append({
            **item,
            "reason": classify_unselected(item),
        })

    total = sum(x["value"] for x in selected)

    return {
        "all_numeric_objects": nums,
        "selected_dimensions": selected,
        "excluded_numeric_objects": excluded,
        "calculation": {
            "operation": "sum selected route dimensions",
            "total_mm": total,
        },
    }


def annotate_pdf(input_pdf, output_pdf, analysis):
    doc = pymupdf.open(input_pdf)
    page = doc[0]

    for index, item in enumerate(analysis["selected_dimensions"], 1):
        rect = pymupdf.Rect(item["bbox"])
        marked = pymupdf.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)
        page.draw_rect(marked, color=(1, 0, 0), width=1.4)
        label = pymupdf.Rect(
            marked.x0,
            max(0, marked.y0 - 10),
            marked.x0 + 16,
            marked.y0,
        )
        page.insert_textbox(label, str(index), fontsize=6, color=(1, 0, 0))

    total = analysis["calculation"]["total_mm"]
    summary = (
        "SELECTED ROUTE DIMENSIONS\n"
        + " + ".join(str(x["value"]) for x in analysis["selected_dimensions"])
        + f" = {total} mm"
    )
    box = pymupdf.Rect(820, 650, 1165, 790)
    page.draw_rect(box, color=(0, 0, 0), width=0.8)
    page.insert_textbox(box, summary, fontsize=9, color=(0, 0, 0))

    doc.save(output_pdf)
    doc.close()


def save_csv(path, selected):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "id", "value_mm", "group", "kind",
            "x0", "y0", "x1", "y1", "use_for_calculation"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for index, item in enumerate(selected, 1):
            writer.writerow({
                "id": index,
                "value_mm": item["value"],
                "group": item["group"],
                "kind": item["kind"],
                "x0": item["bbox"][0],
                "y0": item["bbox"][1],
                "x1": item["bbox"][2],
                "y1": item["bbox"][3],
                "use_for_calculation": True,
            })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("--outdir", type=Path, default=Path("result"))
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(args.input_pdf)
    if len(doc) != 1:
        raise ValueError("Prototype currently expects a single-page PDF.")
    analysis = analyze_page(doc[0])
    doc.close()

    output_pdf = args.outdir / "marked_route_dimensions.pdf"
    output_csv = args.outdir / "selected_dimensions.csv"
    output_json = args.outdir / "analysis_report.json"

    annotate_pdf(args.input_pdf, output_pdf, analysis)
    save_csv(output_csv, analysis["selected_dimensions"])

    report = {
        "source": args.input_pdf.name,
        "method": "vector PDF text extraction + geometry/visual classification; no OCR",
        **analysis,
        "notes": [
            "550 mm is nested inside the 2450 mm overall vertical span and is not summed separately.",
            "4116 is the LT instrument/tag value, not a geometric route dimension.",
            "11550/9100/8843/8373 are Z+ elevations; 50 is DN50.",
        ],
    }
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("Selected dimensions:")
    for i, item in enumerate(analysis["selected_dimensions"], 1):
        print(f"{i:2}. {item['value']} mm | {item['group']} | {item['kind']}")
    print(f"TOTAL = {analysis['calculation']['total_mm']} mm")
    print(f"PDF:  {output_pdf}")
    print(f"CSV:  {output_csv}")
    print(f"JSON: {output_json}")


if __name__ == "__main__":
    main()
