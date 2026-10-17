"""Изоляция тестов от боевых данных и диска моделей.

Без AIP_DATA_DIR платформа берёт app/platform/data — там настройки, накликанные
во вкладке «Настройки», а без AIP_MODELS_DIR — внешний диск с весами. Тесты не
должны ни зависеть от них, ни писать туда: каталог данных — временный, каталог
моделей — несуществующий, а config.load под AIP_TEST_GUARD откажет на боевом пути.
"""

import atexit
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="aip-tests-")
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)

MISSING_MODELS_DIR = os.path.join(_DATA_DIR, "no-models")  # не создаётся: моделей нет

TMP_ROOT = _DATA_DIR

# make экспортирует переменные из local.mk и пустыми — пустое считаем незаданным.
for _key, _value in (("AIP_DATA_DIR", _DATA_DIR), ("AIP_MODELS_DIR", MISSING_MODELS_DIR)):
    if not os.environ.get(_key):
        os.environ[_key] = _value
os.environ["AIP_TEST_GUARD"] = "1"
