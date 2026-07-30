"""CBB player props from DraftKings Sportsbook.

Thin subclass of :class:`DKPropsService` that targets the NCAA Men's
Basketball event group instead of NBA.  All parsing, caching, and odds
math is inherited — only the DK event group ID differs.

If the CBB event group ID changes seasonally, update
``CBB_EVENT_GROUP_ID`` in ``dk_props_service.py`` or set the
``DK_CBB_EVENT_GROUP_ID`` environment variable.
"""

import os

from app.services.dk_props_service import CBB_EVENT_GROUP_ID, DKPropsService


class CBBPropsService(DKPropsService):
    """DraftKings Sportsbook player props for NCAA basketball."""

    def __init__(self):
        # Allow env-var override in case DK shifts the ID mid-season
        event_group_id = int(
            os.environ.get("DK_CBB_EVENT_GROUP_ID", str(CBB_EVENT_GROUP_ID))
        )
        super().__init__(event_group_id=event_group_id)
