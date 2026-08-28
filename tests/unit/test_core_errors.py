from __future__ import annotations

from graphrag.core.errors import AppError


def _all_subclasses(cls: type[AppError]) -> set[type[AppError]]:
    direct = set(cls.__subclasses__())
    return direct.union(*(_all_subclasses(sub) for sub in direct)) if direct else direct


def test_error_codes_unique() -> None:
    subclasses = _all_subclasses(AppError)
    assert subclasses, "expected AppError to have subclasses defined"
    codes = [sub.code for sub in subclasses]
    assert len(codes) == len(set(codes)), f"duplicate error codes found: {codes}"
