"""
Tes integritas statik & scope untuk static/app.js.

Mencegah regresi scope leak seperti:
- _syncListenInputs dideklarasikan di dalam closure fungsi tertentu tetapi dipanggil dari luar
- _demodBandwidthHz tidak terjangkau dari drawCarrierLine
- Fungsi window.* terduplikasi
- Panggilan getElementById pada ID yang tidak ada di index.html
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"


def test_top_level_helpers_accessible():
    """Memastikan helper penting berada di scope modul (kolom 0), bukan di dalam initSpectrumCanvas."""
    text = APP_JS.read_text(encoding="utf-8")

    # Temukan batas fungsi initSpectrumCanvas
    m_start = re.search(r"^function initSpectrumCanvas\(\)\s*\{", text, re.MULTILINE)
    assert m_start is not None, "initSpectrumCanvas() harus ada"
    start_pos = m_start.start()

    # Cari closing brace fungsi initSpectrumCanvas (tingkat indentasi nol)
    m_end = re.search(r"^\}\s*^function updateMarkerReadout", text[start_pos:], re.MULTILINE)
    assert m_end is not None, "updateMarkerReadout harus berada setelah initSpectrumCanvas"
    end_pos = start_pos + m_end.start() + 1
    init_spectrum_body = text[start_pos:end_pos]

    # _syncListenInputs TIDAK boleh dideklarasikan di dalam initSpectrumCanvas
    assert "function _syncListenInputs" not in init_spectrum_body, (
        "_syncListenInputs tidak boleh dideklarasikan di dalam initSpectrumCanvas"
    )

    # _demodBandwidthHz TIDAK boleh dideklarasikan di dalam initSpectrumCanvas
    assert "function _demodBandwidthHz" not in init_spectrum_body, (
        "_demodBandwidthHz tidak boleh dideklarasikan di dalam initSpectrumCanvas"
    )

    # Keduanya HARUS dideklarasikan di tingkat modul (kolom 0)
    assert re.search(r"^function _syncListenInputs\(", text, re.MULTILINE), (
        "_syncListenInputs harus dideklarasikan di scope modul (kolom 0)"
    )
    assert re.search(r"^function _demodBandwidthHz\(", text, re.MULTILINE), (
        "_demodBandwidthHz harus dideklarasikan di scope modul (kolom 0)"
    )


def test_no_duplicate_window_exports():
    """Memastikan tidak ada duplikasi assignment ke window.* seperti window.tuneAndListen."""
    text = APP_JS.read_text(encoding="utf-8")
    matches = re.findall(r"window\.([a-zA-Z0-9_$]+)\s*=", text)
    counts = {}
    for name in matches:
        counts[name] = counts.get(name, 0) + 1

    duplicates = {name: c for name, c in counts.items() if c > 1}
    assert not duplicates, f"Ditemukan duplikasi assignment window.*: {duplicates}"


def test_demod_bandwidth_returns_correct_values():
    """Memverifikasi lebar pita demodulasi matematis."""
    text = APP_JS.read_text(encoding="utf-8")
    assert "case \"FM\": return 150_000;" in text
    assert "case \"AM\": return 8_000;" in text
    assert "case \"USB\":" in text
    assert "case \"LSB\": return 3_000;" in text
    assert "case \"CW\": return 1_000;" in text


def test_tune_and_listen_syncs_both_inputs():
    """Memastikan window.tuneAndListen menyinkronkan kedua input (drawer & monitor bar)."""
    text = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"window\.tuneAndListen\s*=\s*\([^)]*\)\s*=>\s*\{([^}]+)\}", text)
    assert m is not None, "window.tuneAndListen harus terdefinisi"
    body = m.group(1)
    assert "_syncListenInputs" in body, "tuneAndListen harus memanggil _syncListenInputs"


def test_no_dead_draw_scheduled():
    """Memastikan variabel mati drawScheduled tidak ada."""
    text = APP_JS.read_text(encoding="utf-8")
    assert "drawScheduled" not in text


def _brace_depth_positions(text):
    """Kembalikan dict {nama_fungsi_init: (posisi_awal_body, posisi_akhir_body)}.

    Melacak kedalaman brace dari kolom 0 agar tahu persis di mana sebuah
    fungsi tingkat-modul berakhir — inilah yang membuat deteksi scope leak
    di bawah akurat.
    """
    import re as _re

    result = {}
    # Semua fungsi tingkat-modul (indentasi nol)
    for m in _re.finditer(r"^function ([A-Za-z0-9_$]+)\([^)]*\)\s*\{", text, _re.MULTILINE):
        name = m.group(1)
        # Telusuri brace dari akhir signature sampai kembali ke kedalaman 0
        i = text.index("{", m.start())
        depth = 0
        j = i
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        result[name] = (i, j)
    return result


def test_no_nested_identifier_escapes_its_enclosing_function():
    """Deteksi generik: identifier yang dideklarasikan di dalam fungsi A
    tidak boleh direferensikan dari luar fungsi A.

    Ini menangkap SELURUH kelas bug scope-leak, bukan hanya dua kasus yang
    sudah diketahui (_syncListenInputs dan _demodBandwidthHz).
    """
    text = APP_JS.read_text(encoding="utf-8")
    decl_re = re.compile(r"^\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(", )
    names = [
        "initSpectrumCanvas",
        "initWaterfallCanvas",
        "initControls",
        "initNavigation",
    ]
    funcs = _brace_depth_positions(text)

    leaks = []
    for outer in names:
        if outer not in funcs:
            continue
        os_, oe = funcs[outer]
        body = text[os_:oe]

        for dm in decl_re.finditer(body):
            child = dm.group(1)
            # Abaikan nama yang juga dideklarasikan di tingkat-modul
            if re.search(rf"^function {re.escape(child)}\(", text, re.MULTILINE):
                continue

            # Semua referensi child DI LUAR fungsi outer
            for rm in re.finditer(rf"\b{re.escape(child)}\b", text):
                pos = rm.start()
                if os_ <= pos < oe:
                    continue          # dalam scope, sah
                if dm.start() <= pos < dm.end():
                    continue          # itu sendiri deklarasinya
                line = text.count("\n", 0, pos) + 1
                leaks.append(f"{child} (dideklarasikan di dalam {outer}) dipakai di baris {line}")

    assert not leaks, (
        "Identifier nested bocor keluar dari fungsi pembungkusnya "
        "(kelas bug ReferenceError):\n  " + "\n  ".join(leaks)
    )


def test_no_undefined_global_identifier_references():
    """Deteksi 'X is not defined': setiap identifier yang dipanggil sebagai
    fungsi di tingkat-modul harus terdefinisi di tingkat-modul
    (function/const/let/var/window.*/class).
    """
    text = APP_JS.read_text(encoding="utf-8")

    declared = set(re.findall(r"^function\s+([A-Za-z0-9_$]+)\(", text, re.MULTILINE))
    declared |= set(re.findall(r"^const\s+([A-Za-z0-9_$]+)\s*=", text, re.MULTILINE))
    declared |= set(re.findall(r"^let\s+([A-Za-z0-9_$]+)\s*=", text, re.MULTILINE))
    declared |= set(re.findall(r"^class\s+([A-Za-z0-9_$]+)", text, re.MULTILINE))
    declared |= set(re.findall(r"window\.([A-Za-z0-9_$]+)\s*=", text))
    # Built-in & API browser
    builtins = {
        "Math", "JSON", "Date", "Number", "String", "Boolean", "Array",
        "Object", "Map", "Set", "Promise", "Error", "TypeError", "RangeError",
        "SyntaxError", "ReferenceError", "isNaN", "parseFloat", "parseInt",
        "encodeURIComponent", "decodeURIComponent", "fetch", "Uint8ClampedArray",
        "Float32Array", "Int32Array", "Uint8Array", "DataView", "document",
        "window", "localStorage", "location", "performance", "console",
        "setTimeout", "clearTimeout", "setInterval", "clearInterval",
        "requestAnimationFrame", "confirm", "alert", "WebSocket", "AudioContext",
        "FormData", "Intl", "ArrayBuffer", "structuredClone",
    }

    leaks = []
    # Hanya panggilan di MULAI baris dalam skop tingkat-modul (indentasi 0),
    # yaitu pemanggil top-level seperti startListening().
    for m in re.finditer(r"^(\s*)([A-Za-z0-9_$]+)\s*\(", text, re.MULTILINE):
        indent = m.group(1)
        name = m.group(2)
        if indent:            # pemanggilan bersarang, di luar lingkup tes ini
            continue
        if name in declared or name in builtins:
            continue
        if name in ("if", "for", "while", "switch", "catch", "return", "function", "async"):
            continue
        line = text.count("\n", 0, m.start()) + 1
        leaks.append(f"baris {line}: {name}() dipanggil di tingkat-modul tapi tak terdefinisi")

    assert not leaks, (
        "Referensi fungsi tingkat-modul yang tidak terdefinisi:\n  " + "\n  ".join(leaks)
    )


if __name__ == "__main__":
    test_top_level_helpers_accessible()
    test_no_duplicate_window_exports()
    test_demod_bandwidth_returns_correct_values()
    test_tune_and_listen_syncs_both_inputs()
    test_no_dead_draw_scheduled()
    test_no_nested_identifier_escapes_its_enclosing_function()
    test_no_undefined_global_identifier_references()
    print("Semua tes integritas scope JS [LULUS]!")
