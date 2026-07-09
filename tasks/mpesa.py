"""Safaricom Daraja B2C (Business-to-Customer) — pays money OUT to a phone.

This is OPTIONAL. With no credentials configured, ``send_b2c`` returns a
``{'mode': 'simulated'}`` result and makes no network call, so the app works
offline and the demo is unaffected. Configure the ``MPESA_*`` settings (see
mpesa_config.example.py) to make real payouts.

Real payouts are asynchronous: ``send_b2c`` only *initiates* the payment
(Daraja accepts it and returns a ConversationID); Safaricom then POSTs the
final result — including the real M-Pesa transaction code — to
``MPESA_RESULT_URL`` (handled by the ``mpesa_result`` view).
"""
import requests
from django.conf import settings

REQUIRED = [
    'MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET', 'MPESA_SHORTCODE',
    'MPESA_INITIATOR_NAME', 'MPESA_SECURITY_CREDENTIAL',
    'MPESA_RESULT_URL', 'MPESA_TIMEOUT_URL',
]


def _cfg(name):
    return getattr(settings, name, '') or ''


def enabled():
    """True only when every credential/URL needed for a real payout is set."""
    return all(_cfg(k) for k in REQUIRED)


def _base_url():
    return ('https://api.safaricom.co.ke' if _cfg('MPESA_ENV') == 'production'
            else 'https://sandbox.safaricom.co.ke')


def normalise_phone(phone):
    """07XXXXXXXX / +2547XXXXXXXX / 7XXXXXXXX -> 2547XXXXXXXX."""
    p = ''.join(ch for ch in str(phone) if ch.isdigit())
    if p.startswith('0'):
        p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9:
        p = '254' + p
    return p


def get_access_token():
    r = requests.get(
        _base_url() + '/oauth/v1/generate?grant_type=client_credentials',
        auth=(_cfg('MPESA_CONSUMER_KEY'), _cfg('MPESA_CONSUMER_SECRET')),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()['access_token']


def send_b2c(phone, amount, remarks='Payment', occasion='Payment'):
    """Initiate a B2C payout. Returns one of:
        {'mode': 'simulated'}                         # not configured
        {'mode': 'live', 'ok': True,  'conversation_id': ...}
        {'mode': 'live', 'ok': False, 'error': ...}
    """
    if not enabled():
        return {'mode': 'simulated'}
    try:
        token = get_access_token()
        body = {
            'InitiatorName': _cfg('MPESA_INITIATOR_NAME'),
            'SecurityCredential': _cfg('MPESA_SECURITY_CREDENTIAL'),
            'CommandID': 'BusinessPayment',
            'Amount': int(round(float(amount))),
            'PartyA': _cfg('MPESA_SHORTCODE'),
            'PartyB': normalise_phone(phone),
            'Remarks': (remarks or 'Payment')[:100],
            'QueueTimeOutURL': _cfg('MPESA_TIMEOUT_URL'),
            'ResultURL': _cfg('MPESA_RESULT_URL'),
            'Occasion': (occasion or 'Payment')[:100],
        }
        r = requests.post(
            _base_url() + '/mpesa/b2c/v1/paymentrequest',
            json=body, headers={'Authorization': 'Bearer ' + token}, timeout=30,
        )
        d = r.json()
        if str(d.get('ResponseCode')) == '0':
            return {'mode': 'live', 'ok': True,
                    'conversation_id': d.get('ConversationID', ''),
                    'originator': d.get('OriginatorConversationID', '')}
        return {'mode': 'live', 'ok': False,
                'error': d.get('errorMessage') or d.get('ResponseDescription') or 'B2C request rejected'}
    except Exception as e:                       # network / auth / parsing
        return {'mode': 'live', 'ok': False, 'error': str(e)}
