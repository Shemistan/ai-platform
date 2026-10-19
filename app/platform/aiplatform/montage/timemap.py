"""TimeMap (MASTER_SPEC §4): результат в целых кадрах ↔ исходник в секундах (PTS).

Результат идёт на сетке кадров render.FPS: сегмент занимает round(длительность × fps) кадров,
границы следующего считаются от целых кадров предыдущего — накопления float нет. Исходник
хранит свои секунды (PTS из расшифровки и EDL). Слова и сцены привязываются к кадрам результата
через эту карту; после правки плана карта строится заново (инвалидация, §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiplatform.montage import edl as edl_module
from aiplatform.montage import render
from aiplatform.montage.schemas import SCHEMA_VERSION


@dataclass(frozen=True)
class Segment:
    source_id: str
    source_pts_start: float
    source_pts_end: float
    edit_frame_start: int
    edit_frame_end: int  # не включая
    layout: str = "head"
    first_word: int = 0
    last_word: int = 0
    speed: float = 1.0
    transition: str = "cut"

    @property
    def frames(self) -> int:
        return self.edit_frame_end - self.edit_frame_start

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "source_pts_start": round(self.source_pts_start, 4),
            "source_pts_end": round(self.source_pts_end, 4),
            "edit_frame_start": self.edit_frame_start,
            "edit_frame_end": self.edit_frame_end,
            "speed": self.speed,
            "transition": self.transition,
            "layout": self.layout,
            "first_word": self.first_word,
            "last_word": self.last_word,
        }


@dataclass
class TimeMap:
    fps: int = render.FPS
    source_duration: float = 0.0
    segments: list[Segment] = field(default_factory=list)

    # ---------- построение ----------

    @classmethod
    def from_edl(cls, edl: edl_module.Edl, source_id: str = "head", *, fps: int = render.FPS,
                 source_duration: float = 0.0) -> TimeMap:  # fmt: skip
        """Сегменты EDL (секунды исходника) → целые кадры результата подряд, без дыр."""
        out = cls(fps=fps, source_duration=source_duration)
        frame = 0
        for seg in edl.segments:
            n = max(1, round((seg.end - seg.start) * fps))
            out.segments.append(
                Segment(source_id, seg.start, seg.end, frame, frame + n, seg.layout, seg.first, seg.last)
            )
            frame += n
        return out

    @classmethod
    def load(cls, data: object) -> TimeMap:
        src = data if isinstance(data, dict) else {}
        out = cls(fps=int(src.get("fps") or render.FPS), source_duration=float(src.get("source_duration") or 0))
        for item in src.get("segments") or []:
            out.segments.append(Segment(
                str(item["source_id"]), float(item["source_pts_start"]), float(item["source_pts_end"]),
                int(item["edit_frame_start"]), int(item["edit_frame_end"]), str(item.get("layout", "head")),
                int(item.get("first_word", 0)), int(item.get("last_word", 0)),
                float(item.get("speed", 1.0)), str(item.get("transition", "cut")),
            ))  # fmt: skip
        return out

    def as_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "fps": self.fps,
            "source_duration": round(self.source_duration, 3),
            "duration_frames": self.duration_frames,
            "segments": [s.as_dict() for s in self.segments],
        }

    # ---------- свойства ----------

    @property
    def duration_frames(self) -> int:
        return self.segments[-1].edit_frame_end if self.segments else 0

    @property
    def duration_s(self) -> float:
        return self.duration_frames / self.fps

    def frame_of(self, seconds: float) -> int:
        """Секунды результата → кадр (к ближайшему)."""
        return max(0, min(self.duration_frames, round(seconds * self.fps)))

    # ---------- преобразования ----------

    def to_edit_frame(self, source_s: float) -> int | None:
        """Секунда исходника → кадр результата; None — это место вырезано."""
        for seg in self.segments:
            if seg.source_pts_start <= source_s < seg.source_pts_end or (
                seg is self.segments[-1] and abs(source_s - seg.source_pts_end) < 1e-9
            ):
                share = (source_s - seg.source_pts_start) / (seg.source_pts_end - seg.source_pts_start or 1)
                return min(seg.edit_frame_end - 1, seg.edit_frame_start + int(share * seg.frames))
        return None

    def to_source(self, edit_frame: int) -> tuple[str, float] | None:
        """Кадр результата → (источник, секунда исходника); None — за концом."""
        for seg in self.segments:
            if seg.edit_frame_start <= edit_frame < seg.edit_frame_end:
                share = (edit_frame - seg.edit_frame_start) / seg.frames
                return seg.source_id, seg.source_pts_start + share * (seg.source_pts_end - seg.source_pts_start)
        return None

    def words_in_edit(self, words: list[dict]) -> list[dict]:
        """Слова исходника ({w, s, e}) → слова результата с кадрами: {w, s, e, src, start_frame, end_frame}.

        Вырезанные слова пропускаются; слово на границе сегмента обрезается по сегменту.
        """
        out: list[dict] = []
        for seg in self.segments:
            for i in range(seg.first_word, seg.last_word + 1):
                w = words[i]
                s = max(w["s"], seg.source_pts_start)
                e = min(w["e"], seg.source_pts_end)
                if e < s:
                    continue
                offset = seg.edit_frame_start / self.fps
                start = offset + (s - seg.source_pts_start)
                end = offset + (e - seg.source_pts_start)
                out.append({
                    "w": w["w"], "s": round(start, 3), "e": round(max(end, start), 3), "src": i,
                    "start_frame": self.frame_of(start), "end_frame": max(self.frame_of(start), self.frame_of(end)),
                })  # fmt: skip
        return out

    # ---------- инварианты ----------

    def check(self) -> list[str]:
        """Нарушения §4: отрицательные интервалы, пересечения, выход за исходник, разрыв кадров."""
        out: list[str] = []
        frame = 0
        for n, seg in enumerate(self.segments):
            if seg.source_pts_end <= seg.source_pts_start:
                out.append(f"сегмент {n}: отрицательный интервал исходника")
            if seg.edit_frame_end <= seg.edit_frame_start:
                out.append(f"сегмент {n}: отрицательный интервал результата")
            if seg.edit_frame_start != frame:
                out.append(f"сегмент {n}: кадры не подряд ({seg.edit_frame_start} вместо {frame})")
            frame = seg.edit_frame_end
            if seg.source_pts_start < 0 or (self.source_duration and seg.source_pts_end > self.source_duration + 1e-6):
                out.append(f"сегмент {n}: за границами исходника")
            prev = self.segments[n - 1] if n else None
            if prev and seg.source_id == prev.source_id and seg.source_pts_start < prev.source_pts_end - 1e-6:
                out.append(f"сегмент {n}: пересекается с предыдущим в исходнике")
            expected = round((seg.source_pts_end - seg.source_pts_start) * self.fps)
            if abs(seg.frames - expected) > 1 and seg.speed == 1.0:
                out.append(f"сегмент {n}: {seg.frames} кадров при длине {expected} — рассинхрон звука и видео")
        return out
