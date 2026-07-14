"""SQLAlchemy 2.0 async models for the entities in docs/TZ-5-platform.md §3.

Two supporting tables (`EnrollmentToken`, `RefreshToken`) are added beyond the
literal §3 list because they are required infrastructure for functional
requirements that ARE in scope (node enrollment, TZ-5 §4.1; JWT rotation/deny-list,
TZ-5 §6.3) rather than new product concepts - see platform/README.md "Deviations".
"""
from dist_vpn_platform.models.admin_account import AdminAccount
from dist_vpn_platform.models.base import Base
from dist_vpn_platform.models.cascade import Cascade, CascadeExitNode
from dist_vpn_platform.models.enrollment_token import EnrollmentToken
from dist_vpn_platform.models.health_event import HealthEvent
from dist_vpn_platform.models.link_test import LinkTest
from dist_vpn_platform.models.node import Node
from dist_vpn_platform.models.refresh_token import RefreshToken
from dist_vpn_platform.models.rotation_record import RotationRecord
from dist_vpn_platform.models.node_resource_sample import NodeResourceSample
from dist_vpn_platform.models.usage_stat import UsageStat
from dist_vpn_platform.models.user import User, UserCascade
from dist_vpn_platform.models.user_device_kick_event import UserDeviceKickEvent

__all__ = [
    "Base",
    "Node",
    "Cascade",
    "CascadeExitNode",
    "User",
    "UserCascade",
    "UsageStat",
    "HealthEvent",
    "LinkTest",
    "RotationRecord",
    "AdminAccount",
    "EnrollmentToken",
    "RefreshToken",
    "UserDeviceKickEvent",
    "NodeResourceSample",
]
