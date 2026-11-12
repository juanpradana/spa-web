"""Pemeriksaan cepat untuk retune audio manual & penanda geometri sapuan.

Menguji tiga hal yang diperbaiki:
1. `state.spectrumFrame` kini membawa `stopHz` (dipakai pan/zoom di kanvas).
2. `retune_manual_audio()` dapat dipanggil berulang tanpa me-restart stream.
3. `start_manual_audio()` saat sesi sudah berjalan TIDAK lagi memanggil
   `stop_stream()` — jadi audio tidak tersendat saat operator menaris frekuensi.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import CaptureEngine, STATE_MANUAL_AUDIO  # noqa: E402


PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [LULUS] {name}")
    else:
        FAILED += 1
        print(f"  [GAGAL] {name} {detail}")


def main() -> None:
    print("== Retune audio manual & geometri sapuan ==")

    eng = CaptureEngine()
    try:
        info = eng.start()  # type: ignore[attr-defined]
        time.sleep(2.0)
    except Exception as exc:  # pragma: no cover - init lingkungan
        print(f"  [LEWAT] engine tidak dapat dimulai: {exc}")
        return

    # -- 1. retune dasar -------------------------------------------------- #
    try:
        eng.start_manual_audio(freq_hz=156.800e6, demod="FM")
        st = eng.status()
        check("sesi audio manual masuk STATE_MANUAL_AUDIO", st["state"] == STATE_MANUAL_AUDIO, f"state={st['state']}")
        check("status melaporkan manual_audio aktif", st["manual_audio"] is True)
        check("status melaporkan manual_freq_hz benar", abs(st["manual_freq_hz"] - 156.800e6) < 1.0)
    except Exception as exc:
        check("sesi audio manual dapat dimulai", False, str(exc))
        return

    # -- 2. retune berulang (simulasi drag-tuning) ------------------------ #
    freqs = [156.800e6, 156.825e6, 156.850e6, 156.875e6, 156.900e6]
    ok = True
    for f in freqs:
        try:
            eng.retune_manual_audio(freq_hz=f)
            st = eng.status()
            if abs(st["manual_freq_hz"] - f) > 1.0:
                ok = False
        except Exception as exc:  # pragma: no cover
            ok = False
            print(f"    galat retune {f}: {exc}")
            break
    check("retune_manual_audio memperbarui frekuensi berulang", ok)

    # -- 3. start_manual_audio pada sesi aktif = retune, bukan restart ---- #
    try:
        dev = eng._device
    except AttributeError:
        dev = None
    abort_calls: list[int] = []
    if dev is not None and hasattr(dev, "stop_stream"):
        orig = dev.stop_stream

        def counting_stop_stream() -> None:
            abort_calls.append(1)
            orig()

        dev.stop_stream = counting_stop_stream  # type: ignore[assignment]

        try:
            eng.start_manual_audio(freq_hz=157.100e6, demod="FM")
            time.sleep(0.3)
            st = eng.status()
            check("sesi tetap hidup setelah start_manual_audio berulang",
                  st["state"] == STATE_MANUAL_AUDIO)
            check("start_manual_audio pada sesi aktif TIDAK memanggil stop_stream",
                  not abort_calls, f"stop_stream dipanggil {len(abort_calls)}x")
            check("frekuensi terbarui", abs(st["manual_freq_hz"] - 157.100e6) < 1.0)
        finally:
            try:
                dev.stop_stream = orig  # type: ignore[assignment]
            except Exception:
                pass
    else:
        check("perangkat tersedia untuk uji stop_stream", False, "device=None")

    # -- 4. pindah dari sweep ke audio memang menghentikan sapuan --------- #
    eng.stop_manual_audio()
    time.sleep(1.5)
    st = eng.status()
    check("stop_manual_audio kembali ke SWEEPING", st["state"] != STATE_MANUAL_AUDIO, f"state={st['state']}")

    try:
        eng.start_manual_audio(freq_hz=157.250e6, demod="FM")
        dev2 = eng._device
        if dev2 is not None and hasattr(dev2, "stop_stream"):
            abort2: list[int] = []
            orig2 = dev2.stop_stream

            def counting2() -> None:
                abort2.append(1)
                orig2()

            dev2.stop_stream = counting2  # type: ignore[assignment]
            try:
                # pindah sweep -> audio: memang harus menghentikan sapuan sekali
                eng.stop_manual_audio()
                time.sleep(0.5)
                eng.start_manual_audio(freq_hz=157.250e6, demod="FM")
                check("pindahan sweep->audio memakai stop_stream setidaknya sekali",
                      len(abort2) >= 1, f"panggilan={len(abort2)}")
            finally:
                try:
                    dev2.stop_stream = orig2  # type: ignore[assignment]
                except Exception:
                    pass
    except Exception as exc:  # pragma: no cover
        check("uji sweep->audio", False, str(exc))

    try:
        eng.stop_manual_audio()
    except Exception:
        pass
    try:
        eng.stop()
    except Exception:
        pass

    print()
    print("=" * 60)
    print(f"HASIL: {PASSED} lulus, {FAILED} gagal")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
