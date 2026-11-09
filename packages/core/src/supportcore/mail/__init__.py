"""The email channel."""


class MailIsolationUnavailable(Exception):
    """The parser child process (`isolate.py`) could not be started. Transient: retry later, skip nothing.

    Unlike a `too_complex` result, this says nothing about the message, so the caller must not advance past it.
    The message never carries mail content.
    """
