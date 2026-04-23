"""ORM model registry entry point for core models.

Discovered by Registry._scan_directory() via rglob("models.py").
Importing this module triggers ModelMeta registration for all core models.
"""

from core.config_models import (  # noqa: F401
    ChannelAuthorizedUser,
    GroupMcpPermission,
    MemoryGroup,
    OAuthToken,
    PasswordToken,
    UserMcpPermission,
    UserPlatform,
    UserServiceAccount,
)
from core.models.company import Company  # noqa: F401
from core.models.company_user import CompanyUser  # noqa: F401
from core.models.language import Language  # noqa: F401
from core.models.user import User  # noqa: F401
from core.system_config import SystemConfig  # noqa: F401
