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
import base64
from datetime import datetime

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


# ---------------------------------------------------------------------------
# STK Push (Lipa na M-Pesa Online / C2B) — pushes a PIN prompt TO a phone.
#
# Unlike B2C above (which pays money OUT and shows no prompt), STK Push asks a
# customer to authorise a payment by entering their M-Pesa PIN on their handset.
# This is the "prompt on my phone asking for a PIN" flow.
#
# NOTE ON SANDBOX: against the Daraja *sandbox* this call returns
# ResponseCode '0' / "Success. Request accepted for processing" — proving the
# API is correctly wired — but NO dialog reaches a real phone (Safaricom only
# simulates it with test number 254708374149). A real prompt on a real handset
# requires *production* (go-live) credentials.
# ---------------------------------------------------------------------------

STK_REQUIRED = [
    'MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET',
    'MPESA_STK_SHORTCODE', 'MPESA_STK_PASSKEY', 'MPESA_STK_CALLBACK_URL',
]


def stk_enabled():
    """True only when every credential/URL needed for an STK Push is set."""
    return all(_cfg(k) for k in STK_REQUIRED)


def stk_push(phone, amount, account_ref='TaskPay', description='Payment'):
    """Initiate an STK Push (Lipa na M-Pesa Online). Returns one of:
        {'mode': 'disabled'}                          # not configured
        {'mode': 'live', 'ok': True,  'checkout_id': ..., 'customer_message': ...}
        {'mode': 'live', 'ok': False, 'error': ...}
    """
    if not stk_enabled():
        return {'mode': 'disabled'}
    try:
        token = get_access_token()
        shortcode = _cfg('MPESA_STK_SHORTCODE')
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(
            (shortcode + _cfg('MPESA_STK_PASSKEY') + timestamp).encode()
        ).decode()
        msisdn = normalise_phone(phone)
        body = {
            'BusinessShortCode': shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'TransactionType': 'CustomerPayBillOnline',
            'Amount': int(round(float(amount))),
            'PartyA': msisdn,
            'PartyB': shortcode,
            'PhoneNumber': msisdn,
            'CallBackURL': _cfg('MPESA_STK_CALLBACK_URL'),
            'AccountReference': (account_ref or 'TaskPay')[:12],
            'TransactionDesc': (description or 'Payment')[:13],
        }
        r = requests.post(
            _base_url() + '/mpesa/stkpush/v1/processrequest',
            json=body, headers={'Authorization': 'Bearer ' + token}, timeout=30,
        )
        d = r.json()
        if str(d.get('ResponseCode')) == '0':
            return {'mode': 'live', 'ok': True,
                    'checkout_id': d.get('CheckoutRequestID', ''),
                    'merchant_id': d.get('MerchantRequestID', ''),
                    'customer_message': d.get('CustomerMessage', ''),
                    'raw': d}
        return {'mode': 'live', 'ok': False,
                'error': d.get('errorMessage') or d.get('ResponseDescription')
                or 'STK push rejected',
                'raw': d}
    except Exception as e:                       # network / auth / parsing
        return {'mode': 'live', 'ok': False, 'error': str(e)}
