"""Model and parser/serializer for firmware/face-bitmaps.h.

Allows opening an Arduino face-bitmaps.h file, editing faces in Sesame Studio's
Face Library and Face Painter, and saving the changes directly back into the
header file with valid C++ PROGMEM bitmap definitions and FACE_LIST macros.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Set, Optional

from .project import Face, BYTES_PER_FRAME, blank_frame
from .servo_model import (
    RESERVED_FACE_NAMES, FACE_CATEGORY_CUSTOM, FACE_CATEGORY_TALK,
    FACE_WIDTH, FACE_HEIGHT,
)

BITMAP_RE = re.compile(
    r"const\s+unsigned\s+char\s+epd_bitmap_(\w+)\s*\[\]\s*PROGMEM\s*=\s*\{(.*?)\};",
    re.DOTALL,
)
HEX_RE = re.compile(r"0x([0-9A-Fa-f]{1,2})")
FRAME_SUFFIX_RE = re.compile(r"^(.+)_([1-5])$")
FACE_LIST_RE = re.compile(
    r"#define\s+FACE_LIST\s*\\(.*?)(?=\n\s*#define\s+X|\n\s*#undef)",
    re.DOTALL,
)
FPS_ENTRY_RE = re.compile(r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*\}')


def format_byte_array(data: bytes, per_line: int = 16) -> str:
    """Format bytes as tab-indented hex lines of 16 bytes each."""
    rows = []
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        rows.append("\t" + ", ".join(f"0x{b:02X}" for b in chunk))
    return ",\n".join(rows)


def parse_fps_from_sibling_ino(header_path: str) -> dict[str, int]:
    """Look for sesame-firmware-main.ino in the same directory as header_path."""
    try:
        ino_path = Path(header_path).parent / "sesame-firmware-main.ino"
        if not ino_path.is_file():
            return {}
        text = ino_path.read_text(encoding="utf-8")
        match = re.search(r"faceFpsEntries\[\]\s*=\s*\{(.*?)\};", text, re.DOTALL)
        if not match:
            return {}
        return {name: int(fps) for name, fps in FPS_ENTRY_RE.findall(match.group(1))}
    except Exception:
        return {}


class FaceBitmapsFile:
    def __init__(
        self,
        path: str,
        faces: dict[str, Face],
        order: list[str],
        undefined_names: set[str],
        preamble: str = "",
        tail: str = "",
    ):
        self.path = os.path.abspath(path)
        self.faces = faces
        self.order = list(order)
        self.undefined_names = set(undefined_names)
        self.preamble = preamble
        self.tail = tail
        self.dirty = False

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    def mark_dirty(self) -> None:
        self.dirty = True

    def add_face(self, face: Face) -> None:
        self.faces[face.name] = face
        if face.name not in self.order:
            self.order.append(face.name)
        self.undefined_names.discard(face.name)
        self.dirty = True

    def remove_face(self, name: str) -> None:
        if name in self.faces:
            del self.faces[name]
        if name in self.order:
            self.order.remove(name)
        self.undefined_names.discard(name)
        self.dirty = True

    def rename_face(self, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            return
        if old_name in self.faces:
            face = self.faces.pop(old_name)
            face.name = new_name
            self.faces[new_name] = face
        if old_name in self.order:
            idx = self.order.index(old_name)
            self.order[idx] = new_name
        elif new_name not in self.order:
            self.order.append(new_name)
        if old_name in self.undefined_names:
            self.undefined_names.remove(old_name)
            self.undefined_names.add(new_name)
        self.dirty = True

    @classmethod
    def load(cls, path: str) -> "FaceBitmapsFile":
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: '{path}'")

        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()

        # Parse preamble up to #define FACE_LIST \
        preamble_match = re.search(r"^(.*?)#define\s+FACE_LIST\s*\\", text, re.DOTALL)
        if preamble_match:
            preamble = preamble_match.group(1)
        else:
            preamble = (
                "#pragma once\n\n"
                "#include <Arduino.h>\n\n"
                "// ======================================================================\n"
                "// --- FACE BITMAPS ---\n"
                "// Bitmaps for the faces are stored here, can be generated with image2cpp\n"
                "// Optional animated frames can be added by defining bitmaps with suffixes\n"
                "// _1, _2, _3, ... (example: epd_bitmap_walk_1).\n"
                "// ======================================================================\n\n"
                "// Add or remove faces here (single source of truth)\n"
            )

        # Parse FACE_LIST
        face_list_match = FACE_LIST_RE.search(text)
        if face_list_match:
            order = re.findall(r"X\((\w+)\)", face_list_match.group(1))
        else:
            order = []

        # Parse bitmaps
        raw_bitmaps: dict[str, bytes] = {}
        for m in BITMAP_RE.finditer(text):
            name = m.group(1)
            hex_bytes = HEX_RE.findall(m.group(2))
            if len(hex_bytes) == BYTES_PER_FRAME:
                raw_bitmaps[name] = bytes(int(h, 16) for h in hex_bytes)

        if not raw_bitmaps and not order:
            raise ValueError(f"No valid 128x64 face bitmaps or FACE_LIST found in '{path}'.")

        # Group bitmaps by base name and frame index
        grouped: dict[str, dict[int, bytes]] = {}
        for raw_name, data in raw_bitmaps.items():
            m = FRAME_SUFFIX_RE.match(raw_name)
            if m and (m.group(1) in raw_bitmaps or m.group(1) in order):
                base, idx = m.group(1), int(m.group(2))
            else:
                base, idx = raw_name, 0
            grouped.setdefault(base, {})[idx] = data

        fps_map = parse_fps_from_sibling_ino(path)

        faces: dict[str, Face] = {}
        defined_bases = set(grouped.keys())
        undefined_names = set(n for n in order if n not in defined_bases)

        # Add all defined faces
        for base, frame_map in grouped.items():
            ordered_frames = [frame_map[i] for i in sorted(frame_map.keys())]
            category = RESERVED_FACE_NAMES.get(
                base,
                FACE_CATEGORY_TALK if base.startswith("talk_") else FACE_CATEGORY_CUSTOM
            )
            faces[base] = Face(
                name=base,
                category=category,
                fps=fps_map.get(base, 4),
                frames=[bytearray(f) for f in ordered_frames],
                is_override=True,
            )

        # For undefined names in FACE_LIST (e.g. stand, defualt in original firmware),
        # initialize with a blank frame so they can be viewed and edited in the Face Library,
        # but track them in undefined_names so we don't emit a dummy bitmap unless edited.
        for name in undefined_names:
            category = RESERVED_FACE_NAMES.get(
                name,
                FACE_CATEGORY_TALK if name.startswith("talk_") else FACE_CATEGORY_CUSTOM
            )
            faces[name] = Face(
                name=name,
                category=category,
                fps=fps_map.get(name, 4),
                frames=[blank_frame()],
                is_override=True,
            )

        # Ensure all defined bases are in order
        for base in grouped:
            if base not in order:
                order.append(base)

        # Extract tail (weak frame declarations at the end of the file)
        tail_match = re.search(
            r"(\n(?://[^\n]*\n\s*)*#undef\s+const\s*\n\s*#define\s+DECLARE_FACE_FRAMES.*)",
            text,
            re.DOTALL,
        )
        if tail_match:
            tail = tail_match.group(1).lstrip()
        else:
            tail = (
                "// Optional animated frames (weak symbols). If a frame is not defined,\n"
                "// its pointer will be null and the animation will stop at the base frame.\n"
                "#undef const\n\n"
                "#define DECLARE_FACE_FRAMES(name) \\\n"
                "\textern const unsigned char epd_bitmap_##name##_1[] PROGMEM __attribute__((weak)); \\\n"
                "\textern const unsigned char epd_bitmap_##name##_2[] PROGMEM __attribute__((weak)); \\\n"
                "\textern const unsigned char epd_bitmap_##name##_3[] PROGMEM __attribute__((weak)); \\\n"
                "\textern const unsigned char epd_bitmap_##name##_4[] PROGMEM __attribute__((weak)); \\\n"
                "\textern const unsigned char epd_bitmap_##name##_5[] PROGMEM __attribute__((weak));\n\n"
                "#define X(name) DECLARE_FACE_FRAMES(name)\n"
                "FACE_LIST\n"
                "#undef X\n"
                "#undef DECLARE_FACE_FRAMES\n"
            )

        file_obj = cls(
            path=path,
            faces=faces,
            order=order,
            undefined_names=undefined_names,
            preamble=preamble,
            tail=tail,
        )
        file_obj.dirty = False
        return file_obj

    def serialize(self) -> str:
        lines: list[str] = []
        if self.preamble:
            lines.append(self.preamble.rstrip("\n"))
        else:
            lines.extend([
                "#pragma once",
                "",
                "#include <Arduino.h>",
                "",
                "// ======================================================================",
                "// --- FACE BITMAPS ---",
                "// Bitmaps for the faces are stored here, can be generated with image2cpp",
                "// Optional animated frames can be added by defining bitmaps with suffixes",
                "// _1, _2, _3, ... (example: epd_bitmap_walk_1).",
                "// ======================================================================",
                "",
                "// Add or remove faces here (single source of truth)",
            ])

        lines.append("#define FACE_LIST \\")
        # Build list of all names to include in FACE_LIST
        list_names = [n for n in self.order if n in self.faces or n in self.undefined_names]
        for n in self.faces:
            if n not in list_names:
                list_names.append(n)

        for i, name in enumerate(list_names):
            slash = " \\" if i < len(list_names) - 1 else ""
            lines.append(f"\tX({name}){slash}")

        lines.extend([
            "",
            "#define X(name) extern const unsigned char epd_bitmap_##name[] PROGMEM __attribute__((weak));",
            "FACE_LIST",
            "#undef X",
            "",
            "// Paste raw image2cpp output below. This remaps `const` so the symbols",
            "// have external linkage and can be weak-referenced safely.",
            "#define const extern const",
            "",
        ])

        # Bitmaps
        for name in list_names:
            if name in self.undefined_names:
                # Retained as weak symbol declaration in FACE_LIST without emitting empty bitmap
                continue
            face = self.faces.get(name)
            if not face or not face.frames:
                continue
            for f_idx, frame_data in enumerate(face.frames):
                suffix = "" if f_idx == 0 else f"_{f_idx}"
                sym = f"epd_bitmap_{name}{suffix}"
                lines.append(f"// '{name}{suffix}', 128x64px")
                lines.append(f"const unsigned char {sym} [] PROGMEM = {{")
                lines.append(format_byte_array(bytes(frame_data)))
                lines.append("\t};")
                lines.append("")

        if self.tail:
            lines.append(self.tail.rstrip("\n"))
        else:
            lines.extend([
                "// Optional animated frames (weak symbols). If a frame is not defined,",
                "// its pointer will be null and the animation will stop at the base frame.",
                "#undef const",
                "",
                "#define DECLARE_FACE_FRAMES(name) \\",
                "\textern const unsigned char epd_bitmap_##name##_1[] PROGMEM __attribute__((weak)); \\",
                "\textern const unsigned char epd_bitmap_##name##_2[] PROGMEM __attribute__((weak)); \\",
                "\textern const unsigned char epd_bitmap_##name##_3[] PROGMEM __attribute__((weak)); \\",
                "\textern const unsigned char epd_bitmap_##name##_4[] PROGMEM __attribute__((weak)); \\",
                "\textern const unsigned char epd_bitmap_##name##_5[] PROGMEM __attribute__((weak));",
                "",
                "#define X(name) DECLARE_FACE_FRAMES(name)",
                "FACE_LIST",
                "#undef X",
                "#undef DECLARE_FACE_FRAMES",
            ])
        lines.append("")
        return "\n".join(lines)

    def save(self, path: str | None = None) -> None:
        target_path = os.path.abspath(path if path else self.path)
        content = self.serialize()
        temp_path = f"{target_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(temp_path, target_path)
        self.path = target_path
        self.dirty = False
