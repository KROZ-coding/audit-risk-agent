"""Generate the small, synthetic PDF used by the offline CI acceptance gate."""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "tests" / "fixtures" / "petrochina_2025_h1_sanitized.pdf"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(OUTPUT), pagesize=A4)
    canvas.setTitle("Sanitized offline acceptance fixture")

    text = canvas.beginText(20 * mm, 270 * mm)
    text.setFont("Helvetica", 10)
    lines = [
        "Sanitized offline acceptance fixture",
        "Synthetic annual-report text for CI only; contains no source-report data.",
        "Reporting period: 2025 first half.",
        "Audit opinion: unaudited interim report.",
        "The report includes revenue, operating cost, profit, assets, liabilities,",
        "cash flow, disclosure notes, and multi-year comparison sections.",
        "This fixture exists only to prove that the PDF parsing and deterministic",
        "offline toolchain run in a clean checkout without private source files.",
    ]
    for line in lines:
        text.textLine(line)
    canvas.drawText(text)
    canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    main()
