"""Generates firmware-ready C++ snippets matching the existing hand-written
style in firmware/movement-sequences.h and firmware/face-bitmaps.h, so output
can be pasted straight into those files. See ValidationError for range
problems the UI should surface before export.
"""

from __future__ import annotations

import re

from .project import Face, Sequence, frame_to_pixels
from .servo_model import SERVO_ORDER, SERVO_RANGES, RESERVED_FACE_NAMES, is_valid_angle

FACE_ANIM_MODE_C = {
    "loop": "FACE_ANIM_LOOP",
    "once": "FACE_ANIM_ONCE",
    "boomerang": "FACE_ANIM_BOOMERANG",
}


class ValidationError(Exception):
    pass


def _sanitize_identifier(name: str) -> str:
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip())
    if not ident:
        ident = "sequence"
    if ident[0].isdigit():
        ident = f"_{ident}"
    return ident


def _pascal_case(ident: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in ident.split("_") if part)


def validate_sequence(seq: Sequence) -> list[str]:
    """Returns a list of human-readable problems; empty list means OK."""
    problems: list[str] = []
    if not seq.frames:
        problems.append(f"Sequence '{seq.name}' has no frames.")
    for idx, frame in enumerate(seq.frames, start=1):
        for servo in SERVO_ORDER:
            angle = frame.angles.get(servo)
            if angle is None:
                problems.append(f"Frame {idx}: missing angle for {servo}.")
                continue
            if not is_valid_angle(servo, angle):
                lo, hi = SERVO_RANGES[servo]
                problems.append(
                    f"Frame {idx}: {servo}={angle} is out of range ({lo}-{hi})."
                )
    return problems


def export_sequence(seq: Sequence) -> str:
    """Generates a runXPose()-style function. Raises ValidationError if any
    frame has an out-of-range angle."""
    problems = validate_sequence(seq)
    if problems:
        raise ValidationError("\n".join(problems))

    ident = _sanitize_identifier(seq.name)
    fn_name = f"run{_pascal_case(ident)}Pose"
    label = ident.upper()

    lines = [
        f"void {fn_name}() {{",
        f'  Serial.println(F("{label}"));',
    ]
    last_face = object()
    for i, frame in enumerate(seq.frames):
        lines.append(f"  // Frame {i + 1}")
        if frame.face and frame.face != last_face:
            mode = FACE_ANIM_MODE_C.get(frame.face_mode, "FACE_ANIM_LOOP")
            lines.append(f'  setFaceWithMode("{frame.face}", {mode});')
            last_face = frame.face
        for servo in SERVO_ORDER:
            lines.append(f"  setServoAngle({servo}, {frame.angles[servo]});")
        lines.append(f"  delayWithFace({frame.delay_ms});")
        lines.append("")
    lines.append("}")
    return "\n".join(lines)


def export_sequence_prototype(seq: Sequence) -> str:
    ident = _sanitize_identifier(seq.name)
    return f"void run{_pascal_case(ident)}Pose();"


def _format_byte_array(data: bytes, per_line: int = 16) -> str:
    rows = []
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        rows.append(", ".join(f"0x{b:02X}" for b in chunk))
    return ",\n".join(rows)


def export_face(face: Face) -> str:
    """Generates the epd_bitmap_* array(s) for a face, in image2cpp's output
    format, ready to paste into firmware/face-bitmaps.h."""
    blocks: list[str] = []
    for i, frame in enumerate(face.frames):
        suffix = "" if i == 0 else f"_{i}"
        var = f"epd_bitmap_{face.name}{suffix}"
        blocks.append(
            f"// '{face.name}{suffix}', 128x64px\n"
            f"const unsigned char {var} [] PROGMEM = {{\n"
            f"{_format_byte_array(bytes(frame))}\n"
            f"}};"
        )
    return "\n\n".join(blocks)


def face_list_entry(face: Face) -> str:
    return f"\tX({face.name})"


def face_fps_entry(face: Face) -> str:
    return f'  {{ "{face.name}", {face.fps} }},'


def check_face_name_collision(face: Face) -> str | None:
    """Returns a warning string if the name collides with a reserved
    firmware face and the user hasn't explicitly opted to override it."""
    if face.name in RESERVED_FACE_NAMES and not face.is_override:
        cat = RESERVED_FACE_NAMES[face.name]
        return (
            f"'{face.name}' is a built-in {cat} face name. Enable "
            "'Override existing face' in the Face Editor if you intend to "
            "replace it, otherwise choose a different name."
        )
    return None


def export_face_bundle(face: Face) -> str:
    """Full paste-ready bundle: FACE_LIST entry, byte arrays, and (if fps
    differs from the global default) a faceFpsEntries line, each labeled
    with where it goes."""
    warning = check_face_name_collision(face)
    parts = []
    if warning:
        parts.append(f"// WARNING: {warning}")
    parts.append("// 1) Add to FACE_LIST in face-bitmaps.h:")
    parts.append(face_list_entry(face))
    parts.append("")
    parts.append("// 2) Paste bitmap array(s) into face-bitmaps.h:")
    parts.append(export_face(face))
    parts.append("")
    parts.append("// 3) Add FPS entry to faceFpsEntries[] in sesame-firmware-main.ino:")
    parts.append(face_fps_entry(face))
    return "\n".join(parts)
