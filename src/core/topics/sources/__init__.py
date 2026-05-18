"""话题来源实现包。import 各来源模块以触发 @register_topic_source。"""
from src.core.topics.sources import trend                 # noqa: F401
from src.core.topics.sources import conversation_recall    # noqa: F401
from src.core.topics.sources import user_life              # noqa: F401
from src.core.topics.sources import ai_self                # noqa: F401
from src.core.topics.sources import random_api             # noqa: F401
