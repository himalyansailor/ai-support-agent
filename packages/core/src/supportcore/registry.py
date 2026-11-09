from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class UnknownProviderError(KeyError):
    def __init__(self, port_name: str, key: str, available: list[str]):
        self.port_name = port_name
        self.key = key
        self.available = available
        super().__init__(port_name, key, available)

    def __str__(self) -> str:
        return (
            f"Unknown provider {self.key!r} for port {self.port_name!r}; "
            f"available: {', '.join(self.available) or '(none)'}"
        )


class ProviderRegistry(Generic[T]):
    """Maps provider keys to factories for one port (e.g. vector store, LLM)."""

    def __init__(self, port_name: str):
        self.port_name = port_name
        self._factories: dict[str, Callable[..., T]] = {}

    def register(self, key: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
        def decorator(factory: Callable[..., T]) -> Callable[..., T]:
            if key in self._factories:
                raise ValueError(f"Provider {key!r} already registered for port {self.port_name!r}")
            self._factories[key] = factory
            return factory

        return decorator

    def create(self, key: str, **kwargs) -> T:
        try:
            factory = self._factories[key]
        except KeyError:
            raise UnknownProviderError(self.port_name, key, self.keys()) from None
        return factory(**kwargs)

    def copy(self) -> "ProviderRegistry[T]":
        clone: ProviderRegistry[T] = ProviderRegistry(self.port_name)
        clone._factories = dict(self._factories)
        return clone

    def keys(self) -> list[str]:
        return list(self._factories)
