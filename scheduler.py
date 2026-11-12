"""Penjadwal survei okupansi (asyncio background task, tanpa dependensi eksternal).

Model sederhana: satu survei berjalan pada satu waktu (perangkat hanya satu).
Task loop bangun setiap `TICK_SEC`, memeriksa jadwal yang jatuh tempo, menerapkan
preset + auto-record ke `CaptureEngine`, dan menutup survei ketika durasi habis.

Riwayat eksekusi disimpan di `data/schedules.db` sehingga operator dapat melihat
hasil tiap kampanye beserta snapshot okupansi-nya.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from channels import PRESETS, get_preset
from config import DATA_DIR
from occupancy import OCC, SurveyRun
from security import AUDIT

TICK_SEC = 5.0

REPEAT_NONE = "none"
REPEAT_DAILY = "daily"
REPEAT_WEEKLY = "weekly"
REPEATS = (REPEAT_NONE, REPEAT_DAILY, REPEAT_WEEKLY)


@dataclass
class Schedule:
    id: str
    name: str
    preset: str
    start_at: str                 # ISO lokal "YYYY-MM-DDTHH:MM:SS"
    duration_min: float = 60.0
    repeat: str = REPEAT_NONE
    enabled: bool = True
    auto_record: bool = True
    threshold_mode: str = "adaptive"
    fixed_threshold_dbm: float = -85.0
    adaptive_offset_db: float = 10.0
    notes: str = ""
    owner: str = ""
    created_at: str = ""
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "preset": self.preset,
            "preset_name": (get_preset(self.preset).name if get_preset(self.preset) else self.preset),
            "start_at": self.start_at,
            "duration_min": self.duration_min,
            "repeat": self.repeat,
            "enabled": self.enabled,
            "auto_record": self.auto_record,
            "threshold_mode": self.threshold_mode,
            "fixed_threshold_dbm": self.fixed_threshold_dbm,
            "adaptive_offset_db": self.adaptive_offset_db,
            "notes": self.notes,
            "owner": self.owner,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
        }


@dataclass
class RunRecord:
    run_id: str
    schedule_id: str
    name: str
    preset: str
    started_at: str
    ended_at: Optional[str] = None
    status: str = "running"       # running | completed | cancelled | error
    channels_observed: int = 0
    occupancy_pct: float = 0.0
    max_channel_power_dbm: float = -200.0
    recordings: int = 0
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "name": self.name,
            "preset": self.preset,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "channels_observed": self.channels_observed,
            "occupancy_pct": round(self.occupancy_pct, 2),
            "max_channel_power_dbm": round(self.max_channel_power_dbm, 1),
            "recordings": self.recordings,
            "notes": self.notes,
        }


class ScheduleStore:
    def __init__(self, path: Path = DATA_DIR / "schedules.db") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    preset TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    duration_min REAL NOT NULL DEFAULT 60,
                    repeat TEXT NOT NULL DEFAULT 'none',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    auto_record INTEGER NOT NULL DEFAULT 1,
                    threshold_mode TEXT NOT NULL DEFAULT 'adaptive',
                    fixed_threshold_dbm REAL NOT NULL DEFAULT -85,
                    adaptive_offset_db REAL NOT NULL DEFAULT 10,
                    notes TEXT,
                    owner TEXT,
                    created_at TEXT,
                    last_run_at TEXT
                );
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    run_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    name TEXT,
                    preset TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    channels_observed INTEGER NOT NULL DEFAULT 0,
                    occupancy_pct REAL NOT NULL DEFAULT 0,
                    max_channel_power_dbm REAL NOT NULL DEFAULT -200,
                    recordings INTEGER NOT NULL DEFAULT 0,
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_started ON schedule_runs(started_at DESC);
            """)
            conn.commit()
        finally:
            conn.close()

    # --- CRUD -------------------------------------------------------------- #
    def create(self, s: Schedule) -> Schedule:
        now = datetime.now().isoformat(timespec="seconds")
        s.created_at = s.created_at or now
        if not s.id:
            s.id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO schedules (id, name, preset, start_at, duration_min, repeat,
                                              enabled, auto_record, threshold_mode, fixed_threshold_dbm,
                                              adaptive_offset_db, notes, owner, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s.id, s.name, s.preset, s.start_at, s.duration_min, s.repeat,
                     int(s.enabled), int(s.auto_record), s.threshold_mode, s.fixed_threshold_dbm,
                     s.adaptive_offset_db, s.notes, s.owner, s.created_at),
                )
                conn.commit()
            finally:
                conn.close()
        return s

    def update(self, s: Schedule) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """UPDATE schedules SET name=?, preset=?, start_at=?, duration_min=?, repeat=?,
                                           enabled=?, auto_record=?, threshold_mode=?,
                                           fixed_threshold_dbm=?, adaptive_offset_db=?, notes=?
                       WHERE id=?""",
                    (s.name, s.preset, s.start_at, s.duration_min, s.repeat, int(s.enabled),
                     int(s.auto_record), s.threshold_mode, s.fixed_threshold_dbm,
                     s.adaptive_offset_db, s.notes, s.id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete(self, schedule_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get(self, schedule_id: str) -> Optional[Schedule]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        finally:
            conn.close()
        return self._row_to_schedule(row) if row else None

    def list_all(self) -> List[Schedule]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM schedules ORDER BY start_at").fetchall()
        finally:
            conn.close()
        return [self._row_to_schedule(r) for r in rows]

    def mark_run(self, schedule_id: str, when: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (when, schedule_id))
                conn.commit()
            finally:
                conn.close()

    # --- riwayat ----------------------------------------------------------- #
    def start_run(self, r: RunRecord) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO schedule_runs (run_id, schedule_id, name, preset, started_at, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (r.run_id, r.schedule_id, r.name, r.preset, r.started_at, r.status),
                )
                conn.commit()
            finally:
                conn.close()

    def finish_run(self, r: RunRecord) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """UPDATE schedule_runs SET ended_at=?, status=?, channels_observed=?,
                                               occupancy_pct=?, max_channel_power_dbm=?,
                                               recordings=?, notes=?
                       WHERE run_id=?""",
                    (r.ended_at, r.status, r.channels_observed, r.occupancy_pct,
                     r.max_channel_power_dbm, r.recordings, r.notes, r.run_id),
                )
                conn.commit()
            finally:
                conn.close()

    def list_runs(self, limit: int = 50) -> List[RunRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM schedule_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_run(r) for r in rows]

    def active_run(self) -> Optional[RunRecord]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM schedule_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_run(row) if row else None

    # --- helpers ----------------------------------------------------------- #
    @staticmethod
    def _row_to_schedule(row) -> Schedule:
        return Schedule(
            id=row["id"],
            name=row["name"],
            preset=row["preset"],
            start_at=row["start_at"],
            duration_min=float(row["duration_min"]),
            repeat=row["repeat"] or REPEAT_NONE,
            enabled=bool(row["enabled"]),
            auto_record=bool(row["auto_record"]),
            threshold_mode=row["threshold_mode"] or "adaptive",
            fixed_threshold_dbm=float(row["fixed_threshold_dbm"]),
            adaptive_offset_db=float(row["adaptive_offset_db"]),
            notes=row["notes"] or "",
            owner=row["owner"] or "",
            created_at=row["created_at"] or "",
            last_run_at=row["last_run_at"],
        )

    @staticmethod
    def _row_to_run(row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            schedule_id=row["schedule_id"],
            name=row["name"] or "",
            preset=row["preset"] or "",
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=row["status"] or "running",
            channels_observed=int(row["channels_observed"] or 0),
            occupancy_pct=float(row["occupancy_pct"] or 0.0),
            max_channel_power_dbm=float(row["max_channel_power_dbm"] or -200.0),
            recordings=int(row["recordings"] or 0),
            notes=row["notes"] or "",
        )


SCHEDULES = ScheduleStore()


def parse_local(ts: str) -> datetime:
    """Terima 'YYYY-MM-DDTHH:MM' atau 'YYYY-MM-DD HH:MM[:SS]'."""
    cleaned = (ts or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError("format waktu tidak dikenal (pakai YYYY-MM-DDTHH:MM)")


def next_occurrence(s: Schedule, now: Optional[datetime] = None) -> Optional[datetime]:
    """Waktu mulai berikutnya untuk jadwal berulang, atau None bila sekali jalan."""
    now = now or datetime.now()
    try:
        base = parse_local(s.start_at)
    except ValueError:
        return None

    if s.repeat == REPEAT_NONE:
        return base if base > now else None

    step = timedelta(days=1 if s.repeat == REPEAT_DAILY else 7)
    if base > now:
        return base
    # Lewati kelipatan yang sudah terlewat tanpa iterasi per-detik.
    missed = int((now - base) / step)
    candidate = base + step * missed
    while candidate <= now:
        candidate += step
    return candidate


def compute_next_run(s: Schedule, now: Optional[datetime] = None) -> Optional[str]:
    nxt = next_occurrence(s, now)
    return nxt.isoformat(timespec="seconds") if nxt else None


class SurveyScheduler:
    """Task latar yang menjalankan kampanye survei sesuai jadwal."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._lock = threading.RLock()
        self._active: Optional[RunRecord] = None
        self._active_schedule: Optional[Schedule] = None
        self._end_at: Optional[datetime] = None
        self._recording_start_count = 0
        self._events: List[dict] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- siklus hidup ------------------------------------------------------- #
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    # -- status ------------------------------------------------------------- #
    def status(self) -> dict:
        with self._lock:
            active = self._active
            sched = self._active_schedule
            end_at = self._end_at
        remaining = None
        if end_at is not None:
            remaining = max(0.0, (end_at - datetime.now()).total_seconds())
        return {
            "active": active.as_dict() if active else None,
            "active_schedule": sched.as_dict() if sched else None,
            "remaining_sec": round(remaining, 1) if remaining is not None else None,
            "schedules": [
                {**s.as_dict(), "next_run_at": compute_next_run(s)}
                for s in SCHEDULES.list_all()
            ],
            "recent_runs": [r.as_dict() for r in SCHEDULES.list_runs(25)],
        }

    def events(self, since: int = 0) -> List[dict]:
        with self._lock:
            return list(self._events[since:])

    def _emit(self, kind: str, message: str, **extra) -> None:
        evt = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            **extra,
        }
        with self._lock:
            self._events.append(evt)
            # Batasi riwayat in-memory agar tidak tumbuh tanpa batas.
            if len(self._events) > 500:
                del self._events[:250]

    # -- kontrol manual ----------------------------------------------------- #
    def start_now(self, schedule: Schedule, owner: str = "manual") -> RunRecord:
        """Jalankan jadwal segera (tombol 'Mulai sekarang')."""
        self._begin(schedule, owner=owner, manual=True)
        run = self._active
        assert run is not None
        return run

    def cancel(self, reason: str = "dibatalkan operator") -> Optional[RunRecord]:
        return self._finish(status="cancelled", notes=reason)

    # -- loop --------------------------------------------------------------- #
    async def _run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - loop harus tetap hidup
                self._emit("error", f"Kesalahan penjadwal: {exc}")
            await asyncio.sleep(TICK_SEC)

    def _tick(self) -> None:
        now = datetime.now()

        # 1) Tutup survei yang durasinya sudah habis.
        with self._lock:
            end_at = self._end_at
        if end_at is not None and now >= end_at:
            self._finish(status="completed", notes="durasi selesai")
            return

        # 2) Jangan memulai jadwal baru bila masih ada yang berjalan.
        with self._lock:
            if self._active is not None:
                return

        # 3) Cari jadwal yang jatuh tempo (toleransi satu tick).
        for schedule in SCHEDULES.list_all():
            if not schedule.enabled:
                continue
            try:
                nxt = next_occurrence(schedule, now)
            except ValueError:
                continue
            if nxt is None or nxt > now:
                continue
            if (now - nxt).total_seconds() > TICK_SEC * 2:
                # Terlewat jauh (mis. server mati): hanya relevan untuk yang berulang,
                # dan `next_occurrence` sudah memajukan waktunya. Untuk sekali-jalan
                # yang terlewat, tandai selesai tanpa menjalankan.
                if schedule.repeat == REPEAT_NONE and (now - nxt).total_seconds() > 300:
                    SCHEDULES.mark_run(schedule.id, now.isoformat(timespec="seconds"))
                    self._emit("skipped", f"Jadwal '{schedule.name}' terlewat dan dilewati",
                               schedule_id=schedule.id)
                    continue
            self._begin(schedule, owner=schedule.owner or "scheduler")
            return

    # -- implementasi ------------------------------------------------------- #
    def _begin(self, schedule: Schedule, owner: str, manual: bool = False) -> None:
        from engine import ENGINE  # impor lokal: hindari siklus saat modul dimuat

        with self._lock:
            if self._active is not None:
                raise RuntimeError("sudah ada survei berjalan")
            # Reservasi slot lebih dulu supaya tick berikutnya tidak memulai yang lain.
            self._active = RunRecord(
                run_id=uuid.uuid4().hex[:12],
                schedule_id=schedule.id,
                name=schedule.name,
                preset=schedule.preset,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._active_schedule = schedule

        preset = get_preset(schedule.preset)
        if preset is None:
            self._finish(status="error", notes=f"preset '{schedule.preset}' tidak dikenal")
            raise ValueError(f"preset '{schedule.preset}' tidak dikenal")

        try:
            # Terapkan preset + parameter threshold kampanye ke engine.
            ENGINE.apply_preset(schedule.preset)
            ENGINE.set_sweep_params(
                center_hz=preset.center_hz,
                span_hz=preset.span_hz,
                rbw_hz=preset.rbw_hz,
                ref_level_dbm=preset.ref_level_dbm,
                atten_db=preset.atten_db,
            )
            CONFIG_update = {
                "threshold_mode": schedule.threshold_mode,
                "fixed_threshold_dbm": schedule.fixed_threshold_dbm,
                "adaptive_offset_db": schedule.adaptive_offset_db,
                "auto_record": schedule.auto_record,
            }
            from config import CONFIG
            CONFIG.update("occupancy", CONFIG_update)
            ENGINE.set_auto_record(schedule.auto_record)
            ENGINE.start(schedule.preset)
        except Exception as exc:
            self._finish(status="error", notes=f"gagal memulai: {exc}")
            raise

        with self._lock:
            self._end_at = datetime.now() + timedelta(minutes=max(1.0, schedule.duration_min))
            run = self._active
            assert run is not None
            run_record = run

        SCHEDULES.start_run(run_record)
        SCHEDULES.mark_run(schedule.id, run_record.started_at)

        try:
            OCC.begin_session(schedule.preset)
        except Exception:
            pass

        self._emit(
            "started",
            f"Survei '{schedule.name}' dimulai ({preset.name})",
            run_id=run_record.run_id,
            schedule_id=schedule.id,
            preset=schedule.preset,
            duration_min=schedule.duration_min,
            owner=owner,
            manual=manual,
        )
        AUDIT.write(owner, "schedule_start", {
            "schedule_id": schedule.id, "preset": schedule.preset,
            "duration_min": schedule.duration_min, "manual": manual,
        })

    def _finish(self, status: str, notes: str = "") -> Optional[RunRecord]:
        with self._lock:
            run = self._active
            schedule = self._active_schedule
            if run is None:
                self._end_at = None
                return None
            self._active = None
            self._active_schedule = None
            self._end_at = None

        from engine import ENGINE
        from config import CONFIG

        run.ended_at = datetime.now().isoformat(timespec="seconds")
        run.status = status

        # Ringkas okupansi dari akumulator + DB untuk snapshot laporan.
        try:
            OCC.flush()
            stats = OCC.all_stats()
            if stats:
                run.channels_observed = len(stats)
                active = [s for s in stats.values() if s.n_total > 0]
                run.occupancy_pct = (
                    sum(s.occupancy_pct for s in active) / len(active) if active else 0.0
                )
                run.max_channel_power_dbm = max((s.peak_dbm for s in stats.values()), default=-200.0)
        except Exception as exc:
            run.notes = f"{run.notes}; ringkasan gagal: {exc}".strip("; ")

        run.notes = (notes or run.notes or "")[:500]

        # Auto-record dimatikan agar tidak terus merekam di luar kampanye.
        try:
            ENGINE.set_auto_record(False)
            CONFIG.update("occupancy", {"auto_record": False})
        except Exception:
            pass

        # Simpan snapshot survei untuk laporan.
        try:
            preset = get_preset(run.preset)
            occ_cfg = CONFIG.get("occupancy")
            OCC.save_survey(SurveyRun(
                id=0,
                preset=run.preset,
                preset_name=preset.name if preset else run.preset,
                center_hz=preset.center_hz if preset else 0.0,
                start_at=run.started_at,
                end_at=run.ended_at,
                threshold_mode=str(occ_cfg.get("threshold_mode", "adaptive")),
                threshold_dbm=float(occ_cfg.get("fixed_threshold_dbm", -85.0)),
                channels_observed=run.channels_observed,
                occupancy_pct=run.occupancy_pct,
                max_channel_power_dbm=run.max_channel_power_dbm,
                notes=run.notes,
            ))
        except Exception:
            pass

        SCHEDULES.finish_run(run)
        self._emit("finished", f"Survei '{run.name}' berakhir ({status})",
                   run_id=run.run_id, status=status)
        if schedule is not None:
            AUDIT.write(schedule.owner or "scheduler", "schedule_finish", {
                "schedule_id": schedule.id, "status": status,
                "occupancy_pct": round(run.occupancy_pct, 2),
            })
        return run


SCHEDULER = SurveyScheduler()
