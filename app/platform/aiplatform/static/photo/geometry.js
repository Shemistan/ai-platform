// Геометрия кадра фото без DOM (FE-009): размер исходника под сетку модели и поля дорисовки.
//   source   — {width, height} исходной картинки;
//   pads     — {left, right, top, bottom} в процентах стороны (действуют только при outpaint);
//   outpaint — режим дорисовки полей (иначе поля нулевые);
//   grid     — шаг сетки модели, maxSide — предел стороны, workSide — рабочая длинная сторона.
// → {sw, sh, pad, cw, ch}: исходник в кадре, поля в px, итоговый холст.
export function geometry({ source, pads, outpaint, grid, maxSide, workSide }) {
  const snap = (v) => Math.round(v / grid) * grid;
  const fit = (v) => Math.min(Math.max(snap(v), 256), maxSide);
  const { width, height } = source;
  const frac = Object.fromEntries(Object.entries(pads).map(([side, pct]) => [side, outpaint ? pct / 100 : 0]));
  const longest = Math.max(width * (1 + frac.left + frac.right), height * (1 + frac.top + frac.bottom));
  const scale = Math.min(1, workSide / longest);
  const sw = fit(width * scale);
  const sh = fit(height * scale);
  const px = (fraction, side) => (fraction > 0 ? Math.max(grid, snap(side * fraction)) : 0);
  const pad = { left: px(frac.left, sw), right: px(frac.right, sw), top: px(frac.top, sh), bottom: px(frac.bottom, sh) };
  return { sw, sh, pad, cw: sw + pad.left + pad.right, ch: sh + pad.top + pad.bottom };
}
