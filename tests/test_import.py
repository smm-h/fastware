def test_import_fastware():
    import fastware
    assert hasattr(fastware, "__version__")
    assert hasattr(fastware, "Router")
    assert hasattr(fastware, "create_app")
