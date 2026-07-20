"""Concurrency-safe ownership of a session's selected model for one run."""

from __future__ import annotations

from dataclasses import dataclass


def _normalized_provider(value) -> str | None:
    return str(value).strip().lower() or None if value is not None else None


@dataclass
class SessionModelLease:
    """Guard worker writeback from clobbering a newer model-picker choice.

    The HTTP admission path persists the selected model before the detached
    worker starts.  This lease records the value the worker actually owns and
    only allows later profile repair while that value is still current.
    """

    session: object
    lock: object
    model: str | None
    provider: str | None
    owns_value: bool

    @classmethod
    def claim(cls, session, lock, *, model: str, provider: str | None) -> "SessionModelLease":
        provider = _normalized_provider(provider)
        with lock:
            persisted_model = getattr(session, "model", None)
            persisted_provider = _normalized_provider(
                getattr(session, "model_provider", None)
            )
            owns_value = persisted_model in (None, "") or (
                persisted_model == model
                and persisted_provider in (None, provider)
            )
            if owns_value:
                session.model = model
                session.model_provider = provider
                persisted_model = model
                persisted_provider = provider
        return cls(
            session=session,
            lock=lock,
            model=persisted_model,
            provider=persisted_provider,
            owns_value=owns_value,
        )

    def apply_profile_resolution(
        self,
        *,
        model: str,
        provider: str | None,
        repaired: bool,
    ) -> None:
        """Persist profile repair only while this lease still owns the pair."""

        provider = _normalized_provider(provider)
        with self.lock:
            current_provider = _normalized_provider(
                getattr(self.session, "model_provider", None)
            )
            if not (
                self.owns_value
                and getattr(self.session, "model", None) == self.model
                and current_provider == self.provider
            ):
                return
            self.session.model_provider = provider
            if repaired and model != (getattr(self.session, "model", None) or ""):
                self.session.model = model

