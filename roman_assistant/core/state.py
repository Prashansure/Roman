from enum import Enum, auto
from queue import Queue


class AssistantState(Enum):
    ONLINE = auto()
    LISTENING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()
    PAUSED = auto()
    CANCELLED = auto()
    ERROR = auto()
    SHUTTING_DOWN = auto()


class EventBus:
    """Thread-safe UI event queue; Tk widgets are only touched by the UI thread."""

    def __init__(self) -> None:
        self.events: Queue[tuple] = Queue()

    def publish(self, event: str, *payload) -> None:
        self.events.put((event, *payload))
