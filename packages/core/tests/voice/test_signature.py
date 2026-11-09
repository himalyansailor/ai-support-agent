from supportcore.voice.telephony.signature import is_valid_signature, twilio_signature
from twilio.request_validator import RequestValidator

URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}


def test_signature_matches_twilio_reference():
    ours = twilio_signature("12345", URL, PARAMS)
    assert ours == RequestValidator("12345").compute_signature(URL, PARAMS)
    assert ours == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert is_valid_signature("12345", URL, PARAMS, ours)


def test_signature_rejects_tampered_param():
    signature = twilio_signature("12345", URL, PARAMS)
    assert not is_valid_signature("12345", URL, {**PARAMS, "Digits": "9999"}, signature)
    assert not is_valid_signature("12345", URL + "&x=1", PARAMS, signature)
    assert not is_valid_signature("wrong", URL, PARAMS, signature)
