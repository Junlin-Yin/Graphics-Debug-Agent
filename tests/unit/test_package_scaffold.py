import debug_agent


def test_package_exposes_version() -> None:
    assert debug_agent.__version__ == "0.1.0"
