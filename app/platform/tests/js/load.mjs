// Загрузка модулей static/ в node без package.json: исходник импортируется как data:-URL,
// поэтому файл .js с export читается как ES-модуль. Свои import модуля ("./core/format.js",
// "/ui.js") переписываются на data:-URL того же файла из static/ — рекурсивно; один файл —
// один URL, значит и один экземпляр модуля на все тесты процесса.
import { readFileSync } from "node:fs";
import { posix } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../aiplatform/static/", import.meta.url));
const urls = new Map();

function dataUrl(name) {
  if (urls.has(name)) return urls.get(name);
  const source = readFileSync(STATIC + name, "utf8").replace(
    /(\bfrom\s*|\bimport\s*\(?\s*)(["'])([./][^"']*)\2/g,
    (_, head, quote, spec) => {
      const target = spec.startsWith("/") ? spec.slice(1) : posix.normalize(posix.join(posix.dirname(name), spec));
      return `${head}${quote}${dataUrl(target)}${quote}`;
    },
  );
  const url = `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`;
  urls.set(name, url);
  return url;
}

export async function loadStatic(name) {
  return import(dataUrl(name));
}
