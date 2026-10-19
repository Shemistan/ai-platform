"""Итеративное ревью готового ролика (ITERATIVE_REVIEW_SPEC, PRD-028, ADR 0012).

Запуск (`state.py`) → раунды: бандл (`bundle.py`) → роли (`reviewers.py`) → замечания
(`issues.py`) → план (`planner.py`) → правки и пересборка (`repair.py`) → сравнение
(`compare.py`) → версия принята или откат (`versions.py`). Оркестрация — `engine.py`,
политика и бюджеты — `policy.py`.
"""
