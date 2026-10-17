// Загрузка модулей static/ в node без package.json: исходник импортируется как data:-URL,
// поэтому файл .js с export читается как ES-модуль. Подходит для модулей без своих import.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../aiplatform/static/", import.meta.url));

export async function loadStatic(name) {
  const source = readFileSync(STATIC + name, "utf8");
  return import(`data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`);
}
