"""tests/test_use_cases/test_skeleton.py — 验证 use case 层骨架可用。"""


def test_use_case_error_importable():
    from core.use_cases import UseCaseError
    err = UseCaseError("test error", code="TEST")
    assert err.message == "test error"
    assert err.code == "TEST"


def test_use_case_error_to_dict():
    from core.use_cases import UseCaseError
    err = UseCaseError("bad symbol", code="INVALID_SYMBOL")
    assert err.to_dict() == {"error": "bad symbol", "code": "INVALID_SYMBOL"}


def test_use_case_error_default_code():
    from core.use_cases import UseCaseError
    err = UseCaseError("oops")
    assert err.code == "USE_CASE_ERROR"


def test_use_case_error_is_exception():
    from core.use_cases import UseCaseError
    try:
        raise UseCaseError("boom")
    except Exception as e:
        assert isinstance(e, UseCaseError)
