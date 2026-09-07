"""
tools/documents.py
---------------------
Generates real artifacts (PDF invoice, PPTX sales-analysis deck) from live
database data. Every number in these documents traces back to a real query
in this file — nothing here is written or estimated by the model; the
model only decides WHEN to call these, never what numbers go in them.

Fonts: reportlab's built-in Helvetica does NOT include the ₹ glyph — it
silently renders as a black box instead of erroring, which is exactly the
kind of silent failure that's easy to ship without noticing. We register
DejaVu Sans (confirmed present on this system) and use it everywhere
instead.

Charts: built as NATIVE PowerPoint chart objects (python-pptx's
CategoryChartData + add_chart), not rendered images. A pasted-in picture
of a chart can't be edited, resized, or re-colored in PowerPoint/Google
Slides — a native chart can. This was corrected after checking a real
generated deck's internal XML and finding only ppt/media/*.png with no
ppt/charts/ folder, i.e. the earlier version was silently doing the
picture-paste thing despite looking fine visually.
"""

import os
from datetime import date, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

from database.database import get_db, round2 as _round2
from tools.store_tools import get_bill_summary, get_preference, get_reorder_suggestions

DOCUMENTS_DIR = "documents"

_DEJAVU_REGULAR = r"C:\Windows\Fonts\arial.ttf"
_DEJAVU_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
_FONT = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_fonts_ready = False


def _ensure_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    pdfmetrics.registerFont(TTFont(_FONT, _DEJAVU_REGULAR))
    pdfmetrics.registerFont(TTFont(_FONT_BOLD, _DEJAVU_BOLD))
    _fonts_ready = True


# ---------------------------------------------------------------------------
# PDF invoice
# ---------------------------------------------------------------------------

def generate_invoice_pdf(bill_id):
    """
    Builds a PDF invoice for an already-finalized bill. Refuses for a bill
    that's still a draft — an invoice is a record of a completed sale, not
    a quote.
    """
    summary = get_bill_summary(bill_id)
    if summary["status"] != "ok":
        return summary
    if summary["bill_status"] != "finalized":
        return {
            "status": "error",
            "reason": f"Bill {bill_id} is still a draft — finalize it before generating an invoice.",
        }

    _ensure_fonts()
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    file_path = os.path.join(DOCUMENTS_DIR, f"invoice_{bill_id}.pdf")

    shop_name = get_preference("shop_name")["value"] or "Kirana Store"
    #logo_path = get_preference("shop_logo_path")["value"]
    logo_path = os.path.join(DOCUMENTS_DIR, "my_logo.png")
    print("Logo path:", logo_path)
    print("Logo exists:", os.path.exists(logo_path))
    footer_message = get_preference("invoice_footer_message")["value"] or "Thank you for shopping with us!"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontName=_FONT_BOLD, fontSize=28,
                                  alignment=TA_CENTER)
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontName=_FONT, fontSize=10)
    bold = ParagraphStyle("bold", parent=styles["Normal"], fontName=_FONT_BOLD, fontSize=11)
    small = ParagraphStyle("small", parent=styles["Normal"], fontName=_FONT, fontSize=8, textColor=colors.grey,
                            alignment=TA_LEFT)

    doc = SimpleDocTemplate(
        file_path, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )

    # Branded header: logo in a left column, shop name in a middle column,
    # and an empty right-hand column the SAME width as the logo column.
    # That symmetric left/right padding is what makes the title cell
    # actually centered on the page — a 2-column [logo | title] layout
    # centers the title only within the leftover space next to the logo,
    # which visually reads as left-of-center on the full page. Falls back
    # gracefully to plain text if no logo was ever set — never errors on
    # a missing file.
    if logo_path and os.path.exists(logo_path):
        header_left = RLImage(logo_path, width=44 * mm, height=44 * mm)
    else:
        header_left = Spacer(1, 1)
    header_center = [Paragraph(shop_name, title_style)]
    header_right = Spacer(1, 1)
    header_table = Table(
        [[header_left, header_center, header_right]],
        colWidths=[48 * mm, 74 * mm, 48 * mm],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 2, colors.HexColor("#2c3e50")),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
    ]))

    story = [
        header_table,
        Spacer(1, 14),
        Paragraph(f"Invoice #{bill_id}", bold),
        Paragraph(f"Payment method: {summary['payment_method']}", normal),
        Spacer(1, 14),
    ]

    table_data = [["Item", "HSN", "Qty", "Unit", "Price", "GST%", "CGST", "SGST", "Total"]]
    for item in summary["items"]:
        table_data.append([
            item["product"], item["hsn_code"], f"{item['quantity']:g}", item["unit"],
            f"\u20b9{item['unit_price']:.2f}", f"{item['gst_rate']:g}%",
            f"\u20b9{item['cgst_amount']:.2f}", f"\u20b9{item['sgst_amount']:.2f}",
            f"\u20b9{item['line_total']:.2f}",
        ])

    item_table = Table(table_data, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 14))

    totals_data = [
        ["Subtotal", f"\u20b9{summary['subtotal']:.2f}"],
        ["Total GST", f"\u20b9{summary['tax_total']:.2f}"],
        ["Grand Total", f"\u20b9{summary['grand_total']:.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=[100, 100], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), _FONT),
        ("FONTNAME", (0, -1), (-1, -1), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]))
    story.append(totals_table)
    story.append(Spacer(1, 24))
    story.append(Paragraph(footer_message, small))

    doc.build(story)
    return {"status": "ok", "bill_id": bill_id, "file_path": file_path}


# ---------------------------------------------------------------------------
# PPTX sales analysis deck — data layer
# ---------------------------------------------------------------------------

def _query_range_summary(conn, start_date, end_date):
    bills = conn.execute(
        "SELECT * FROM bills WHERE status='finalized' AND date(finalized_at) BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchall()
    if not bills:
        return None

    total_sales = _round2(sum(b["grand_total"] for b in bills))
    tax_collected = _round2(sum(b["tax_total"] for b in bills))
    bill_count = len(bills)

    by_method = {}
    for b in bills:
        by_method[b["payment_method"]] = by_method.get(b["payment_method"], 0) + b["grand_total"]
    by_method = {k: _round2(v) for k, v in by_method.items()}

    daily = conn.execute(
        """SELECT date(finalized_at) AS d, SUM(grand_total) AS total
           FROM bills WHERE status='finalized' AND date(finalized_at) BETWEEN ? AND ?
           GROUP BY d ORDER BY d""",
        (start_date, end_date),
    ).fetchall()
    daily_by_date = {r["d"]: r["total"] for r in daily}

    # Fill every date in the range, including days with zero sales — a
    # missing bar is easy to misread as "no data" instead of "no sales".
    complete_daily = []
    d = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while d <= end:
        iso = d.isoformat()
        complete_daily.append({"date": iso, "total": daily_by_date.get(iso, 0)})
        d += timedelta(days=1)

    bill_ids = [b["bill_id"] for b in bills]
    placeholders = ",".join("?" * len(bill_ids))
    # pretax_revenue and cost_total let us compute real margin, separately
    # from "revenue" (which includes GST collected on the owner's behalf —
    # not their money, and mixing it into a margin figure would overstate it).
    top_items = conn.execute(
        f"""SELECT p.name, SUM(bi.quantity) AS qty_sold, SUM(bi.line_total) AS revenue,
                   SUM(bi.unit_price * bi.quantity) AS pretax_revenue,
                   SUM(bi.quantity * p.cost_price) AS cost_total
            FROM bill_items bi JOIN products p ON p.product_id = bi.product_id
            WHERE bi.bill_id IN ({placeholders})
            GROUP BY p.product_id ORDER BY revenue DESC LIMIT 5""",
        bill_ids,
    ).fetchall()

    return {
        "total_sales": total_sales,
        "tax_collected": tax_collected,
        "bill_count": bill_count,
        "avg_bill_value": _round2(total_sales / bill_count),
        "by_method": by_method,
        "daily": complete_daily,
        "top_items": [
            {
                "name": r["name"],
                "qty_sold": r["qty_sold"],
                "revenue": r["revenue"],
                "margin": _round2(r["pretax_revenue"] - r["cost_total"]),
            }
            for r in top_items
        ],
    }


def _get_low_stock(conn):
    rows = conn.execute(
        "SELECT name, unit, stock_quantity, reorder_level FROM products "
        "WHERE stock_quantity <= reorder_level ORDER BY stock_quantity ASC"
    ).fetchall()
    return [
        {"name": r["name"], "unit": r["unit"], "stock_quantity": r["stock_quantity"], "reorder_level": r["reorder_level"]}
        for r in rows
    ]


def generate_sales_deck(start_date=None, end_date=None):
    """
    Builds a PPTX sales-analysis deck for a date range. Defaults to the
    last 7 days (inclusive) if no dates given — "this week's sales".
    Every chart and number comes from a real query in this file; there is
    no model-authored commentary in the numbers themselves.
    """
    if end_date is None:
        end_date = date.today().isoformat()
    if start_date is None:
        start_date = (date.fromisoformat(end_date) - timedelta(days=6)).isoformat()

    with get_db(exclusive=False) as conn:
        current = _query_range_summary(conn, start_date, end_date)
        if current is None:
            return {
                "status": "error",
                "reason": f"No finalized bills between {start_date} and {end_date} — nothing to analyze.",
            }

        # Previous period of equal length, for a real (not invented) % comparison
        period_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
        prev_end = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
        prev_start = (date.fromisoformat(prev_end) - timedelta(days=period_days - 1)).isoformat()
        previous = _query_range_summary(conn, prev_start, prev_end)

        low_stock = _get_low_stock(conn)

    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    shop_name = get_preference("shop_name")["value"] or "Kirana Store"

    deck_path = os.path.join(DOCUMENTS_DIR, f"sales_deck_{start_date}_to_{end_date}.pptx")
    _build_deck(deck_path, shop_name, start_date, end_date, current, previous, low_stock)

    return {"status": "ok", "file_path": deck_path, "start_date": start_date, "end_date": end_date}


def _pct_change(current, previous):
    if previous in (None, 0):
        return None
    return _round2((current - previous) / previous * 100)


def period_days_text(start_date, end_date):
    days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    return "day" if days == 1 else f"{days}-day period"


# ---------------------------------------------------------------------------
# PPTX sales analysis deck — slide building
# ---------------------------------------------------------------------------

DARK = RGBColor(0x2C, 0x3E, 0x50)
GREY = RGBColor(0x7F, 0x8C, 0x8D)


def _add_title(slide, text, size=32):
    box = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(12), Inches(0.9))
    tf = box.text_frame
    tf.text = text
    run = tf.paragraphs[0].runs[0]
    run.font.size = Pt(size)
    run.font.bold = True
    run.font.color.rgb = DARK
    return box


def _add_native_bar_chart(slide, categories, values, series_name, chart_type, title):
    """Native PowerPoint chart object — editable/resizable in PowerPoint or
    Google Slides, unlike a pasted-in picture of a chart."""
    chart_data = CategoryChartData()
    chart_data.categories = categories
    chart_data.add_series(series_name, values)
    graphic_frame = slide.shapes.add_chart(
        chart_type, Inches(1.0), Inches(1.5), Inches(11.3), Inches(5.3), chart_data
    )
    chart = graphic_frame.chart
    chart.has_legend = False
    chart.has_title = True
    chart.chart_title.text_frame.text = title
    if chart_type == XL_CHART_TYPE.BAR_CLUSTERED:
        # Horizontal bar charts plot the first category at the BOTTOM by
        # default. Our data is already sorted "highest first" — without
        # this, the top-ranked item would render at the bottom, which
        # reads backwards.
        chart.category_axis.reverse_order = True
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.number_format = "\u20b9#,##0"
    plot.data_labels.number_format_is_linked = False
    return chart


def _build_deck(path, shop_name, start_date, end_date, current, previous, low_stock):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # --- Slide 1: title ---
    slide = prs.slides.add_slide(blank_layout)
    box = slide.shapes.add_textbox(Inches(1), Inches(2.8), Inches(11.3), Inches(1.2))
    tf = box.text_frame
    tf.text = f"{shop_name} — Sales Analysis"
    tf.paragraphs[0].runs[0].font.size = Pt(40)
    tf.paragraphs[0].runs[0].font.bold = True
    tf.paragraphs[0].runs[0].font.color.rgb = DARK
    box2 = slide.shapes.add_textbox(Inches(1), Inches(3.9), Inches(11.3), Inches(0.6))
    tf2 = box2.text_frame
    tf2.text = f"{start_date} to {end_date}"
    tf2.paragraphs[0].runs[0].font.size = Pt(20)
    tf2.paragraphs[0].runs[0].font.color.rgb = GREY

    # --- Slide 2: key metrics ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "Key Metrics")
    metrics = [
        ("Total Sales", f"\u20b9{current['total_sales']:.2f}"),
        ("Bills", str(current["bill_count"])),
        ("Avg. Bill Value", f"\u20b9{current['avg_bill_value']:.2f}"),
        ("Tax Collected", f"\u20b9{current['tax_collected']:.2f}"),
    ]
    card_w = Inches(2.8)
    for i, (label, value) in enumerate(metrics):
        x = Inches(0.6 + i * (card_w.inches + 0.2))
        box = slide.shapes.add_textbox(x, Inches(2.0), card_w, Inches(1.5))
        tf = box.text_frame
        tf.word_wrap = True
        p1 = tf.paragraphs[0]
        p1.text = value
        p1.runs[0].font.size = Pt(28)
        p1.runs[0].font.bold = True
        p1.runs[0].font.color.rgb = DARK
        p2 = tf.add_paragraph()
        p2.text = label
        p2.runs[0].font.size = Pt(14)
        p2.runs[0].font.color.rgb = GREY

    box = slide.shapes.add_textbox(Inches(0.6), Inches(4.0), Inches(6), Inches(0.5))
    box.text_frame.text = "Payment methods:"
    box.text_frame.paragraphs[0].runs[0].font.bold = True
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(16)
    box.text_frame.paragraphs[0].runs[0].font.color.rgb = DARK
    for i, (method, amount) in enumerate(current["by_method"].items()):
        line = slide.shapes.add_textbox(Inches(0.8), Inches(4.5 + i * 0.4), Inches(6), Inches(0.4))
        line.text_frame.text = f"{method}: \u20b9{amount:.2f}"
        line.text_frame.paragraphs[0].runs[0].font.size = Pt(14)

    # --- Slide 3: low stock alert — most actionable slide in the deck ---
    if low_stock:
        slide = prs.slides.add_slide(blank_layout)
        _add_title(slide, "Low Stock \u2014 Reorder Soon")
        box = slide.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.5), Inches(4.8))
        tf = box.text_frame
        tf.word_wrap = True
        for i, item in enumerate(low_stock[:10]):  # cap at 10 so it never overflows the slide
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = f"\u2022 {item['name']}: {item['stock_quantity']:g} {item['unit']} left (reorder at {item['reorder_level']:g})"
            p.runs[0].font.size = Pt(18)
            p.runs[0].font.color.rgb = DARK
            p.space_after = Pt(12)
        if len(low_stock) > 10:
            note = tf.add_paragraph()
            note.text = f"...and {len(low_stock) - 10} more."
            note.runs[0].font.size = Pt(14)
            note.runs[0].font.color.rgb = GREY

    # --- Slide 4: daily revenue trend — native chart ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "Daily Revenue Trend")
    _add_native_bar_chart(
        slide,
        categories=[d["date"][5:] for d in current["daily"]],
        values=[d["total"] for d in current["daily"]],
        series_name="Revenue",
        chart_type=XL_CHART_TYPE.COLUMN_CLUSTERED,
        title="Daily Revenue",
    )

    # --- Slide 5: top products — native chart ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "Top Products")
    _add_native_bar_chart(
        slide,
        categories=[i["name"] for i in current["top_items"]],
        values=[i["revenue"] for i in current["top_items"]],
        series_name="Revenue",
        chart_type=XL_CHART_TYPE.BAR_CLUSTERED,
        title="Top Products by Revenue",
    )

    # --- Slide 6: insights, computed from real numbers only ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "Insights")
    insights = []

    if previous:
        change = _pct_change(current["total_sales"], previous["total_sales"])
        if change is not None:
            direction = "up" if change >= 0 else "down"
            insights.append(
                f"Revenue is {direction} {abs(change):.1f}% vs. the previous {period_days_text(start_date, end_date)}."
            )

    if current["top_items"]:
        top_revenue = current["top_items"][0]
        share = _round2(top_revenue["revenue"] / current["total_sales"] * 100) if current["total_sales"] else 0
        insights.append(
            f"{top_revenue['name']} was the top earner by revenue, contributing "
            f"\u20b9{top_revenue['revenue']:.2f} ({share:.1f}% of total sales)."
        )

        top_margin = max(current["top_items"], key=lambda i: i["margin"])
        if top_margin["name"] != top_revenue["name"]:
            insights.append(
                f"But {top_margin['name']} was the most PROFITABLE line, with \u20b9{top_margin['margin']:.2f} "
                f"margin \u2014 revenue leader and profit leader aren't the same product."
            )
        else:
            insights.append(f"{top_margin['name']} was also the most profitable line, with \u20b9{top_margin['margin']:.2f} margin.")

    if current["daily"]:
        best_day = max(current["daily"], key=lambda d: d["total"])
        insights.append(f"Best day was {best_day['date']}, with \u20b9{best_day['total']:.2f} in sales.")

    if current["total_sales"]:
        tax_share = _round2(current["tax_collected"] / current["total_sales"] * 100)
        insights.append(f"Tax collected was \u20b9{current['tax_collected']:.2f}, {tax_share:.1f}% of total sales.")

    insights.append(f"Average bill value was \u20b9{current['avg_bill_value']:.2f} across {current['bill_count']} bills.")

    if low_stock:
        insights.append(f"{len(low_stock)} product(s) are at or below their reorder level \u2014 see the Low Stock slide.")

    box = slide.shapes.add_textbox(Inches(0.7), Inches(1.6), Inches(12), Inches(5.3))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(insights):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"\u2022 {line}"
        p.runs[0].font.size = Pt(16)
        p.runs[0].font.color.rgb = DARK
        p.space_after = Pt(12)

    prs.save(path)


# ---------------------------------------------------------------------------
# PPTX weekly REVIEW deck — a different document from generate_sales_deck.
#
# generate_sales_deck() answers "how did this period do?" for an
# owner-chosen date range, on demand.
#
# generate_weekly_review_deck() is what the scheduler sends automatically
# every week, and answers a different question on purpose: "what should
# change next week?" — multi-week trend (not a single-period snapshot),
# week-over-week product movers, reorder priorities (reuses Feature 3's
# get_reorder_suggestions), and khata follow-ups. Every number here still
# comes from a real query, same as the rest of this file — the "Action
# Items" slide is built from fixed thresholds on real numbers, never
# free-text model commentary.
# ---------------------------------------------------------------------------

TREND_WEEKS = 6            # how many weeks back the trend chart covers
KHATA_FOLLOWUP_THRESHOLD = 500   # ₹ balance above which a customer is flagged
KHATA_FOLLOWUP_LIMIT = 5         # cap so the slide never overflows


def _week_bounds(end_date):
    """The 7-day window ending on end_date (inclusive), and the 7-day
    window immediately before that — used everywhere in this deck as
    'this week' vs 'last week'."""
    end = date.fromisoformat(end_date)
    cur_start = end - timedelta(days=6)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    return cur_start.isoformat(), end.isoformat(), prev_start.isoformat(), prev_end.isoformat()


def _query_weekly_trend(conn, end_date, num_weeks=TREND_WEEKS):
    """Total revenue for each of the last num_weeks 7-day windows ending
    at end_date, oldest first — the actual trajectory, not one snapshot."""
    end = date.fromisoformat(end_date)
    weeks = []
    for i in range(num_weeks - 1, -1, -1):
        w_end = end - timedelta(days=7 * i)
        w_start = w_end - timedelta(days=6)
        row = conn.execute(
            "SELECT COALESCE(SUM(grand_total), 0) AS total FROM bills "
            "WHERE status='finalized' AND date(finalized_at) BETWEEN ? AND ?",
            (w_start.isoformat(), w_end.isoformat()),
        ).fetchone()
        weeks.append({"label": f"{w_start.isoformat()[5:]}", "total": _round2(row["total"])})
    return weeks


def _query_product_movers(conn, cur_start, cur_end, prev_start, prev_end, top_n=3):
    """Per-product revenue this week vs last week, so the owner can see
    what's trending up (worth pushing/restocking ahead) and what's
    trending down (worth investigating) — something a single-period
    snapshot can never show."""

    def _revenue_by_product(start, end):
        rows = conn.execute(
            """SELECT p.name, SUM(bi.line_total) AS revenue
               FROM bill_items bi
               JOIN bills b ON b.bill_id = bi.bill_id
               JOIN products p ON p.product_id = bi.product_id
               WHERE b.status='finalized' AND date(b.finalized_at) BETWEEN ? AND ?
               GROUP BY p.product_id""",
            (start, end),
        ).fetchall()
        return {r["name"]: r["revenue"] for r in rows}

    cur = _revenue_by_product(cur_start, cur_end)
    prev = _revenue_by_product(prev_start, prev_end)

    movers = []
    for name in set(cur) | set(prev):
        cur_rev = cur.get(name, 0)
        prev_rev = prev.get(name, 0)
        if cur_rev == 0 and prev_rev == 0:
            continue
        if prev_rev == 0:
            change_pct = None  # brand new this week — can't express as a % change
        else:
            change_pct = _round2((cur_rev - prev_rev) / prev_rev * 100)
        movers.append({
            "name": name, "cur_revenue": _round2(cur_rev), "prev_revenue": _round2(prev_rev),
            "change_pct": change_pct,
        })

    # Risers: real prior-week revenue to compare against, sorted by biggest % gain.
    risers = sorted(
        [m for m in movers if m["change_pct"] is not None and m["change_pct"] > 0],
        key=lambda m: m["change_pct"], reverse=True,
    )[:top_n]
    # Decliners: same requirement, sorted by biggest % drop.
    decliners = sorted(
        [m for m in movers if m["change_pct"] is not None and m["change_pct"] < 0],
        key=lambda m: m["change_pct"],
    )[:top_n]
    return risers, decliners


def _query_khata_followups(conn, threshold=KHATA_FOLLOWUP_THRESHOLD, limit=KHATA_FOLLOWUP_LIMIT):
    """Customers whose outstanding khata balance is above the threshold,
    largest first — an operational to-do the old deck never surfaced."""
    rows = conn.execute(
        """SELECT k.customer_name,
                  COALESCE(SUM(CASE WHEN t.transaction_type='charge' THEN t.amount ELSE 0 END), 0) -
                  COALESCE(SUM(CASE WHEN t.transaction_type='payment' THEN t.amount ELSE 0 END), 0) AS balance
           FROM khata k LEFT JOIN khata_transactions t ON t.customer_id = k.customer_id
           GROUP BY k.customer_id
           HAVING balance > ?
           ORDER BY balance DESC
           LIMIT ?""",
        (threshold, limit),
    ).fetchall()
    return [{"customer_name": r["customer_name"], "balance": _round2(r["balance"])} for r in rows]


def generate_weekly_review_deck(end_date=None):
    """
    Builds the automatic 'Weekly Business Review' deck — deliberately a
    different document from generate_sales_deck (see module note above).
    Defaults to the 7 days ending today, same as the weekly cadence the
    scheduler runs on.
    """
    if end_date is None:
        end_date = date.today().isoformat()
    cur_start, cur_end, prev_start, prev_end = _week_bounds(end_date)

    with get_db(exclusive=False) as conn:
        current = _query_range_summary(conn, cur_start, cur_end)
        if current is None:
            return {
                "status": "error",
                "reason": f"No finalized bills between {cur_start} and {cur_end} — nothing to review.",
            }
        previous = _query_range_summary(conn, prev_start, prev_end)
        trend = _query_weekly_trend(conn, cur_end)
        risers, decliners = _query_product_movers(conn, cur_start, cur_end, prev_start, prev_end)
        khata_followups = _query_khata_followups(conn)

    reorder = get_reorder_suggestions()
    reorder_suggestions = reorder["suggestions"] if reorder["status"] == "ok" else []

    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    shop_name = get_preference("shop_name")["value"] or "Kirana Store"

    deck_path = os.path.join(DOCUMENTS_DIR, f"weekly_review_{cur_start}_to_{cur_end}.pptx")
    _build_weekly_review_deck(
        deck_path, shop_name, cur_start, cur_end, trend, current, previous,
        risers, decliners, reorder_suggestions, khata_followups,
    )

    return {"status": "ok", "file_path": deck_path, "start_date": cur_start, "end_date": cur_end}


def _add_bullet_list(slide, title, lines, empty_message, y_start=1.7, font_size=16):
    _add_title(slide, title)
    box = slide.shapes.add_textbox(Inches(0.7), Inches(y_start), Inches(12), Inches(5.2))
    tf = box.text_frame
    tf.word_wrap = True
    display_lines = lines if lines else [empty_message]
    for i, line in enumerate(display_lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"\u2022 {line}"
        p.runs[0].font.size = Pt(font_size)
        p.runs[0].font.color.rgb = DARK if lines else GREY
        p.space_after = Pt(10)


def _build_weekly_review_deck(path, shop_name, start_date, end_date, trend, current, previous,
                               risers, decliners, reorder_suggestions, khata_followups):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # --- Slide 1: title ---
    slide = prs.slides.add_slide(blank_layout)
    box = slide.shapes.add_textbox(Inches(1), Inches(2.8), Inches(11.3), Inches(1.2))
    tf = box.text_frame
    tf.text = f"{shop_name} \u2014 Weekly Business Review"
    tf.paragraphs[0].runs[0].font.size = Pt(40)
    tf.paragraphs[0].runs[0].font.bold = True
    tf.paragraphs[0].runs[0].font.color.rgb = DARK
    box2 = slide.shapes.add_textbox(Inches(1), Inches(3.9), Inches(11.3), Inches(0.6))
    tf2 = box2.text_frame
    tf2.text = f"{start_date} to {end_date}"
    tf2.paragraphs[0].runs[0].font.size = Pt(20)
    tf2.paragraphs[0].runs[0].font.color.rgb = GREY

    # --- Slide 2: revenue trend over several weeks, not just this one ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, f"Revenue Trend \u2014 Last {len(trend)} Weeks")
    _add_native_bar_chart(
        slide,
        categories=[w["label"] for w in trend],
        values=[w["total"] for w in trend],
        series_name="Weekly Revenue",
        chart_type=XL_CHART_TYPE.LINE_MARKERS,
        title="Revenue by Week (week starting)",
    )

    # --- Slide 3: this week vs last week ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "This Week vs. Last Week")
    metrics = [
        ("Total Sales", current["total_sales"], previous["total_sales"] if previous else None, True),
        ("Bills", current["bill_count"], previous["bill_count"] if previous else None, False),
        ("Avg. Bill Value", current["avg_bill_value"], previous["avg_bill_value"] if previous else None, True),
        ("Tax Collected", current["tax_collected"], previous["tax_collected"] if previous else None, True),
    ]
    card_w = Inches(2.8)
    for i, (label, cur_val, prev_val, is_currency) in enumerate(metrics):
        x = Inches(0.6 + i * (card_w.inches + 0.2))
        box = slide.shapes.add_textbox(x, Inches(2.0), card_w, Inches(2.2))
        tf = box.text_frame
        tf.word_wrap = True
        display = f"\u20b9{cur_val:.2f}" if is_currency else str(cur_val)
        p1 = tf.paragraphs[0]
        p1.text = display
        p1.runs[0].font.size = Pt(26)
        p1.runs[0].font.bold = True
        p1.runs[0].font.color.rgb = DARK
        p2 = tf.add_paragraph()
        p2.text = label
        p2.runs[0].font.size = Pt(14)
        p2.runs[0].font.color.rgb = GREY
        change = _pct_change(cur_val, prev_val) if prev_val is not None else None
        p3 = tf.add_paragraph()
        if change is None:
            p3.text = "(no prior week to compare)"
            p3.runs[0].font.color.rgb = GREY
        else:
            direction = "\u25b2" if change >= 0 else "\u25bc"
            p3.text = f"{direction} {abs(change):.1f}% vs last week"
            p3.runs[0].font.color.rgb = RGBColor(0x27, 0xAE, 0x60) if change >= 0 else RGBColor(0xC0, 0x39, 0x2B)
        p3.runs[0].font.size = Pt(14)
        p3.runs[0].font.bold = True

    # --- Slide 4: product movers — what to push, what to check on ---
    slide = prs.slides.add_slide(blank_layout)
    _add_title(slide, "Product Movers vs. Last Week")
    box = slide.shapes.add_textbox(Inches(0.6), Inches(1.8), Inches(6), Inches(0.5))
    box.text_frame.text = "\U0001F4C8 Trending Up"
    box.text_frame.paragraphs[0].runs[0].font.bold = True
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
    box.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(0x27, 0xAE, 0x60)
    for i, m in enumerate(risers):
        line = slide.shapes.add_textbox(Inches(0.8), Inches(2.3 + i * 0.5), Inches(6), Inches(0.5))
        line.text_frame.text = f"{m['name']}: \u20b9{m['cur_revenue']:.2f} (+{m['change_pct']:.1f}%)"
        line.text_frame.paragraphs[0].runs[0].font.size = Pt(15)
    if not risers:
        line = slide.shapes.add_textbox(Inches(0.8), Inches(2.3), Inches(6), Inches(0.5))
        line.text_frame.text = "No clear risers this week."
        line.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        line.text_frame.paragraphs[0].runs[0].font.color.rgb = GREY

    box = slide.shapes.add_textbox(Inches(6.9), Inches(1.8), Inches(6), Inches(0.5))
    box.text_frame.text = "\U0001F4C9 Trending Down"
    box.text_frame.paragraphs[0].runs[0].font.bold = True
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
    box.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)
    for i, m in enumerate(decliners):
        line = slide.shapes.add_textbox(Inches(7.1), Inches(2.3 + i * 0.5), Inches(6), Inches(0.5))
        line.text_frame.text = f"{m['name']}: \u20b9{m['cur_revenue']:.2f} ({m['change_pct']:.1f}%)"
        line.text_frame.paragraphs[0].runs[0].font.size = Pt(15)
    if not decliners:
        line = slide.shapes.add_textbox(Inches(7.1), Inches(2.3), Inches(6), Inches(0.5))
        line.text_frame.text = "No clear decliners this week."
        line.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        line.text_frame.paragraphs[0].runs[0].font.color.rgb = GREY

    # --- Slide 5: reorder priorities for next week (Feature 3's data) ---
    slide = prs.slides.add_slide(blank_layout)
    reorder_lines = [
        f"{r['name']}: {r['days_of_stock_left']:.1f} days of stock left "
        f"at current pace \u2014 reorder \u2248{r['suggested_reorder_qty']:g} {r['unit']}"
        for r in reorder_suggestions[:10]
    ]
    _add_bullet_list(
        slide, "Reorder Priorities for Next Week", reorder_lines,
        empty_message="Nothing projected to run out soon \u2014 no reorders needed this week.",
        font_size=16,
    )

    # --- Slide 6: khata follow-ups ---
    slide = prs.slides.add_slide(blank_layout)
    khata_lines = [f"{k['customer_name']}: \u20b9{k['balance']:.2f} outstanding" for k in khata_followups]
    _add_bullet_list(
        slide, "Khata Follow-ups", khata_lines,
        empty_message=f"No customer is over \u20b9{KHATA_FOLLOWUP_THRESHOLD} outstanding right now.",
        font_size=16,
    )

    # --- Slide 7: action items — rule-based, from the real numbers above ---
    slide = prs.slides.add_slide(blank_layout)
    actions = []

    change = _pct_change(current["total_sales"], previous["total_sales"]) if previous else None
    if change is not None and change < 0:
        actions.append(f"Revenue is down {abs(change):.1f}% vs last week \u2014 worth understanding why before next week.")
    elif change is not None and change > 0:
        actions.append(f"Revenue is up {change:.1f}% vs last week \u2014 check the Product Movers slide for what's driving it.")

    if reorder_suggestions:
        urgent = [r for r in reorder_suggestions if r["days_of_stock_left"] <= 3]
        if urgent:
            names = ", ".join(r["name"] for r in urgent[:5])
            actions.append(f"{len(urgent)} item(s) have 3 days of stock or less \u2014 reorder before next week: {names}.")

    if decliners:
        actions.append(f"{decliners[0]['name']} dropped {abs(decliners[0]['change_pct']):.1f}% \u2014 check pricing, stock, or placement.")

    if risers:
        actions.append(f"{risers[0]['name']} is up {risers[0]['change_pct']:.1f}% \u2014 make sure it doesn't run out next week.")

    if khata_followups:
        top = khata_followups[0]
        actions.append(f"Follow up with {top['customer_name']} on \u20b9{top['balance']:.2f} outstanding khata.")

    if not actions:
        actions.append("No urgent action items this week \u2014 steady as it goes.")

    _add_bullet_list(slide, "Action Items for Next Week", actions, empty_message="", font_size=18)

    prs.save(path)
