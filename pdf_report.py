"""Render a print-ready PDF timesheet from report data (fpdf2).

A branded header band (company name, period, optional logo), a per-employee
summary table, and — when a single employee is in view — a day-by-day table.
"""

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# Core PDF fonts are Latin-1 only; map the few non-Latin-1 glyphs we use.
_REPLACEMENTS = {
    "—": "-", "–": "-", "·": "-", "•": "-",
    "…": "...", "’": "'", "“": '"', "”": '"',
    "⚓": "",
}


def _s(text):
    text = "" if text is None else str(text)
    for bad, good in _REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


NAVY = (20, 37, 79)
RED = (224, 35, 31)
INK = (15, 35, 51)
MUTED = (95, 118, 137)
LIGHT = (238, 242, 248)
WHITE = (255, 255, 255)

# Summary columns: (heading, width mm, formatter) — widths fit A4 portrait
# (usable width ~190mm).
SUMMARY_COLS = [
    ("Employee", 38, lambda r: r.name),
    ("Position", 30, lambda r: r.position or "-"),
    ("Net h", 18, lambda r: f"{r.net_hours:.2f}"),
    ("OT h", 16, lambda r: f"{r.overtime:.2f}"),
    ("Present", 18, lambda r: str(r.present_days)),
    ("Weekend", 20, lambda r: str(r.weekend_days)),
    ("Late", 14, lambda r: str(r.late_days)),
    ("Absent", 16, lambda r: str(r.absent_days)),
    ("Offshore", 16, lambda r: str(r.offshore_days)),
]

DAY_COLS = [
    ("Date", 28, lambda d: d.day.isoformat()),
    ("Day", 16, lambda d: d.weekday),
    ("Status", 26, lambda d: d.status),
    ("In", 22, lambda d: d.clock_in.strftime("%H:%M") if d.clock_in else "-"),
    ("Out", 22, lambda d: d.clock_out.strftime("%H:%M") if d.clock_out else "-"),
    ("Net h", 20, lambda d: f"{d.net_hours:.2f}"),
    ("OT h", 18, lambda d: f"{d.overtime:.2f}"),
    ("Lunch", 18, lambda d: "yes" if d.lunch_worked else "-"),
]


class ReportPDF(FPDF):
    def __init__(self, company, fleet, period, logo_path=None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.company = company
        self.fleet = fleet
        self.period = period
        self.logo_path = logo_path
        self.set_auto_page_break(True, margin=16)
        self.set_title(f"Timesheet {period}")

    def header(self):
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, 20, "F")
        text_x = 10
        if self.logo_path:
            try:
                self.image(self.logo_path, x=8, y=3.5, h=13)
                text_x = 30
            except Exception:
                text_x = 10
        self.set_xy(text_x, 4)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 6, _s(self.company), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(text_x)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(200, 215, 235)
        self.cell(0, 5, _s(f"Crew Time & Attendance   |   {self.period}"))
        self.set_fill_color(*RED)
        self.rect(0, 20, self.w, 1.1, "F")
        self.ln(16)

    def footer(self):
        self.set_y(-12)
        self.set_draw_color(*RED)
        self.set_line_width(0.3)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.set_y(-11)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 8, _s(f"{self.company}  -  Fleet: {self.fleet}"), align="L")
        self.set_y(-11)
        self.cell(0, 8, f"Page {self.page_no()}", align="R")

    def section_title(self, text):
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*NAVY)
        self.cell(0, 8, _s(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def table(self, columns, rows):
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(*NAVY)
        self.set_text_color(*WHITE)
        for heading, width, _ in columns:
            self.cell(width, 8, heading, border=0, align="C", fill=True)
        self.ln()
        self.set_text_color(*INK)
        fill = False
        for row in rows:
            self.set_font("Helvetica", "", 8)
            self.set_fill_color(*(LIGHT if fill else WHITE))
            for i, (_, width, getter) in enumerate(columns):
                align = "L" if i == 0 else "C"
                self.cell(width, 7, _s(getter(row)), border=0, align=align, fill=True)
            self.ln()
            fill = not fill


def build_pdf(reports, period, company, fleet, logo_path=None, detail=None):
    pdf = ReportPDF(company, fleet, period, logo_path=logo_path)
    pdf.add_page()

    pdf.section_title("Summary")
    pdf.table(SUMMARY_COLS, reports)

    if detail is not None:
        pdf.ln(4)
        pdf.section_title(f"{detail.name} - day by day")
        if detail.days:
            pdf.table(DAY_COLS, detail.days)
        else:
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*MUTED)
            pdf.cell(0, 7, "No days in this range.", ln=1)

    out = pdf.output()
    return bytes(out)
