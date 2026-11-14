"""Test doubles in one package (docs/testing/00 section 5, CONVENTIONS C6)."""

from .loopback import (OTHER_TOKEN, TOKEN, LoopbackBody, body, collect, error_codes, raw, raw_handshake, raw_session,
                       run, the_link, wait_for, with_server)

__all__ = ["OTHER_TOKEN", "TOKEN", "LoopbackBody", "body", "collect", "error_codes", "raw", "raw_handshake",
           "raw_session", "run", "the_link", "wait_for", "with_server"]
