// Чистые функции экрана подтверждения монтажа: фразы, вырезки и планы по индексам слов. Без DOM — тесты в node.

export const PHRASE_GAP_S = 0.45; // как plan.PHRASE_GAP_S на сервере

// Слова → фразы: разрыв на паузе или после точки. { first, last, start, end, gapBefore }.
export function phrases(words) {
  const out = [];
  words.forEach((word, i) => {
    const prev = words[i - 1];
    const gap = prev ? word.s - prev.e : word.s;
    if (!prev || gap >= PHRASE_GAP_S || /[.!?…]$/.test(prev.w)) {
      out.push({ first: i, last: i, start: word.s, end: word.e, gapBefore: gap });
    } else {
      const p = out[out.length - 1];
      p.last = i;
      p.end = word.e;
    }
  });
  return out;
}

// Для каждого слова — индекс вырезки, в которую оно попало, или -1.
export function cutOf(cuts, count) {
  const map = new Array(count).fill(-1);
  cuts.forEach((cut, n) => {
    for (let i = cut.from; i <= cut.to && i < count; i++) map[i] = n;
  });
  return map;
}

const sorted = (cuts) => [...cuts].sort((a, b) => a.from - b.from || a.to - b.to);

// Клик по слову: в вырезке — вырезка снимается целиком; нет — новая вырезка «человек» на это слово.
// С extend (Shift) — последняя своя вырезка тянется до слова.
export function toggleWord(cuts, index, { extend = false, last = null } = {}) {
  const hit = cuts.findIndex((c) => c.from <= index && index <= c.to);
  if (hit >= 0 && !extend) return cuts.filter((_, n) => n !== hit);
  if (extend && last) {
    const from = Math.min(last.from, index);
    const to = Math.max(last.to, index);
    const rest = cuts.filter((c) => c !== last && (c.to < from || c.from > to));
    return sorted([...rest, { ...last, from, to }]);
  }
  if (hit >= 0) return cuts;
  return sorted([...cuts, { from: index, to: index, kind: "human", reason: "вырезал человек" }]);
}

// Фраза целиком: вся вырезана — восстановить; иначе вырезать целиком (пересекающиеся вырезки поглощаются).
export function togglePhrase(cuts, phrase) {
  const map = cutOf(cuts, phrase.last + 1);
  let whole = true;
  for (let i = phrase.first; i <= phrase.last; i++) if (map[i] < 0) whole = false;
  // Части вырезок, вылезающие за фразу, остаются как были.
  const kept = [];
  for (const c of cuts) {
    if (c.to < phrase.first || c.from > phrase.last) kept.push(c);
    else {
      if (c.from < phrase.first) kept.push({ ...c, to: phrase.first - 1 });
      if (c.to > phrase.last) kept.push({ ...c, from: phrase.last + 1 });
    }
  }
  if (whole) return sorted(kept);
  return sorted([...kept, { from: phrase.first, to: phrase.last, kind: "human", reason: "вырезал человек" }]);
}

// План с начала фразы: отметка ставится (или меняется) на первом слове фразы.
export function setLayout(layout, from, mode) {
  const rest = layout.filter((m) => m.from !== from);
  const marks = [...rest, { from, layout: mode }].sort((a, b) => a.from - b.from);
  const out = [];
  for (const m of marks) if (!out.length || out[out.length - 1].layout !== m.layout) out.push(m);
  if (!out.length || out[0].from !== 0) out.unshift({ from: 0, layout: "head" });
  return out;
}

export function layoutAt(layout, index) {
  let current = "head";
  for (const m of layout) {
    if (m.from > index) break;
    current = m.layout;
  }
  return current;
}

// 83.4 → «1:23»; 3725 → «1:02:05».
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// Тело запроса «Новый ролик» из формы; ошибка — текстом.
// Сводка свёрнутого «Как монтировать» (UX-033): «Whisper Large V3 Turbo · пауза до 0,6 с · проверка · С подтверждением».
export function setupSummary(options, { models = [], modes = [] } = {}) {
  const model = models.find((m) => m.id === options.model)?.label || options.model;
  const pause = `пауза до ${String(options.max_pause).replace(".", ",")} с`;
  const mode = modes.find((m) => m.id === options.mode)?.label;
  return [model, pause, options.check ? "проверка" : null, options.subtitles ? "субтитры" : null, options.motion ? "моушн" : null, mode]
    .filter(Boolean)
    .join(" · ");
}

export function projectPayload({ title, model, terms, grade, auto, subtitles, check, motion, maxPause }) {
  const pause = Number(String(maxPause ?? "").replace(",", "."));
  if (!Number.isFinite(pause) || pause < 0.3 || pause > 3) return { error: "Самая длинная пауза — от 0,3 до 3 секунд." };
  if ((terms || "").length > 2000) return { error: "Термины — до 2000 знаков." };
  return {
    payload: {
      title: (title || "").trim(),
      options: { model, terms: (terms || "").trim(), grade, auto: !!auto, subtitles: !!subtitles, check: !!check, motion: !!motion, max_pause: pause },
    },
  };
}
