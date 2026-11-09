import pytest

from supportcore.registry import ProviderRegistry, UnknownProviderError


def test_registry_creates_registered_provider():
    reg = ProviderRegistry[str]("greeter")

    @reg.register("hello")
    def make(name):
        return f"hello {name}"

    assert reg.create("hello", name="x") == "hello x"
    assert reg.keys() == ["hello"]


def test_registry_unknown_key_lists_available():
    reg = ProviderRegistry[str]("greeter")
    reg.register("a")(lambda: "a")
    with pytest.raises(UnknownProviderError) as e:
        reg.create("b")
    assert "a" in str(e.value) and "greeter" in str(e.value)
    assert e.value.port_name == "greeter"
    assert e.value.key == "b"
    assert e.value.available == ["a"]


def test_registry_duplicate_key_raises():
    reg = ProviderRegistry[str]("greeter")
    reg.register("a")(lambda: "a")
    with pytest.raises(ValueError):
        reg.register("a")(lambda: "other")
