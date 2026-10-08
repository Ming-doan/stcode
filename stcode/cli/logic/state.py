"""The last facts reported by the daemon, independent of terminal widgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClientState:
    session_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    effective: dict[str, str] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)

    def adopt_session(self, frame: dict[str, Any]) -> None:
        session_id = str(frame.get("id", ""))
        if session_id:
            self.session_id = session_id
        self.effective = {
            "model": str(frame.get("model", "")),
            "provider": str(frame.get("provider", "")),
            "effort": str(frame.get("reasoning_effort", "")),
            "mode": str(frame.get("approval_mode", "")),
        }

    @property
    def provider(self) -> str:
        return self.effective.get("provider") or str(self.config.get("provider", ""))

    @property
    def provider_entry(self) -> dict[str, Any]:
        return dict(self.config.get("providers", {}).get(self.provider, {}))

    @property
    def effort(self) -> str:
        return self.effective.get("effort") or str(
            self.config.get("reasoning_effort", "")
        )

    @property
    def has_credential(self) -> bool:
        return bool(self.provider_entry.get("has_key"))
