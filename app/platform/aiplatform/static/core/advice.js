// Советы по настройке (UX-030): текст вида «что не так — команда.» → команда для кнопки «Скопировать».
// Нет команды после последнего « — » — null, кнопка не нужна.
export function adviceCommand(text) {
  const at = text.lastIndexOf(" — ");
  if (at < 0) return null;
  const command = text.slice(at + 3).trim().replace(/\.$/, "");
  return command || null;
}
