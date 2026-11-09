"""Length limits of the parsed email fields; they equal the `email_messages` / `email_attachments` columns.

`tests/mail/test_parse.py::test_limits_match_model_columns` fails if a column changes without this file.
"""

MAX_MESSAGE_ID = 998  # rfc_message_id, in_reply_to, each reference, content_id
MAX_SUBJECT = 998
MAX_FROM_NAME = 255
MAX_ADDRESS = 320  # from_address, reply_to, each to/cc entry
MAX_FILENAME = 255  # bytes (UTF-8)
MAX_CONTENT_TYPE = 255
MAX_ADDRESSES = 100  # per to/cc list
