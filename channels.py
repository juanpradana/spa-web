"""Preset pita frekuensi dan rencana kanal (channel plan).

Preset dipakai UI sebagai tombol 1-klik dan oleh penjadwal survei. Nilai
RBW/VBW untuk pita microwave sengaja lebih lebar karena noise floor SA124B
naik tajam di atas 8 GHz (DANL -134 dBm @ 8-11 GHz).

Seluruh RBW yang ditawarkan ke perangkat melewati `clamp_rbw()` supaya tidak
melampaui batas demodulator I/Q internal SA124B (250 kHz pada mode sapuan).
Nilai yang diminta di atas batas itu akan di-clamp oleh pustaka Signal Hound
dan menghasilkan sapuan yang tidak lagi sesuai permintaan operator, jadi
pembatasan dilakukan di sisi aplikasi agar UI menampilkan angka yang benar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Batas RBW hardware SA124B (mode sapuan / swept)
#
# Demodulator I/Q internal hanya menangkap sampai 250 kHz; sapuan yang lebih
# lebar adalah gabungan sub-sapuan bertahap. Karena itu RBW efektif tidak
# pernah melebihi 250 kHz. Batas bawah bergantung pada lebar sapuan:
#   - sapuan > 99 MHz  atau start < 16 MHz : RBW minimum 6.5 kHz
#   - sapuan 201 kHz .. 99 MHz             : RBW minimum 30 Hz
#   - sapuan < 201 kHz                     : RBW minimum 0.1 Hz
# --------------------------------------------------------------------------- #
RBW_ABS_MIN_HZ = 0.1
RBW_MAX_SWEPT_HZ = 250_000.0
RBW_MIN_NARROW_HZ = 30.0
RBW_MIN_WIDE_HZ = 6_500.0

NARROW_SPAN_MAX_HZ = 99_000_000.0
NARROW_SPAN_MIN_HZ = 201_000.0
LOW_BAND_EDGE_HZ = 16_000_000.0


def clamp_rbw(center_hz: float, span_hz: float, rbw_hz: float) -> float:
    """Kembalikan RBW yang sah untuk geometri sapuan SA124B.

    Memakai (center, span) alih-alih (start, span) supaya pemanggil tidak perlu
    menghitung tepi pita; `start < 16 MHz` juga mencakup kasus span yang lebih
    lebar dari dua kali center.
    """
    try:
        center = float(center_hz)
        span = float(span_hz)
        wanted = float(rbw_hz)
    except (TypeError, ValueError):
        return RBW_MAX_SWEPT_HZ

    start_hz = center - span / 2.0
    if span > NARROW_SPAN_MAX_HZ or start_hz < LOW_BAND_EDGE_HZ or span <= 0:
        low = RBW_MIN_WIDE_HZ
    elif span >= NARROW_SPAN_MIN_HZ:
        low = RBW_MIN_NARROW_HZ
    else:
        low = RBW_ABS_MIN_HZ

    return max(low, min(wanted, RBW_MAX_SWEPT_HZ))


def clamp_vbw(rbw_hz: float, vbw_hz: float) -> float:
    """VBW tidak boleh melebihi RBW (aturan kopling pustaka Signal Hound)."""
    try:
        rbw = float(rbw_hz)
        vbw = float(vbw_hz)
    except (TypeError, ValueError):
        return float(rbw_hz)
    if vbw <= 0:
        return rbw
    return min(vbw, rbw)


@dataclass(frozen=True)
class Channel:
    freq_hz: float
    label: str = ""
    bw_hz: float = 12_500.0

    @property
    def mhz(self) -> float:
        return self.freq_hz / 1e6


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    category: str
    description: str
    center_hz: float
    span_hz: float
    demod: str = "FM"
    if_bw_hz: float = 15_000.0
    rbw_hz: float = 30_000.0
    vbw_hz: float = 30_000.0
    ref_level_dbm: float = -20.0
    atten_db: int = 10
    channel_bw_hz: float = 12_500.0
    channel_start_hz: Optional[float] = None
    channel_stop_hz: Optional[float] = None
    channel_step_hz: float = 25_000.0
    extra_channels: List[float] = field(default_factory=list)
    notes: str = ""

    def channels(self) -> List[Channel]:
        """Bangun daftar kanal diskrit dari rentang + spacing."""
        out: List[Channel] = []
        start = self.channel_start_hz or (self.center_hz - self.span_hz / 2)
        stop = self.channel_stop_hz or (self.center_hz + self.span_hz / 2)
        step = max(self.channel_step_hz, self.channel_bw_hz)
        if step > 0 and stop > start:
            count = int((stop - start) / step)
            # Batasi agar UI tetap responsif (maks 4000 kanal per preset).
            if count <= 4000:
                f = start
                while f <= stop:
                    out.append(Channel(f, bw_hz=self.channel_bw_hz))
                    f += step
        for f in self.extra_channels:
            out.append(Channel(f, bw_hz=self.channel_bw_hz))
        return out

    @property
    def effective_rbw_hz(self) -> float:
        """RBW setelah dibatasi ke kemampuan hardware SA124B."""
        return clamp_rbw(self.center_hz, self.span_hz, self.rbw_hz)

    @property
    def effective_vbw_hz(self) -> float:
        return clamp_vbw(self.effective_rbw_hz, self.vbw_hz)

    @property
    def channel_step_hz_effective(self) -> float:
        """Stepwidth = jarak antar kanal; tidak boleh lebih sempit dari bandwidth."""
        return max(self.channel_step_hz, self.channel_bw_hz)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "center_hz": self.center_hz,
            "span_hz": self.span_hz,
            "demod": self.demod,
            "if_bw_hz": self.if_bw_hz,
            "rbw_hz": self.effective_rbw_hz,
            "vbw_hz": self.effective_vbw_hz,
            "rbw_requested_hz": self.rbw_hz,
            "ref_level_dbm": self.ref_level_dbm,
            "atten_db": self.atten_db,
            "channel_bw_hz": self.channel_bw_hz,
            "channel_step_hz": self.channel_step_hz_effective,
            "channel_count": len(self.channels()),
            "notes": self.notes,
        }


PRESETS: Dict[str, Preset] = {}


def _add(p: Preset) -> None:
    PRESETS[p.key] = p


# --- VHF / UHF voice ------------------------------------------------------- #
_add(Preset(
    key="vhf_airband",
    name="VHF Airband (118-137 MHz)",
    category="VHF/UHF Voice",
    description="Komunikasi penerbangan. Modulasi AM, kanal 25 kHz.",
    center_hz=127_500_000.0, span_hz=19_000_000.0,
    demod="AM", if_bw_hz=8_000.0,
    rbw_hz=25_000.0, vbw_hz=25_000.0, ref_level_dbm=-25.0, atten_db=10,
    channel_bw_hz=25_000.0, channel_step_hz=25_000.0,
    channel_start_hz=118_000_000.0, channel_stop_hz=137_000_000.0,
    extra_channels=[121_500_000.0, 243_000_000.0],
    notes="121.5 MHz = emergency. 243 MHz = militer (butuh harmonisa/antena lebar).",
))

_add(Preset(
    key="vhf_marine",
    name="VHF Marine (156-162 MHz)",
    category="VHF/UHF Voice",
    description="Kanal maritim internasional. Modulasi FM, spacing 25 kHz.",
    center_hz=159_000_000.0, span_hz=6_000_000.0,
    demod="FM", if_bw_hz=15_000.0,
    rbw_hz=25_000.0, vbw_hz=25_000.0, ref_level_dbm=-25.0, atten_db=10,
    channel_bw_hz=25_000.0, channel_step_hz=25_000.0,
    channel_start_hz=156_000_000.0, channel_stop_hz=162_000_000.0,
    extra_channels=[156_800_000.0, 156_525_000.0],
    notes="156.800 MHz = Ch16 distress/safety.",
))

_add(Preset(
    key="uhf_ht",
    name="UHF HT / PMR (400-470 MHz)",
    category="VHF/UHF Voice",
    description="Handy talkie, repeater komunitas, radio trunking. FM 12.5 kHz.",
    center_hz=435_000_000.0, span_hz=70_000_000.0,
    demod="FM", if_bw_hz=12_500.0,
    rbw_hz=30_000.0, vbw_hz=30_000.0, ref_level_dbm=-20.0, atten_db=10,
    channel_bw_hz=12_500.0, channel_step_hz=25_000.0,
    channel_start_hz=400_000_000.0, channel_stop_hz=470_000_000.0,
    extra_channels=[433_920_000.0, 446_006_250.0, 446_093_750.0],
    notes="433.92 MHz = ISM. 446 MHz = PMR446.",
))

_add(Preset(
    key="vhf_lmr",
    name="VHF LMR (136-174 MHz)",
    category="VHF/UHF Voice",
    description="Land Mobile Radio VHF: instansi, kehutanan, perhubungan.",
    center_hz=155_000_000.0, span_hz=38_000_000.0,
    demod="FM", if_bw_hz=12_500.0,
    rbw_hz=30_000.0, vbw_hz=30_000.0, ref_level_dbm=-20.0, atten_db=10,
    channel_bw_hz=12_500.0, channel_step_hz=25_000.0,
    channel_start_hz=136_000_000.0, channel_stop_hz=174_000_000.0,
))

# --- SFR / broadcast ------------------------------------------------------- #
_add(Preset(
    key="fm_broadcast",
    name="FM Broadcast (87.5-108 MHz)",
    category="Broadcast",
    description="Radio siaran FM. IF BW lebih lebar (180 kHz) untuk deviasi penuh.",
    center_hz=97_750_000.0, span_hz=20_500_000.0,
    demod="FM", if_bw_hz=180_000.0,
    rbw_hz=100_000.0, vbw_hz=100_000.0, ref_level_dbm=-10.0, atten_db=20,
    channel_bw_hz=200_000.0, channel_step_hz=200_000.0,
    channel_start_hz=87_500_000.0, channel_stop_hz=108_000_000.0,
    notes="Cocok untuk uji OBW dan harmonisa siaran FM.",
))

# --- Microwave / SFR link -------------------------------------------------- #
_add(Preset(
    key="microwave_lower",
    name="Microwave Link 6/7 GHz",
    category="Microwave / SFR Link",
    description="Point-to-point 5.925-7.125 GHz (lower band) dan 7.125-7.725 GHz.",
    center_hz=6_800_000_000.0, span_hz=500_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=1_000_000.0, vbw_hz=1_000_000.0, ref_level_dbm=-40.0, atten_db=20,
    channel_bw_hz=29_650_000.0, channel_step_hz=29_650_000.0,
    channel_start_hz=5_925_000_000.0, channel_stop_hz=7_125_000_000.0,
    notes="DANL SA124B di pita ini sekitar -149 dBm/Hz. Gunakan antena directional.",
))

_add(Preset(
    key="microwave_upper",
    name="Microwave Link 8/11 GHz",
    category="Microwave / SFR Link",
    description="Band 7.725-8.5 GHz dan 10.7-11.7 GHz.",
    center_hz=8_100_000_000.0, span_hz=700_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=1_000_000.0, vbw_hz=1_000_000.0, ref_level_dbm=-40.0, atten_db=20,
    channel_bw_hz=29_650_000.0, channel_step_hz=29_650_000.0,
    channel_start_hz=7_725_000_000.0, channel_stop_hz=8_500_000_000.0,
    notes="DANL sekitar -134 dBm/Hz di 8-11 GHz; sensitivitas menurun.",
))

_add(Preset(
    key="cband_satelit",
    name="C-Band Satelit (3.4-4.2 GHz)",
    category="Microwave / SFR Link",
    description="Downlink stasiun bumi C-band / VSAT.",
    center_hz=3_800_000_000.0, span_hz=800_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=1_000_000.0, vbw_hz=1_000_000.0, ref_level_dbm=-45.0, atten_db=20,
    channel_bw_hz=36_000_000.0, channel_step_hz=40_000_000.0,
    channel_start_hz=3_400_000_000.0, channel_stop_hz=4_200_000_000.0,
))

# --- ISM ------------------------------------------------------------------- #
_add(Preset(
    key="ism_24",
    name="ISM 2.4 GHz (2400-2483.5 MHz)",
    category="ISM / Drone",
    description="WiFi 2.4 GHz, Bluetooth, RC/drone OcuSync & ExpressLRS.",
    center_hz=2_441_750_000.0, span_hz=83_500_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=250_000.0, vbw_hz=250_000.0, ref_level_dbm=-30.0, atten_db=20,
    channel_bw_hz=20_000_000.0, channel_step_hz=20_000_000.0,
    channel_start_hz=2_400_000_000.0, channel_stop_hz=2_483_500_000.0,
    notes="Untuk karakterisasi okupansi/emisi; audio digital tidak dapat didemodulasi SA124B.",
))

_add(Preset(
    key="ism_58",
    name="ISM 5.8 GHz (5725-5850 MHz)",
    category="ISM / Drone",
    description="WiFi 5 GHz, video/telemetri drone FPV analog & digital.",
    center_hz=5_787_500_000.0, span_hz=125_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=500_000.0, vbw_hz=500_000.0, ref_level_dbm=-35.0, atten_db=20,
    channel_bw_hz=20_000_000.0, channel_step_hz=20_000_000.0,
    channel_start_hz=5_725_000_000.0, channel_stop_hz=5_850_000_000.0,
))

# --- Seluler (untuk pelacakan interferensi uplink) -------------------------- #
_add(Preset(
    key="cellular_uplink",
    name="Uplink Seluler (900/1800/2100 MHz)",
    category="Interferensi",
    description="Pita uplink 890-915, 1710-1785, 1920-1980 MHz. Pelacakan repeater liar/jammer.",
    center_hz=1_900_000_000.0, span_hz=1_100_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=250_000.0, vbw_hz=250_000.0, ref_level_dbm=-30.0, atten_db=20,
    channel_bw_hz=5_000_000.0, channel_step_hz=5_000_000.0,
    channel_start_hz=890_000_000.0, channel_stop_hz=1_980_000_000.0,
))

# --- Broadband ------------------------------------------------------------- #
_add(Preset(
    key="broadband_vhf_uhf",
    name="Broadband Sweep VHF-UHF (100-1000 MHz)",
    category="Broadband",
    description="Survei cepat mencari emisi tak dikenal di pita VHF/UHF.",
    center_hz=550_000_000.0, span_hz=900_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=1_000_000.0, vbw_hz=1_000_000.0, ref_level_dbm=-25.0, atten_db=10,
    channel_bw_hz=1_000_000.0, channel_step_hz=1_000_000.0,
    channel_start_hz=100_000_000.0, channel_stop_hz=1_000_000_000.0,
    notes="RBW lebar => noise floor lebih tinggi, sinyal lemah bisa terlewat.",
))

_add(Preset(
    key="broadband_full",
    name="Broadband Sweep Penuh (100 kHz - 12.4 GHz)",
    category="Broadband",
    description="Sapuan seluruh kemampuan SA124B. Gunakan sebagai pemindaian awal.",
    center_hz=6_200_000_000.0, span_hz=12_400_000_000.0,
    demod="FM", if_bw_hz=200_000.0,
    rbw_hz=6_000_000.0, vbw_hz=6_000_000.0, ref_level_dbm=-20.0, atten_db=10,
    channel_bw_hz=50_000_000.0, channel_step_hz=50_000_000.0,
    channel_start_hz=100_000.0, channel_stop_hz=12_400_000_000.0,
    notes="6 MHz RBW hanya tersedia di atas 200 MHz; di bawahnya perangkat memakai RBW lebih sempit.",
))


def get_preset(key: str) -> Optional[Preset]:
    return PRESETS.get(key)


def preset_list() -> List[dict]:
    return [p.as_dict() for p in PRESETS.values()]


def categories() -> List[str]:
    seen: List[str] = []
    for p in PRESETS.values():
        if p.category not in seen:
            seen.append(p.category)
    return seen
