"""Shared outbound boundary for the proactive public-search path."""

import re

_PRIVATE_TEXT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|(?<!\d)(?:\+?\d[ ().-]*){10,}(?!\d)|\b(?:sk|rk|pk)-[a-z0-9_-]{8,}\b"
    r"|\b(?:api[ _-]?key|access[ _-]?token|bearer|password|passcode)\s*[:= ]"
    r"|(?:手机号|身份证号|家庭住址|家庭地址|银行卡号|私聊原文|密码)[：:\s]?"
    r"|\b(?:home|street) address\b|\b(?!20\d{2}\b)\d{1,6}\s+"
    r"(?:[a-z][a-z'-]*\s+){1,5}(?:street|road|avenue|lane|drive|court|st|rd|ave|ln|blvd)\b"
    r"|(?:路|街|巷|弄)\s*[0-9一二三四五六七八九十百零]+\s*号"
    r"|[a-z]:[\\/]|(?:^|\s)/(?:home|users|mnt|workspace)/"
    r"|https?://|\b[a-z0-9_-]{32,}\b",
    re.IGNORECASE,
)


def is_public_query(text):
    return (isinstance(text, str) and 0 < len(text.strip()) <= 300
            and text.isprintable() and not _PRIVATE_TEXT.search(text))
