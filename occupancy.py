"""Pengukuran & statistik okupansi kanal (ITU-R SM.1880 / SM.1537).

Untuk setiap kanal:
  occupancy_pct = n_above / n_total * 100

Tersimpan ke SQLite (`data/monitoring.db`) dan dapat diekspor ke Excel/CSV.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import DATA_DIR

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class ChannelStats:
    key: str                # "156800000" atau label unik
    freq_hz: float
    freq_mhz: float
    label: str = ""
    n_total: int = 0        # jumlah kali kanal terpantau
    n_above: int = 0        # jumlah sampel di atas threshold
    occupancy_pct: float = 0.0
    peak_dbm: float = -200.0
    mean_dbm: float = -200.0
    events: int = 0         # jumlah transmisi terdeteksi
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    last_above_ts: Optional[str] = None
    preset: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "freq_hz": self.freq_hz,
            "freq_mhz": self.freq_mhz,
            "label": self.label,
            "occupancy_pct": round(self.occupancy_pct, 3),
            "peak_dbm": round(self.peak_dbm, 2),
            "mean_dbm": round(self.mean_dbm, 2),
            "n_total": self.n_total,
            # n_above ikut dikirim: tabel okupansi menampilkan jumlah sampel di
            # atas threshold per kanal, dan tanpa field ini pembacaannya kosong.
            "n_above": self.n_above,
            "events": self.events,
        }


def channel_key(freq_hz: float) -> str:
    """Kunci kanal yang stabil terhadap pembulatan float (1 Hz)."""
    return f"{int(round(freq_hz))}"


@dataclass
class _LiveAcc:
    """Akumulator in-memory untuk satu kanal.

    Mesin sweep memanggil `record_channel_sample` ribuan kali per detik pada
    preset broadband (4000 kanal). Menulis satu baris SQLite per panggilan akan
    membuat I/O disk menjadi bottleneck, jadi sampel diakumulasi di memori dan
    dibersihkan (flush) secara berkala sebagai delta.
    """
    freq_hz: float
    label: str = ""
    n_total: int = 0
    n_above: int = 0
    peak_dbm: float = -200.0
    sum_dbm: float = 0.0
    events: int = 0
    was_above: bool = False
    last_above_ts: Optional[str] = None
    dirty: bool = False


@dataclass
class SurveyRun:
    id: int
    preset: str
    preset_name: str
    center_hz: float
    start_at: str
    end_at: Optional[str]
    threshold_mode: str
    threshold_dbm: float
    channels_observed: int
    occupancy_pct: float
    max_channel_power_dbm: float
    notes: str = ""


class OccupancyStore:
    def __init__(self, path: Path = DATA_DIR / "monitoring.db") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._live: Dict[str, _LiveAcc] = {}
        self._preset_tag: str = ""
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_stats (
                key TEXT PRIMARY KEY,
                freq_hz REAL NOT NULL,
                freq_mhz REAL NOT NULL,
                label TEXT,
                n_total INTEGER NOT NULL DEFAULT 0,
                n_above INTEGER NOT NULL DEFAULT 0,
                peak_dbm REAL NOT NULL DEFAULT -200.0,
                sum_dbm REAL NOT NULL DEFAULT 0.0,
                events INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                ended_at TEXT,
                last_above_ts TEXT,
                preset TEXT
            );
            CREATE TABLE IF NOT EXISTS surveys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset TEXT NOT NULL,
                preset_name TEXT,
                center_hz REAL NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT,
                threshold_mode TEXT NOT NULL,
                threshold_dbm REAL NOT NULL,
                channels_observed INTEGER NOT NULL,
                occupancy_pct REAL NOT NULL,
                max_channel_power_dbm REAL NOT NULL,
                notes TEXT
            );
        """)
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- kanal ----------------------------------------------------------- #
    def create_channel(self, key: str, freq_hz: float, label: str = "", preset: str = "") -> Optional[ChannelStats]:
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO channel_stats (key, freq_hz, freq_mhz, label, n_total, n_above,
                                           peak_dbm, sum_dbm, events, started_at, preset)
                       VALUES (?, ?, ?, ?, 0, 0, -200.0, 0.0, 0, ?, ?)""",
                    (key, freq_hz, freq_hz / 1e6, label, now, preset),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
            return self.get_channel(key)

    def get_channel(self, key: str) -> Optional[ChannelStats]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM channel_stats WHERE key = ?", (key,)).fetchone()
        conn.close()
        return self._row_to_stats(row) if row else None

    def upsert(self, stats: ChannelStats) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO channel_stats (key, freq_hz, freq_mhz, label, n_total, n_above,
                                               peak_dbm, sum_dbm, events, started_at, last_above_ts, preset)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       n_total = excluded.n_total,
                       n_above = excluded.n_above,
                       peak_dbm = excluded.peak_dbm,
                       sum_dbm = excluded.sum_dbm,
                       events = excluded.events,
                       last_above_ts = excluded.last_above_ts""",
                (stats.key, stats.freq_hz, stats.freq_mhz, stats.label, stats.n_total, stats.n_above,
                 stats.peak_dbm, stats.mean_dbm * stats.n_total, stats.events,
                 stats.started_at or datetime.now().isoformat(timespec="seconds"),
                 stats.last_above_ts, stats.preset),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- akumulasi sampel langsung (dari engine) ---------------------------- #
    def begin_session(self, preset: str) -> None:
        """Tandai preset aktif dan buang akumulator kanal dari preset sebelumnya."""
        with self._lock:
            if self._preset_tag != preset:
                self.flush()
                self._live.clear()
                self._preset_tag = preset

    def record_channel_sample(self, freq_hz: float, dbm: float, is_above: bool, label: str = "") -> None:
        """Tambahkan satu pengamatan kanal ke akumulator in-memory."""
        key = channel_key(freq_hz)
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            acc = self._live.get(key)
            if acc is None:
                acc = _LiveAcc(freq_hz=float(freq_hz), label=label)
                self._live[key] = acc
            elif label and acc.label != label:
                acc.label = label

            acc.n_total += 1
            acc.sum_dbm += float(dbm)
            if dbm > acc.peak_dbm:
                acc.peak_dbm = float(dbm)
            if is_above:
                acc.n_above += 1
                acc.last_above_ts = now
                # Tepi naik (rise edge) = satu peristiwa transmisi baru.
                if not acc.was_above:
                    acc.events += 1
            acc.was_above = bool(is_above)
            acc.dirty = True

    def flush(self) -> int:
        """Tulis delta akumulator ke SQLite. Kembalikan jumlah kanal yang ditulis."""
        with self._lock:
            pending = [(k, a) for k, a in self._live.items() if a.dirty]
            for _, acc in pending:
                acc.dirty = False
            tag = self._preset_tag

        if not pending:
            return 0

        conn = self._connect()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            for key, acc in pending:
                conn.execute(
                    """INSERT INTO channel_stats (key, freq_hz, freq_mhz, label, n_total, n_above,
                                                   peak_dbm, sum_dbm, events, started_at, ended_at,
                                                   last_above_ts, preset)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           n_total = channel_stats.n_total + excluded.n_total,
                           n_above = channel_stats.n_above + excluded.n_above,
                           peak_dbm = MAX(channel_stats.peak_dbm, excluded.peak_dbm),
                           sum_dbm = channel_stats.sum_dbm + excluded.sum_dbm,
                           events = channel_stats.events + excluded.events,
                           ended_at = excluded.ended_at,
                           last_above_ts = COALESCE(excluded.last_above_ts, channel_stats.last_above_ts),
                           label = CASE WHEN excluded.label != '' THEN excluded.label
                                        ELSE channel_stats.label END""",
                    (key, acc.freq_hz, acc.freq_hz / 1e6, acc.label, acc.n_total, acc.n_above,
                     acc.peak_dbm, acc.sum_dbm, acc.events, now, now,
                     acc.last_above_ts, tag),
                )
            conn.commit()
            # Delta sudah dipindahkan ke DB: kosongkan bagian terakumulasi supaya
            # tidak dihitung dua kali pada flush berikutnya. `peak_dbm` ikut
            # direset karena MAX() di SQL sudah menyimpan nilai tertinggi.
            with self._lock:
                for key, acc in pending:
                    if self._live.get(key) is acc:
                        acc.n_total = 0
                        acc.n_above = 0
                        acc.sum_dbm = 0.0
                        acc.events = 0
                        acc.peak_dbm = -200.0
            return len(pending)
        except Exception:
            conn.rollback()
            # Kembalikan flag agar delta tidak hilang saat flush berikutnya.
            with self._lock:
                for _, acc in pending:
                    acc.dirty = True
            raise
        finally:
            conn.close()

    def live_stat(self, freq_hz: float) -> Optional[ChannelStats]:
        """Statistik gabungan (DB + akumulator yang belum di-flush) untuk satu kanal."""
        key = channel_key(freq_hz)
        with self._lock:
            acc = self._live.get(key)
            snapshot = None
            if acc is not None:
                snapshot = (acc.n_total, acc.n_above, acc.sum_dbm, acc.peak_dbm,
                            acc.events, acc.label, acc.last_above_ts)
        stored = self.get_channel(key)
        if snapshot is None:
            return stored

        n_total, n_above, sum_dbm, peak_dbm, events, label, last_above = snapshot
        base_total = stored.n_total if stored else 0
        base_above = stored.n_above if stored else 0
        base_sum = (stored.mean_dbm * stored.n_total) if stored and stored.n_total else 0.0
        base_peak = stored.peak_dbm if stored else -200.0
        base_events = stored.events if stored else 0

        total = base_total + n_total
        above = base_above + n_above
        return ChannelStats(
            key=key,
            freq_hz=float(freq_hz),
            freq_mhz=float(freq_hz) / 1e6,
            label=label or (stored.label if stored else ""),
            n_total=total,
            n_above=above,
            occupancy_pct=(above / total * 100.0) if total else 0.0,
            peak_dbm=max(base_peak, peak_dbm),
            mean_dbm=((base_sum + sum_dbm) / total) if total else -200.0,
            events=base_events + events,
            started_at=stored.started_at if stored else None,
            ended_at=stored.ended_at if stored else None,
            last_above_ts=last_above or (stored.last_above_ts if stored else None),
        )

    def live_stats(self) -> Dict[str, ChannelStats]:
        """`all_stats()` + akumulator yang belum di-flush, untuk tampilan realtime."""
        with self._lock:
            keys = list(self._live.keys())
        merged = self.all_stats()
        for key in keys:
            with self._lock:
                acc = self._live.get(key)
            if acc is None:
                continue
            live = self.live_stat(acc.freq_hz)
            if live is not None:
                merged[key] = live
        return merged

    # --- survey run (snapshot) --------------------------------------------- #
    def save_survey(self, s: SurveyRun) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO surveys (preset, preset_name, center_hz, start_at, end_at,
                                        threshold_mode, threshold_dbm, channels_observed,
                                        occupancy_pct, max_channel_power_dbm, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s.preset, s.preset_name, s.center_hz, s.start_at, s.end_at,
                 s.threshold_mode, s.threshold_dbm, s.channels_observed, s.occupancy_pct,
                 s.max_channel_power_dbm, s.notes),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_surveys(self, limit: int = 100) -> List[SurveyRun]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM surveys ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_survey(r) for r in rows]

    # --- reset & util ------------------------------------------------------ #
    def reset_channel(self, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE channel_stats SET n_total=0, n_above=0, peak_dbm=-200.0, "
                "sum_dbm=0.0, events=0, started_at=NULL, ended_at=NULL, last_above_ts=NULL WHERE key=?",
                (key,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_all(self) -> None:
        """Hapus seluruh baris channel_stats + surveys, dan buang akumulator live.

        Tanpa `_live.clear()`, delta yang belum di-flush akan menulis ulang baris
        yang baru dihapus pada flush berikutnya, sehingga "hapus semua" hanya
        bersifat sementara selama engine masih berjalan.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM channel_stats")
            conn.execute("DELETE FROM surveys")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        with self._lock:
            self._live.clear()

    def delete_channels(self, keys: List[str]) -> int:
        """Hapus baris channel_stats untuk `keys` dan buang akumulator live-nya.

        Kembalikan jumlah baris yang benar-benar terhapus. Dipakai hapus
        multi-select dari tabel okupansi supaya operator bisa membuang kanal
        tertentu tanpa merusak sisa riwayat survei.
        """
        clean = [str(k) for k in (keys or []) if str(k)]
        if not clean:
            return 0
        deleted = 0
        conn = self._connect()
        try:
            placeholders = ",".join("?" * len(clean))
            cur = conn.execute(f"DELETE FROM channel_stats WHERE key IN ({placeholders})", clean)
            deleted = int(cur.rowcount or 0)
            if not deleted:
                conn.rollback()
                return 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        with self._lock:
            for k in clean:
                self._live.pop(k, None)
        return deleted

    def all_stats(self) -> Dict[str, ChannelStats]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM channel_stats ORDER BY freq_hz").fetchall()
        finally:
            conn.close()
        return {r["key"]: self._row_to_stats(r) for r in rows}

    # --- helpers ----------------------------------------------------------- #
    def _row_to_stats(self, row) -> Optional[ChannelStats]:
        if row is None:
            return None
        n_total = row["n_total"] or 0
        n_above = row["n_above"] or 0
        sum_dbm = row["sum_dbm"] or 0.0
        peak_dbm = row["peak_dbm"] or -200.0
        return ChannelStats(
            key=row["key"],
            freq_hz=float(row["freq_hz"]),
            freq_mhz=float(row["freq_mhz"]),
            label=row["label"] or "",
            n_total=n_total,
            n_above=n_above,
            occupancy_pct=(n_above / n_total * 100) if n_total else 0.0,
            peak_dbm=peak_dbm,
            mean_dbm=(sum_dbm / n_total) if n_total else -200.0,
            events=row["events"] or 0,
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            last_above_ts=row["last_above_ts"],
            preset=row["preset"] or "",
        )

    def _row_to_survey(self, row) -> SurveyRun:
        return SurveyRun(
            id=int(row["id"]),
            preset=row["preset"],
            preset_name=row["preset_name"] or "",
            center_hz=float(row["center_hz"]),
            start_at=row["start_at"],
            end_at=row["end_at"],
            threshold_mode=row["threshold_mode"],
            threshold_dbm=float(row["threshold_dbm"]),
            channels_observed=int(row["channels_observed"]),
            occupancy_pct=float(row["occupancy_pct"]),
            max_channel_power_dbm=float(row["max_channel_power_dbm"]),
            notes=row["notes"] or "",
        )


OCC = OccupancyStore()
