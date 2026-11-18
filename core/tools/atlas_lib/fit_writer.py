"""Pure-stdlib ANT+ / Garmin `.FIT` Binary Workout File Generator.

Python 3 standard library only (`struct`, `re`, `time`). Generates valid binary `.FIT`
workout files (`FileType.WORKOUT = 5`) with `file_id`, `workout`, and `workout_step`
messages plus 16-bit FIT header & file CRCs for Garmin watches (Forerunner, Fenix, Epix),
Garmin Edge, Wahoo, Hammerhead Karoo, and Zwift (`GARMIN/NewFiles`).
"""
from __future__ import annotations

import re
import struct
import time
from typing import Any

FIT_EPOCH_OFFSET = 631065600  # Seconds between 1970-01-01T00:00:00Z and 1989-12-31T00:00:00Z

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)

# FIT Base Types
BT_ENUM = 0x00
BT_STRING = 0x07
BT_UINT16 = 0x84
BT_UINT32 = 0x86
BT_UINT32Z = 0x8C

UINT32_INVALID = 0xFFFFFFFF

# FIT Sport Enums
FIT_SPORT_RUNNING = 1
FIT_SPORT_CYCLING = 2
FIT_SPORT_TRAINING = 10

# FIT Intensity Enums
FIT_INTENSITY_ACTIVE = 0
FIT_INTENSITY_REST = 1
FIT_INTENSITY_WARMUP = 2
FIT_INTENSITY_COOLDOWN = 3
FIT_INTENSITY_RECOVERY = 4

# FIT WorkoutStepDuration Enums
FIT_DUR_TIME = 0
FIT_DUR_DISTANCE = 1
FIT_DUR_OPEN = 5

# FIT WorkoutStepTarget Enums
FIT_TGT_SPEED = 0
FIT_TGT_HR = 1
FIT_TGT_OPEN = 2
FIT_TGT_POWER = 4


def fit_crc16(data: bytes) -> int:
    """Compute standard 16-bit ANT+ FIT CRC over `data`."""
    crc = 0
    for b in data:
        tmp = _CRC_TABLE[crc & 0x0F]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[b & 0x0F]
        tmp = _CRC_TABLE[crc & 0x0F]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(b >> 4) & 0x0F]
    return crc & 0xFFFF


def _fixed_utf8(text: str, size: int) -> bytes:
    """Encode `text` as null-terminated UTF-8 padded with `0x00` to `size` bytes."""
    clean = re.sub(r"[^\w\s.,:/%+-]", "", str(text or "")).strip()
    raw = clean.encode("utf-8")[: max(0, size - 1)]
    return raw + b"\x00" * (size - len(raw))


def _def_mesg(local_num: int, global_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    """Build a FIT Definition Message (little-endian `arch = 0`)."""
    hdr = 0x40 | (local_num & 0x0F)
    out = bytearray(struct.pack("<BBBHB", hdr, 0, 0, global_num, len(fields)))
    for f_num, f_size, f_type in fields:
        out.extend(struct.pack("<BBB", f_num, f_size, f_type))
    return bytes(out)


def _expand_parsed_blocks_to_fit_steps(
    parsed_spec: dict[str, Any] | None,
    sport: str,
    ftp_watts: int = 275,
    cs_mps: float | None = None,
    default_duration_min: int = 45,
) -> list[dict[str, Any]]:
    """Flatten `parse_workout_spec` blocks (including repeats) into executable FIT steps."""
    steps: list[dict[str, Any]] = []
    raw_items = (parsed_spec or {}).get("segments") or (parsed_spec or {}).get("blocks") or []
    sp_low = str(sport or "cycling").lower()
    is_cycling = sp_low == "cycling"
    is_running = sp_low == "running"
    eff_ftp = max(80, int(ftp_watts or 275))
    eff_cs = float(cs_mps) if (cs_mps and cs_mps > 1.5) else None

    def _power_bounds(p_low_f: float, p_high_f: float) -> tuple[int, int, int]:
        if not is_cycling:
            return FIT_TGT_OPEN, UINT32_INVALID, UINT32_INVALID
        lo_w = max(40, int(round(min(p_low_f, p_high_f) * eff_ftp * 0.97)))
        hi_w = max(lo_w + 5, int(round(max(p_low_f, p_high_f) * eff_ftp * 1.03)))
        # ANT+ FIT custom power offset: watts + 1000
        return FIT_TGT_POWER, 1000 + lo_w, 1000 + hi_w

    def _run_bounds(intensity_factor: float) -> tuple[int, int, int]:
        if not is_running or not eff_cs:
            return FIT_TGT_OPEN, UINT32_INVALID, UINT32_INVALID
        lo_mps = max(1.5, eff_cs * intensity_factor * 0.96)
        hi_mps = max(lo_mps + 0.1, eff_cs * intensity_factor * 1.04)
        # ANT+ FIT custom speed scale: m/s * 1000
        return FIT_TGT_SPEED, int(round(lo_mps * 1000.0)), int(round(hi_mps * 1000.0))

    for blk in raw_items:
        kind = str(blk.get("type") or blk.get("kind") or "steady").lower()
        if kind == "intervals":
            reps = max(1, int(blk.get("repeat") or blk.get("repeats") or 1))
            on_s = max(5, int(blk.get("on_sec") or 60))
            off_s = max(5, int(blk.get("off_sec") or 60))
            on_p = float(blk.get("on_p") or blk.get("on_power") or 1.0)
            off_p = float(blk.get("off_p") or blk.get("off_power") or 0.55)
            for r_idx in range(1, reps + 1):
                t_type, t_lo, t_hi = _power_bounds(on_p, on_p) if is_cycling else _run_bounds(on_p)
                steps.append({
                    "name": f"Int {r_idx}/{reps}",
                    "duration_ms": on_s * 1000,
                    "intensity": FIT_INTENSITY_ACTIVE,
                    "target_type": t_type,
                    "custom_low": t_lo,
                    "custom_high": t_hi,
                })
                r_type, r_lo, r_hi = _power_bounds(off_p, off_p) if is_cycling else _run_bounds(off_p)
                steps.append({
                    "name": "Recovery",
                    "duration_ms": off_s * 1000,
                    "intensity": FIT_INTENSITY_RECOVERY,
                    "target_type": r_type,
                    "custom_low": r_lo,
                    "custom_high": r_hi,
                })
        elif kind in ("warmup", "cooldown", "ramp"):
            dur_s = max(30, int(blk.get("sec") or blk.get("duration_sec") or 600))
            p_lo = float(blk.get("p_low") or blk.get("power_low") or 0.55)
            p_hi = float(blk.get("p_high") or blk.get("power_high") or 0.72)
            p_mid = 0.5 * (p_lo + p_hi)
            t_type, t_lo, t_hi = _power_bounds(p_lo, p_hi) if is_cycling else _run_bounds(p_mid)
            steps.append({
                "name": "Warmup" if kind == "warmup" else ("Cooldown" if kind == "cooldown" else "Ramp"),
                "duration_ms": dur_s * 1000,
                "intensity": FIT_INTENSITY_WARMUP if kind == "warmup" else (FIT_INTENSITY_COOLDOWN if kind == "cooldown" else FIT_INTENSITY_ACTIVE),
                "target_type": t_type,
                "custom_low": t_lo,
                "custom_high": t_hi,
            })
        else:
            dur_s = max(30, int(blk.get("sec") or blk.get("duration_sec") or 600))
            pwr = float(blk.get("p") or blk.get("power") or 0.70)
            inten = FIT_INTENSITY_RECOVERY if pwr < 0.60 else FIT_INTENSITY_ACTIVE
            lbl = str(blk.get("label") or ("Recovery" if inten == FIT_INTENSITY_RECOVERY else "Steady"))[:22]
            t_type, t_lo, t_hi = _power_bounds(pwr, pwr) if is_cycling else _run_bounds(pwr)
            steps.append({
                "name": lbl,
                "duration_ms": dur_s * 1000,
                "intensity": inten,
                "target_type": t_type,
                "custom_low": t_lo,
                "custom_high": t_hi,
            })

    if not steps:
        tot_s = max(600, int((parsed_spec or {}).get("total_min") or default_duration_min) * 60)
        wu_s = min(600, int(tot_s * 0.2))
        cd_s = min(300, int(tot_s * 0.15))
        main_s = max(300, tot_s - wu_s - cd_s)
        for nm, ds, inten, pf in (
            ("Warmup", wu_s, FIT_INTENSITY_WARMUP, 0.62),
            ("Main Set", main_s, FIT_INTENSITY_ACTIVE, 0.72),
            ("Cooldown", cd_s, FIT_INTENSITY_COOLDOWN, 0.58),
        ):
            t_type, t_lo, t_hi = _power_bounds(pf, pf) if is_cycling else _run_bounds(pf)
            steps.append({
                "name": nm,
                "duration_ms": ds * 1000,
                "intensity": inten,
                "target_type": t_type,
                "custom_low": t_lo,
                "custom_high": t_hi,
            })
    return steps


def compile_fit_workout_bytes(
    title: str,
    sport: str = "cycling",
    parsed_spec: dict[str, Any] | None = None,
    ftp_watts: int = 275,
    cs_mps: float | None = None,
    default_duration_min: int = 45,
    now_unix: int | None = None,
) -> bytes:
    """Compile a binary ANT+ `.FIT` workout file using Python stdlib `struct` only."""
    ts_unix = int(now_unix if now_unix is not None else time.time())
    fit_ts = max(1, ts_unix - FIT_EPOCH_OFFSET)
    serial = (ts_unix & 0x7FFFFFFF) or 1

    sp_low = str(sport or "cycling").lower()
    if sp_low in ("run", "running", "trailrun"):
        fit_sport = FIT_SPORT_RUNNING
    elif sp_low in ("cycling", "ride", "virtualride", "gravel"):
        fit_sport = FIT_SPORT_CYCLING
    else:
        fit_sport = FIT_SPORT_TRAINING

    steps = _expand_parsed_blocks_to_fit_steps(
        parsed_spec=parsed_spec,
        sport=sp_low,
        ftp_watts=ftp_watts,
        cs_mps=cs_mps,
        default_duration_min=default_duration_min,
    )

    records = bytearray()

    # 1. file_id (Global Mesg 0, Local 0)
    records.extend(_def_mesg(0, 0, [
        (0, 1, BT_ENUM),      # type = 5 (workout)
        (1, 2, BT_UINT16),    # manufacturer = 255 (development)
        (2, 2, BT_UINT16),    # product = 1
        (3, 4, BT_UINT32Z),   # serial_number
        (4, 4, BT_UINT32),    # time_created
    ]))
    records.extend(struct.pack("<BBHHII", 0x00, 5, 255, 1, serial, fit_ts))

    # 2. workout (Global Mesg 26, Local 1)
    records.extend(_def_mesg(1, 26, [
        (4, 1, BT_ENUM),      # sport
        (6, 2, BT_UINT16),    # num_valid_steps
        (8, 32, BT_STRING),   # wkt_name
    ]))
    records.extend(struct.pack("<BBH", 0x01, fit_sport, len(steps)) + _fixed_utf8(title or "Atlas Workout", 32))

    # 3. workout_step (Global Mesg 27, Local 2)
    records.extend(_def_mesg(2, 27, [
        (254, 2, BT_UINT16),  # message_index
        (0, 24, BT_STRING),   # wkt_step_name
        (1, 1, BT_ENUM),      # duration_type
        (2, 4, BT_UINT32),    # duration_value (ms)
        (3, 1, BT_ENUM),      # target_type
        (4, 4, BT_UINT32),    # target_value (0 = custom low/high)
        (5, 4, BT_UINT32),    # custom_target_value_low
        (6, 4, BT_UINT32),    # custom_target_value_high
        (7, 1, BT_ENUM),      # intensity
    ]))
    for idx, st in enumerate(steps):
        records.extend(
            struct.pack("<BH", 0x02, idx)
            + _fixed_utf8(st["name"], 24)
            + struct.pack(
                "<BIBIIIB",
                FIT_DUR_TIME,
                int(st["duration_ms"]),
                int(st["target_type"]),
                0,
                int(st["custom_low"]),
                int(st["custom_high"]),
                int(st["intensity"]),
            )
        )

    rec_bytes = bytes(records)
    header_no_crc = struct.pack("<BBHI4s", 14, 0x10, 2132, len(rec_bytes), b".FIT")
    header_bytes = header_no_crc + struct.pack("<H", fit_crc16(header_no_crc))
    full_no_file_crc = header_bytes + rec_bytes
    file_crc = struct.pack("<H", fit_crc16(full_no_file_crc))
    return full_no_file_crc + file_crc
