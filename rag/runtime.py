"""单个在线请求的截止时间与协作取消状态，可随Context传播到检索线程。"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
import time


class RequestExpired(TimeoutError):
    pass


@dataclass
class RequestState:
    deadline: float
    cancelled: Event = field(default_factory=Event)


REQUEST_STATE: ContextVar[RequestState | None] = ContextVar("ragent_request_state", default=None)


def remaining_timeout(configured: float) -> float:
    state = REQUEST_STATE.get()
    if state is None:
        return configured
    remaining = state.deadline - time.monotonic()
    if state.cancelled.is_set() or remaining <= 0:
        raise RequestExpired("请求截止时间已到或已取消")
    return min(configured, remaining)
