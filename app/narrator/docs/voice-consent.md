# Живой голос: образец и согласие владельца

Голос живого человека — его личные данные. По образцу в 10 секунд модель
говорит этим голосом что угодно, поэтому образец не лежит в репозитории, а
клон без записи о согласии не строится (CMP-009).

<!-- Сверить человеку: достаточно ли записи в файле как подтверждения
     согласия (152-ФЗ ст. 9, биометрия — ст. 11; GDPR art. 7, 9), срок и
     форма согласия для чужого голоса. -->

## Где лежат образцы

| Голос | Чей | Где образец | Согласие |
|---|---|---|---|
| M1 | синтетический (Supertonic) | `src/narrator/voices/` в пакете | не нужно |
| Charon | синтетический (Gemini TTS) | пакет; перезаписанный — `<папка моделей>/voices/` | не нужно |
| Shem, Shem-v2, Shem-v3 | живой человек | только `<папка моделей>/voices/` | `<имя>.consent.json` рядом |

Папка моделей — `$NARRATOR_MODELS_DIR`, по умолчанию
`/Volumes/External SSD/local_models`. Файлы `voices/shem*` в пакете
запрещены `.gitignore`.

## Запись о согласии

`shem.wav` → `shem.consent.json` в той же папке:

```json
{
  "version": 1,
  "kind": "live",
  "owner": "чей голос",
  "date": "2026-10-17",
  "form": "self",
  "purposes": ["озвучка статей и дайджестов narrator"],
  "expires": null
}
```

- `form`: `self` — это мой голос; `written` — у меня письменное согласие
  владельца. Сам документ храните у себя, в файл кладите только отметку.
- `expires` — до какого дня можно клонировать (`ГГГГ-ММ-ДД`), `null` — бессрочно.
  После этой даты клон не строится.
- `kind: synthetic` — ничей голос. Встроенным M1 и Charon файл не нужен.

Создать запись:

- записываете образец: `./narrate voice shem --from запись.wav --text «…»
  --owner «Имя» --form self`. Платформа (вкладка «Аудио») делает так же и
  не отправит форму без галочки согласия;
- образец уже лежит в папке: `./narrate voice-consent shem --owner «Имя»
  --form self`.

Без записи синтез голосом Shem останавливается с ошибкой «клонировать без
согласия владельца нельзя». Удалить голос — удалить из `<папка моделей>/voices/`
файлы `<имя>.wav`, `<имя>.txt` и `<имя>.consent.json`.

## Перенос старых образцов (один раз, делает человек)

До CMP-009 `shem.wav`, `shem-v2.wav` и их `.txt` лежали в пакете. После
слияния они пропадут из рабочей копии. Перенесите их из истории git в папку
моделей и запишите согласие:

```sh
V="/Volumes/External SSD/local_models/voices"; mkdir -p "$V"
for f in shem.wav shem.txt shem-v2.wav shem-v2.txt; do
  git show 067ecfa:app/narrator/src/narrator/voices/$f > "$V/$f"
done
./narrate voice-consent shem --owner «Имя» --form self
./narrate voice-consent shem-v2 --owner «Имя» --form self
```

## История git (решение человека)

Файлы удалены из текущей версии, но остаются в прошлых коммитах и на
GitHub. Удалить их оттуда можно только переписав историю:

1. Проверьте видимость репозитория: `gh repo view Shemistan/ai-platform
   --json visibility`. Закрытый репозиторий без посторонних — переписывать
   не обязательно.
2. Публичный, или доступ есть у посторонних — переписать историю:
   ```sh
   git filter-repo --invert-paths \
     --path app/narrator/src/narrator/voices/shem.wav \
     --path app/narrator/src/narrator/voices/shem.txt \
     --path app/narrator/src/narrator/voices/shem-v2.wav \
     --path app/narrator/src/narrator/voices/shem-v2.txt
   git push --force --all && git push --force --tags
   ```
   Это меняет хэши всех коммитов: остальные копии репозитория придётся
   клонировать заново, открытые PR — пересоздать. Форки и кэш GitHub так
   не чистятся, для них нужно обращение в поддержку GitHub.
