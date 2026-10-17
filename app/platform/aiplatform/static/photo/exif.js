// Ориентация JPEG из EXIF и загрузка фото с поворотом в любом браузере (FE-011).
// Без import: модуль грузится и в node-тестах (tests/js/exif.test.mjs).

// Сколько начала файла читать: APP1 с EXIF стоит в начале JPEG и не длиннее 64 КБ.
const HEAD_BYTES = 128 * 1024;

// exifOrientation(ArrayBuffer начала файла) → 1…8 (тег 0x0112 в IFD0) или 1, если тега нет / не JPEG.
export function exifOrientation(buffer) {
  const view = new DataView(buffer);
  if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) return 1;
  let pos = 2;
  while (pos + 4 <= view.byteLength) {
    const marker = view.getUint16(pos);
    if ((marker & 0xff00) !== 0xff00 || marker === 0xffda) return 1; // данные кадра — EXIF уже не будет
    const length = view.getUint16(pos + 2);
    if (marker === 0xffe1 && pos + 10 <= view.byteLength && view.getUint32(pos + 4) === 0x45786966 && view.getUint16(pos + 8) === 0) {
      return tiffOrientation(view, pos + 10, Math.min(pos + 2 + length, view.byteLength));
    }
    pos += 2 + length;
  }
  return 1;
}

function tiffOrientation(view, start, end) {
  if (start + 8 > end) return 1;
  const order = view.getUint16(start);
  if (order !== 0x4949 && order !== 0x4d4d) return 1;
  const little = order === 0x4949;
  const ifd = start + view.getUint32(start + 4, little);
  if (ifd + 2 > end) return 1;
  const count = view.getUint16(ifd, little);
  for (let i = 0; i < count; i++) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > end) return 1;
    if (view.getUint16(entry, little) === 0x0112) {
      const value = view.getUint16(entry + 8, little);
      return value >= 1 && value <= 8 ? value : 1;
    }
  }
  return 1;
}

// loadOriented(file) → { image, width, height, close() }: image годится для drawImage, размеры — уже
// после EXIF-поворота. Фото с поворотом (iPhone в портрете) идёт через <img> + decode(): поворот к
// <img> браузеры применяют сами (Safari 13.1+, Chrome 81+, Firefox 77+), а опцию imageOrientation у
// createImageBitmap старый Safari (до 17) не знает и отдаёт кадр боком. Остальное — createImageBitmap.
export async function loadOriented(file) {
  let orientation = 1;
  try {
    orientation = exifOrientation(await file.slice(0, HEAD_BYTES).arrayBuffer());
  } catch {
    /* не прочитали начало — считаем, что поворота нет */
  }
  if (orientation === 1) {
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      return { image: bitmap, width: bitmap.width, height: bitmap.height, close: () => bitmap.close() };
    } catch {
      /* опция или формат не поддержаны — пробуем через <img> */
    }
  }
  // Адрес blob: живёт, пока фото на холсте: отзываем в close().
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.src = url;
  try {
    await img.decode();
  } catch (err) {
    URL.revokeObjectURL(url);
    throw err;
  }
  return { image: img, width: img.naturalWidth, height: img.naturalHeight, close: () => URL.revokeObjectURL(url) };
}
