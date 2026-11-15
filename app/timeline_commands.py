"""QUndoCommands for every timeline edit.

Commands hold the ``Sequence`` object itself (identity survives renames and
delete/undelete) and mutate ``seq.frames`` in place. Each redo()/undo() ends
by calling ``timeline._command_applied(cmd, undo, first)`` which refreshes
only what that kind of edit can have changed; ``first`` is True for the
redo() that QUndoStack.push() runs, so live edits (slider drags) stay O(1)
and never re-announce the selection, while real undo/redo steps do.
"""

from __future__ import annotations

from PySide6.QtGui import QUndoCommand

MERGE_WINDOW_MS = 400

ID_ANGLES = 0x5E01
ID_FIELDS = 0x5E02


class _TimelineCommand(QUndoCommand):
    kind = "structure"      # "angles" | "fields" | "delays" | "structure" | "sequences"

    def __init__(self, timeline, seq, text: str):
        super().__init__(text)
        self.tl = timeline
        self.seq = seq
        self._first = True
        # (indices, primary) to select after redo / undo; None = leave as is
        self.sel_redo = None
        self.sel_undo = None

    def _apply(self) -> None:
        raise NotImplementedError

    def _revert(self) -> None:
        raise NotImplementedError

    def redo(self) -> None:
        self._apply()
        first, self._first = self._first, False
        self.tl._command_applied(self, False, first)

    def undo(self) -> None:
        self._revert()
        self.tl._command_applied(self, True, False)


class _MergeableFrameEdit(_TimelineCommand):
    """Shared merge rule: same frame, and either the same explicit
    begin_edit()/end_edit() bracket or (no bracket) within MERGE_WINDOW_MS of
    the previous edit -- so one slider drag is one undo step."""

    def __init__(self, timeline, seq, index, old, new, text, group, stamp_ms):
        super().__init__(timeline, seq, text)
        self.index = index
        self.old = old
        self.new = new
        self.group = group
        self.stamp = stamp_ms
        self.sel_redo = self.sel_undo = ([index], index)

    def mergeWith(self, other) -> bool:
        if type(other) is not type(self) or other.seq is not self.seq or other.index != self.index:
            return False
        if self.group is not None or other.group is not None:
            if self.group != other.group:
                return False
        elif other.stamp - self.stamp > MERGE_WINDOW_MS:
            return False
        self.new = other.new
        self.stamp = other.stamp
        self._merged(other)
        return True

    def _merged(self, other) -> None:
        pass


class SetAnglesCommand(_MergeableFrameEdit):
    kind = "angles"

    def id(self) -> int:
        return ID_ANGLES

    def _apply(self) -> None:
        self.seq.frames[self.index].angles = dict(self.new)

    def _revert(self) -> None:
        self.seq.frames[self.index].angles = dict(self.old)


class SetFieldsCommand(_MergeableFrameEdit):
    """old/new are (delay_ms, face, face_mode)."""
    kind = "fields"

    def __init__(self, *args):
        super().__init__(*args)
        self.delay_changed = self.old[0] != self.new[0]
        self.face_changed = self.old[1:] != self.new[1:]

    def id(self) -> int:
        return ID_FIELDS

    def _merged(self, other) -> None:
        # The flags of a freshly pushed command describe just that step (its
        # redo runs before the merge); once merged they must describe the
        # whole old -> new span for later undo/redo refreshes.
        self.delay_changed = self.old[0] != self.new[0]
        self.face_changed = self.old[1:] != self.new[1:]

    def _set(self, values) -> None:
        f = self.seq.frames[self.index]
        f.delay_ms, f.face, f.face_mode = values

    def _apply(self) -> None:
        self._set(self.new)

    def _revert(self) -> None:
        self._set(self.old)


class SetDelaysCommand(_TimelineCommand):
    """Retime one or more frames ({index: delay_ms})."""
    kind = "delays"

    def __init__(self, timeline, seq, old: dict, new: dict, primary, text="Retime Frame"):
        super().__init__(timeline, seq, text)
        self.old = dict(old)
        self.new = dict(new)
        self.sel_redo = self.sel_undo = (sorted(self.new), primary)

    def _apply(self) -> None:
        for i, d in self.new.items():
            self.seq.frames[i].delay_ms = d

    def _revert(self) -> None:
        for i, d in self.old.items():
            self.seq.frames[i].delay_ms = d


class InsertFramesCommand(_TimelineCommand):
    def __init__(self, timeline, seq, index: int, frames: list, text: str, sel_undo=None):
        super().__init__(timeline, seq, text)
        self.index = index
        self.frames = list(frames)
        last = index + len(self.frames) - 1
        self.sel_redo = (list(range(index, last + 1)), last)
        self.sel_undo = sel_undo

    def _apply(self) -> None:
        self.seq.frames[self.index:self.index] = self.frames

    def _revert(self) -> None:
        del self.seq.frames[self.index:self.index + len(self.frames)]


class RemoveFramesCommand(_TimelineCommand):
    def __init__(self, timeline, seq, indices, text: str, sel_undo=None):
        super().__init__(timeline, seq, text)
        self.indices = sorted(set(indices))
        self.removed: list = []
        remaining = len(seq.frames) - len(self.indices)
        keep = max(0, min(self.indices[0], remaining - 1))
        self.sel_redo = ([keep], keep) if remaining > 0 else ([], None)
        self.sel_undo = sel_undo or (list(self.indices), self.indices[-1])

    def _apply(self) -> None:
        frames = self.seq.frames
        self.removed = [frames[i] for i in self.indices]
        for i in reversed(self.indices):
            del frames[i]

    def _revert(self) -> None:
        frames = self.seq.frames
        for i, f in zip(self.indices, self.removed):
            frames.insert(i, f)


class MoveFramesCommand(_TimelineCommand):
    """Reorder by permutation: new_frames[k] = old_frames[perm[k]]."""

    def __init__(self, timeline, seq, perm: list, text: str, sel_redo, sel_undo):
        super().__init__(timeline, seq, text)
        self.perm = list(perm)
        self.sel_redo = sel_redo
        self.sel_undo = sel_undo

    def _apply(self) -> None:
        frames = self.seq.frames
        frames[:] = [frames[p] for p in self.perm]

    def _revert(self) -> None:
        frames = self.seq.frames
        old = [None] * len(frames)
        for k, p in enumerate(self.perm):
            old[p] = frames[k]
        frames[:] = old


# -- sequence-level commands ---------------------------------------------------

class _SequenceCommand(_TimelineCommand):
    kind = "sequences"

    def __init__(self, timeline, seq, text: str):
        super().__init__(timeline, seq, text)
        self.show_redo = None   # sequence name to display after redo / undo
        self.show_undo = None

    def _restore_order(self, items) -> None:
        d = self.tl.sequences
        d.clear()
        d.update(items)


class AddSequenceCommand(_SequenceCommand):
    def __init__(self, timeline, seq, text: str, shown_before):
        super().__init__(timeline, seq, text)
        self.show_redo = seq.name
        self.show_undo = shown_before

    def _apply(self) -> None:
        self.tl.sequences[self.seq.name] = self.seq

    def _revert(self) -> None:
        self.tl.sequences.pop(self.seq.name, None)


class DeleteSequenceCommand(_SequenceCommand):
    def __init__(self, timeline, seq, show_after):
        super().__init__(timeline, seq, f"Delete Sequence '{seq.name}'")
        self._order = list(timeline.sequences.items())
        self.show_redo = show_after
        self.show_undo = seq.name

    def _apply(self) -> None:
        self.tl.sequences.pop(self.seq.name, None)

    def _revert(self) -> None:
        self._restore_order(self._order)


class RenameSequenceCommand(_SequenceCommand):
    def __init__(self, timeline, seq, new_name: str):
        super().__init__(timeline, seq, f"Rename Sequence to '{new_name}'")
        self.old_name = seq.name
        self.new_name = new_name
        self.show_redo = new_name
        self.show_undo = self.old_name

    def _rekey(self, src: str, dst: str) -> None:
        items = [(dst if k == src else k, v) for k, v in self.tl.sequences.items()]
        self.seq.name = dst
        self._restore_order(items)
        self.tl._sequence_renamed(src, dst)

    def _apply(self) -> None:
        self._rekey(self.old_name, self.new_name)

    def _revert(self) -> None:
        self._rekey(self.new_name, self.old_name)
