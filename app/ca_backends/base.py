"""Abstract base class for CA renewal backends."""
from abc import ABC, abstractmethod


class CABackend(ABC):

    @abstractmethod
    def get_name(self) -> str:
        """Machine-readable backend identifier."""

    @abstractmethod
    def get_label(self) -> str:
        """Human-readable label for UI."""

    @abstractmethod
    def can_auto_renew(self) -> bool:
        """True if this backend supports fully automated renewal without user action."""

    def is_ready(self) -> bool:
        """True if this backend is fully configured and selectable.

        Backends needing setup (credentials / hub registration) override this and
        return False until configuration is complete; the UI greys them out.
        """
        return True

    def not_ready_reason(self) -> str:
        """Short hint shown in the UI when is_ready() is False."""
        return ""

    @abstractmethod
    def get_portal_url(self, email: str, user_config: dict) -> str:
        """Return the CA portal URL for manual renewal steps."""

    @abstractmethod
    def get_instructions_html(
        self,
        email: str,
        days_left: int,
        expiry_str: str,
        upload_url: str,
        user_config: dict,
    ) -> str:
        """Return HTML renewal instructions for the notification email sent to the user."""

    async def initiate_renewal(self, email: str, user_config: dict) -> bool:
        """Initiate automated renewal (Backend B only).
        Raises NotImplementedError when not supported by this backend.
        """
        raise NotImplementedError(f"Backend '{self.get_name()}' unterstützt kein Auto-Renewal")
