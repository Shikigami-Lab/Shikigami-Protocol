from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, List


class LLMProvider(ABC):
    @abstractmethod
    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Yield response tokens one by one."""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs,
    ) -> str:
        """Return the full response as a single string (non-streaming)."""
        ...
