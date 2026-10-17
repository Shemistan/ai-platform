// Расчёт ключевых кадров видео без DOM (FE-009): длина ролика в кадрах, сколько кадров влезает,
// равномерная расстановка и проверка позиций. Страница — video/app.js, тесты — tests/js/frames.test.mjs.

// Кадров в ролике: duration секунд при fps плюс нулевой.
export const frameCount = (duration, fps) => duration * fps + 1;

// Сколько ключевых кадров влезает: не ближе gap кадров друг к другу и не больше лимита модели.
export const keyframeLimit = (total, gap, maxKeyframes = Infinity) => Math.min(Math.floor((total - 1) / gap) + 1, maxKeyframes ?? Infinity);

// Равномерные позиции count кадров от 0 до last; один кадр — в начале.
export function spread(count, last) {
  return Array.from({ length: count }, (_, i) => (count === 1 ? 0 : Math.round((i * last) / (count - 1))));
}

// Позиция кадра по секундам из поля ввода («1,5» тоже): null — не число.
export function frameAt(value, fps) {
  const sec = Number(String(value).replace(",", "."));
  return Number.isFinite(sec) ? Math.max(0, Math.round(sec * fps)) : null;
}

// Что не так с каждым кадром (null — всё в порядке): frames — [{frameIdx, error}].
export function frameProblems(frames, { gap, last, fps }) {
  return frames.map((frame, i) => {
    if (frame.error) return frame.error;
    if (i === 0) return frame.frameIdx === 0 ? null : "Первый кадр всегда в начале";
    if (frame.frameIdx > last) return "Позже конца ролика";
    if (frame.frameIdx - frames[i - 1].frameIdx < gap) {
      return `Нужно позже кадра ${i} хотя бы на ${(gap / fps).toFixed(2)} с`;
    }
    return null;
  });
}
