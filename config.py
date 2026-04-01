"""Configuration extension for odev-plugin-ai-upgrade."""

from odev.common.config import Section


class KnowledgeSection(Section):
    """Configuration for the Upgrade Knowledge Index.

    Only ``repo_url`` needs to be set. The local clone path is derived automatically
    from the URL using odev's standard convention:
    ``config.paths.repositories / <org> / <repo>``
    — consistent with how ``odev pull`` manages all other repositories.
    """

    _name = "knowledge"

    @property
    def repo_url(self) -> str | None:
        """SSH or HTTPS URL of the private GitHub repository used to store upgrade knowledge.
        Example: ``git@github.com:myorg/odev-upgrade-knowledge.git``

        When unset (None), the first-time setup wizard will be triggered on next use.
        The local clone path is inferred automatically from this URL.
        """
        value = self.get("repo_url", "")
        return value or None

    @repo_url.setter
    def repo_url(self, value: str):
        self.set("repo_url", value)
