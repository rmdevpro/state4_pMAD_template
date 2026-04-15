"""KernelTEContext — concrete TEContext implementation for the kernel.

Provides tools, checkpointer, config, and inference to TE packages
via dependency injection. TE packages receive this in build_graph()
and use it instead of importing from app.* directly.

ERQ-002 §12.3: TE packages must not import from app.*.
"""

import logging
import os
from typing import Any

_log = logging.getLogger("pmad_template.te_context")


class KernelTEContext:
    """Concrete TEContext that bridges TE packages to the kernel's services."""

    def get_tools_for_model(self, model_name: str, tool_names: list[str]) -> list:
        from app.tools import get_tools_for_model
        return get_tools_for_model(model_name, tool_names)

    def get_all_tools(self) -> dict[str, Any]:
        from app.tools import get_tool_registry
        return get_tool_registry()

    def get_checkpointer(self) -> Any:
        from app.checkpointer import get_checkpointer
        return get_checkpointer()

    def get_api_key(self, llm_config: dict) -> str | None:
        api_key_env = llm_config.get("api_key_env", "")
        if api_key_env:
            return os.environ.get(api_key_env)
        return None

    def get_chat_model(self, llm_config: dict) -> Any:
        from langchain_openai import ChatOpenAI

        api_key = self.get_api_key(llm_config)
        kwargs = {
            "base_url": llm_config.get("base_url"),
            "model": llm_config.get("model", "gpt-4o-mini"),
            "api_key": api_key or "not-needed",
            "timeout": llm_config.get("timeout", 1800),
        }
        temp = llm_config.get("temperature")
        if temp is not None:
            kwargs["temperature"] = temp
        return ChatOpenAI(**kwargs)

    def load_config(self) -> dict:
        from app.config import load_merged_config
        return load_merged_config()
