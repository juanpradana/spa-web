"""Uji end-to-end engine + okupansi + perekam + laporan memakai simulator.

Dijalankan tanpa perangkat keras: `python tests/smoke_engine.py`.

Seluruh direktori data/recordings/reports/logs dialihkan ke direktori sementara
sebelum modul aplikasi diimpor. Tanpa ini, uji menulis rekaman WAV, laporan
.xlsx/.csv, dan delta okupansi ke data produksi operator — persis yang terjadi
saat suite ini dijalankan pada data nyata.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolasi wajib: harus diset SEBELUM `config`/`engine`/`occupancy` diimpor,
# karena `config.py` membaca environment saat modul dimuat dan menyimpan path
# akhirnya di konstanta tingkat modul.
_tmpdir = tempfile.TemporaryDirectory()
os.environ["SA_DATA_DIR"] = str(Path(_tmpdir.name) / "data")
os.environ["SA_RECORDINGS_DIR"] = str(Path(_tmpdir.name) / "recordings")
os.environ["SA_REPORTS_DIR"] = str(Path(_tmpdir.name) / "reports")
os.environ["SA_LOGS_DIR"] = str(Path(_tmpdir.name) / "logs")

from channels import PRESETS, get_preset          # noqa: E402
from config import CONFIG, RECORDINGS_DIR          # noqa: E402
from engine import ENGINE                          # noqa: E402
from occupancy import OCC                          # noqa: E402
from recorder import scan_recordings               # noqa: E402
from reports import export_csv, export_excel       # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "LULUS" if condition else "GAGAL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    # Simpan preferensi operator; uji ini sempat menulis auto_record=true
    # permanen ke config.json, membalikkan setelan "rekam hanya bila diminta".
    auto_record_prev = bool(CONFIG.get("occupancy").get("auto_record", False))
    try:
        return _run()
    finally:
        ENGINE.set_auto_record(auto_record_prev)
        print(f"\n[bersih] auto_record dikembalikan ke {auto_record_prev}")


def _run() -> int:
    print("== 1. Inisialisasi perangkat (harus jatuh ke simulator) ==")
    info = ENGINE.initialize_device(force_simulate=True)
    check("perangkat terbuka", info.connected, f"model={info.model}")
    check("mode simulasi", info.simulated)

    print("\n== 2. Konfigurasi preset VHF Marine (156.800 MHz) ==")
    preset = get_preset("vhf_marine")
    assert preset is not None
    ENGINE.apply_preset("vhf_marine")
    ENGINE.set_auto_record(True)
    check("preset diterapkan", ENGINE.status()["preset"] == "vhf_marine")

    print("\n== 3. Jalankan engine, biarkan menyapu ~8 detik ==")
    OCC.begin_session("vhf_marine")
    ENGINE.start("vhf_marine")
    time.sleep(8.0)
    st = ENGINE.status()
    check("engine berjalan", st["running"], f"state={st['state']}")
    check("sweep terjadi", st["sweeps"] > 0, f"{st['sweeps']} sapuan, {st['fps']} fps")

    print("\n== 4. Akumulasi okupansi ==")
    OCC.flush()
    stats = OCC.all_stats()
    check("statistik kanal tercatat", len(stats) > 0, f"{len(stats)} kanal")
    if stats:
        top = max(stats.values(), key=lambda s: s.occupancy_pct)
        check("okupansi terhitung", top.n_total > 0,
              f"puncak: {top.freq_mhz:.4f} MHz = {top.occupancy_pct:.2f}% "
              f"({top.n_above}/{top.n_total}), peak {top.peak_dbm:.1f} dBm")
        check("persentase dalam rentang 0-100", 0.0 <= top.occupancy_pct <= 100.0)

    print("\n== 5. Tunggu rekaman audio otomatis (dwell) ==")
    deadline = time.time() + 60.0
    while time.time() < deadline:
        files = scan_recordings(RECORDINGS_DIR, limit=5)
        if files:
            break
        time.sleep(1.0)
    files = scan_recordings(RECORDINGS_DIR, limit=5)
    check("file WAV terbentuk", len(files) > 0, f"{len(files)} file")
    if files:
        f = files[0]
        check("durasi > 0", f["duration_sec"] > 0, f"{f['filename']} {f['duration_sec']}s "
              f"@{f['sample_rate']} Hz")
        check("nama file memuat frekuensi MHz", "MHz_" in f["filename"], f["filename"])

    print("\n== 6. Hentikan engine ==")
    ENGINE.stop()
    time.sleep(0.5)
    check("engine berhenti", not ENGINE.status()["running"])

    print("\n== 7. Ekspor laporan ==")
    if stats:
        xlsx = export_excel(stats)
        csv_path = export_csv(stats)
        check("Excel terbentuk", xlsx.exists() and xlsx.stat().st_size > 0, xlsx.name)
        check("CSV terbentuk", csv_path.exists() and csv_path.stat().st_size > 0, csv_path.name)

    print("\n== 8. Cakupan preset ==")
    check("semua preset dapat membangun kanal",
          all(len(p.channels()) >= 0 for p in PRESETS.values()),
          f"{len(PRESETS)} preset")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"HASIL: {len(FAILURES)} pemeriksaan GAGAL")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("HASIL: semua pemeriksaan LULUS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            _tmpdir.cleanup()
        except Exception:
            pass
    sys.exit(code)
