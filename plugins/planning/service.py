"""Planning plugin service — stub for plugin loader."""

from config.logging_config import logger


class PlanningService:
    """Minimal service stub. All logic is in virtual_tools.py."""

    def __init__(self, config=None):
        self.config = config or {}

    async def initialize(self):
        logger.info("Planning plugin initialized")
