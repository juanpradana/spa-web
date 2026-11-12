"""Ekspor laporan okupansi ke Excel (.xlsx) dan CSV.

Format Excel mengikuti standar tabel pengukuran monitoring SFR (UPT Komdigi /
Balmon) dengan penataan gaya yang konsisten dengan `archerfish/build_excel.py`:
header berlatar gelap, font Segoe UI, baris belang-belang (zebra striping),
dan persentase okupansi bergradasi warna lembut.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import REPORTS_DIR
from occupancy import ChannelStats, SurveyRun

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    openpyxl = None


# --------------------------------------------------------------------------- #
# Palet gaya Excel (ala instrumen SFR)
# --------------------------------------------------------------------------- #
DARK_NAVY = "1A2530"
ACCENT_BLUE = "2A5F8A"
LIGHT_GRAY = "F4F6F8"
WHITE = "FFFFFF"
BORDER_GRAY = "D0D5DD"
TEXT_DARK = "1D2939"
TEXT_MUTED = "667085"

# Warna status okupansi
FILL_CLEAR = "E6F4EA"     # hijau muda (<5%)
FILL_LOW = "FEF3D6"       # kuning muda (5-20%)
FILL_MED = "FEE4D2"       # jingga muda (20-50%)
FILL_HIGH = "FCE8E6"      # merah muda (>50%)


def _border_all(thin_color: str = BORDER_GRAY) -> Border:
    s = Side(border_style="thin", color=thin_color)
    return Border(left=s, right=s, top=s, bottom=s)


def export_excel(
    stats: Dict[str, ChannelStats],
    survey: Optional[SurveyRun] = None,
    out_path: Optional[Path] = None,
    title: str = "Laporan Pengukuran Okupansi Spektrum",
) -> Path:
    if openpyxl is None:
        raise RuntimeError("openpyxl tidak terpasang; tidak dapat membuat file .xlsx")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_path is None:
        out_path = REPORTS_DIR / f"Okupansi_Spektrum_{ts}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    # Sheet 1: Rekap Kanal
    ws = wb.active
    ws.title = "Rekap Kanal"
    ws.views.sheetView[0].showGridLines = True

    # Header Dokumen
    font_title = Font(name="Segoe UI", size=14, bold=True, color=WHITE)
    font_sub = Font(name="Segoe UI", size=9, italic=True, color="D0D5DD")
    fill_hdr = PatternFill("solid", fgColor=DARK_NAVY)

    ws.merge_cells("A1:I1")
    ws["A1"] = title.upper()
    ws["A1"].font = font_title
    ws["A1"].fill = fill_hdr
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:I2")
    sub_text = f"Diekspor pada {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Standar: ITU-R SM.1880"
    if survey:
        sub_text += f" | Preset: {survey.preset_name} ({survey.preset}) | Mulai: {survey.start_at}"
    ws["A2"] = sub_text
    ws["A2"].font = font_sub
    ws["A2"].fill = PatternFill("solid", fgColor=ACCENT_BLUE)
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18

    # Ringkasan KPI di atas tabel
    ws.row_dimensions[4].height = 20
    kpi_items = [
        ("Total Kanal", len(stats)),
        ("Kanal Aktif (>0%)", sum(1 for s in stats.values() if s.occupancy_pct > 0)),
        ("Rata-rata Okupansi", f"{sum(s.occupancy_pct for s in stats.values()) / max(len(stats), 1):.2f}%"),
        ("Daya Puncak", f"{max((s.peak_dbm for s in stats.values()), default=-200.0):.1f} dBm"),
    ]
    col = 1
    for label, val in kpi_items:
        c1 = ws.cell(row=4, column=col, value=label)
        c1.font = Font(name="Segoe UI", size=9, color=TEXT_MUTED)
        c2 = ws.cell(row=5, column=col, value=str(val))
        c2.font = Font(name="Segoe UI", size=12, bold=True, color=TEXT_DARK)
        col += 2

    # Tabel Kolom
    headers = [
        ("No", 6),
        ("Frekuensi (MHz)", 16),
        ("Frekuensi (Hz)", 18),
        ("Label / Nama Kanal", 22),
        ("Okupansi (%)", 14),
        ("Sampel Aktif", 13),
        ("Total Sampel", 13),
        ("Daya Puncak (dBm)", 18),
        ("Rata-rata (dBm)", 16),
    ]
    start_row = 7
    ws.row_dimensions[start_row].height = 24
    for idx, (h, w) in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=idx, value=h)
        cell.font = Font(name="Segoe UI", size=10, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=ACCENT_BLUE)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _border_all()
        ws.column_dimensions[get_column_letter(idx)].width = w

    # Isi Data
    row_idx = start_row + 1
    sorted_stats = sorted(stats.values(), key=lambda s: s.freq_hz)
    bdr = _border_all()

    for num, st in enumerate(sorted_stats, start=1):
        ws.row_dimensions[row_idx].height = 18
        zebra = PatternFill("solid", fgColor=LIGHT_GRAY if num % 2 == 0 else WHITE)

        # Warna okupansi
        occ_fill = zebra
        if st.occupancy_pct >= 50.0:
            occ_fill = PatternFill("solid", fgColor=FILL_HIGH)
        elif st.occupancy_pct >= 20.0:
            occ_fill = PatternFill("solid", fgColor=FILL_MED)
        elif st.occupancy_pct >= 5.0:
            occ_fill = PatternFill("solid", fgColor=FILL_LOW)
        elif st.occupancy_pct > 0.0:
            occ_fill = PatternFill("solid", fgColor=FILL_CLEAR)

        cells = [
            (num, Alignment(horizontal="center"), zebra, "0"),
            (st.freq_mhz, Alignment(horizontal="right"), zebra, "0.0000"),
            (int(st.freq_hz), Alignment(horizontal="right"), zebra, "#,##0"),
            (st.label or "-", Alignment(horizontal="left"), zebra, None),
            (st.occupancy_pct / 100.0, Alignment(horizontal="right"), occ_fill, "0.0%"),
            (st.n_above, Alignment(horizontal="right"), zebra, "#,##0"),
            (st.n_total, Alignment(horizontal="right"), zebra, "#,##0"),
            (round(st.peak_dbm, 1), Alignment(horizontal="right"), zebra, "0.0"),
            (round(st.mean_dbm, 1), Alignment(horizontal="right"), zebra, "0.0"),
        ]

        for col_i, (val, align, fill, num_fmt) in enumerate(cells, start=1):
            c = ws.cell(row=row_idx, column=col_i, value=val)
            c.font = Font(name="Segoe UI", size=9, color=TEXT_DARK)
            c.alignment = align
            c.fill = fill
            c.border = bdr
            if num_fmt:
                c.number_format = num_fmt

        row_idx += 1

    wb.save(str(out_path))
    return out_path


def export_csv(stats: Dict[str, ChannelStats], out_path: Optional[Path] = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_path is None:
        out_path = REPORTS_DIR / f"Okupansi_Spektrum_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sorted_stats = sorted(stats.values(), key=lambda s: s.freq_hz)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "no", "freq_mhz", "freq_hz", "label",
            "occupancy_pct", "samples_above", "samples_total",
            "peak_dbm", "mean_dbm",
        ])
        for num, st in enumerate(sorted_stats, start=1):
            writer.writerow([
                num,
                f"{st.freq_mhz:.4f}",
                int(st.freq_hz),
                st.label or "",
                f"{st.occupancy_pct:.2f}",
                st.n_above,
                st.n_total,
                f"{st.peak_dbm:.2f}",
                f"{st.mean_dbm:.2f}",
            ])
    return out_path
