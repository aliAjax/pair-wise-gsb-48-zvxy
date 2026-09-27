"""违约处置与参与者管理的HTTP路由，由主HTTP层委托调用。"""
import re
from typing import Any


PARTICIPANTS_RE = re.compile(r"^/api/participants$")
PARTICIPANT_RE = re.compile(r"^/api/participants/([^/]+)$")
MARGIN_RE = re.compile(r"^/api/participants/([^/]+)/margin$")
DEFAULTS_RE = re.compile(r"^/api/defaults$")
DEFAULT_RE = re.compile(r"^/api/defaults/(\d+)$")
RECOVER_RE = re.compile(r"^/api/defaults/(\d+)/recover$")


class DefaultRoutes:
    """参与者与违约处置接口。handler需提供_actor()与_send()。返回True表示已处理。"""

    def __init__(self, defaults: Any) -> None:
        self.defaults = defaults

    def handle_get(self, handler: Any, path: str) -> bool:
        if PARTICIPANTS_RE.match(path):
            handler._send(200, {"items": self.defaults.list_participants(handler._actor())})
            return True
        match = PARTICIPANT_RE.match(path)
        if match:
            handler._send(200, self.defaults.participant_detail(handler._actor(), match.group(1)))
            return True
        if DEFAULTS_RE.match(path):
            handler._send(200, {"items": self.defaults.list_cases(handler._actor())})
            return True
        match = DEFAULT_RE.match(path)
        if match:
            handler._send(200, self.defaults.case_detail(handler._actor(), int(match.group(1))))
            return True
        return False

    def handle_post(self, handler: Any, path: str, body: dict) -> bool:
        if PARTICIPANTS_RE.match(path):
            handler._send(201, self.defaults.register_participant(handler._actor(), body))
            return True
        match = MARGIN_RE.match(path)
        if match:
            handler._send(200, self.defaults.adjust_margin(handler._actor(), match.group(1), body))
            return True
        match = RECOVER_RE.match(path)
        if match:
            handler._send(200, self.defaults.recover(handler._actor(), int(match.group(1))))
            return True
        return False
