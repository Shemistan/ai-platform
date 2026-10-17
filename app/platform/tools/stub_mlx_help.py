"""`--help` консольного Python-скрипта пайплайна без Metal (QA-005).

mflux, ltx-2-mlx и pocketai-wan-video импортируют mlx при старте, а mlx без GPU
(песочница, CI, ssh без сессии) падает раньше, чем argparse напечатает справку.
Здесь модули mlx* подменены пустышками: парсер собирается настоящий, справка — его.
Запуск: <python окружения> stub_mlx_help.py <скрипт> [подкоманда] --help.
STUB_KEEP=a,b — свои пакеты пайплайна с префиксом mlx (mlx_video у Wan): их не подменять.
"""

import importlib.abc
import importlib.machinery
import os
import runpy
import sys
import types
from unittest import mock

KEEP = set(filter(None, os.environ.get("STUB_KEEP", "").split(",")))


class _Meta(type):
    """Любой атрибут пустышки — класс: годится в аннотации, базовые классы и вызовы."""

    def __getattr__(cls, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Meta(attr, (), {})

    def __call__(cls, *args, **kwargs):
        return mock.MagicMock(name=cls.__name__)

    def __getitem__(cls, item):
        return cls

    def __or__(cls, other):
        return cls

    __ror__ = __or__


class _Stub(importlib.abc.Loader, importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top.startswith("mlx") and top not in KEEP:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []

        def attr(name):
            if name.startswith("__"):
                raise AttributeError(name)
            return _Meta(name, (), {})

        module.__getattr__ = attr
        return module

    def exec_module(self, module):
        pass


if __name__ == "__main__":
    sys.meta_path.insert(0, _Stub())
    script = sys.argv[1]
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")
