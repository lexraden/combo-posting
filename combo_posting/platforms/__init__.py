"""Платформы публикации. Общий интерфейс — см. base.Publisher."""
from .base import PublishError, PublishResult, Publisher

__all__ = ["PublishError", "PublishResult", "Publisher", "get_publisher"]


def get_publisher(name: str, **kwargs) -> Publisher:
    if name == "ig":
        from .instagram import InstagramPublisher
        return InstagramPublisher(**kwargs)
    if name == "threads":
        from .threads import ThreadsPublisher
        return ThreadsPublisher(**kwargs)
    raise ValueError(f"нет публикатора для {name}")
