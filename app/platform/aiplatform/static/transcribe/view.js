// «Расшифровка»: подписи времени и размера — без DOM, проверяются node --test.

// Секунды → «м:сс» или «ч:мм:сс», как таймкоды абзацев в text.md (transcribe/formats.py clock).
export function clock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

// Байты → «820 КБ», «14,5 МБ», «1,2 ГБ».
export function sizeLabel(bytes) {
  const n = Number(bytes) || 0;
  const units = [["ГБ", 2 ** 30], ["МБ", 2 ** 20], ["КБ", 2 ** 10]];
  for (const [unit, size] of units) {
    if (n >= size) {
      const value = n / size;
      return `${value >= 100 ? Math.round(value) : value.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ${unit}`;
    }
  }
  return `${n} Б`;
}
