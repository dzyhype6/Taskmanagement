"""Copy this file to `mpesa_config.py` (same folder) and fill in your Safaricom
Daraja B2C credentials to enable REAL M-Pesa payouts.

Leave the file absent (or values blank) to keep payouts simulated.

Where to get these:
  - Create an app at https://developer.safaricom.co.ke → Consumer Key/Secret.
  - MPESA_SHORTCODE: your B2C shortcode (sandbox provides a test one).
  - MPESA_INITIATOR_NAME + MPESA_SECURITY_CREDENTIAL: from the B2C credentials
    (the security credential is your initiator password encrypted with
    Safaricom's public certificate — the portal has a tool for this).
  - RESULT/TIMEOUT URLs must be PUBLIC https endpoints. For local testing,
    expose your server with a tunnel (e.g. ngrok) and point them at:
        https://<your-tunnel>/mpesa/b2c/result/
        https://<your-tunnel>/mpesa/b2c/timeout/
"""
MPESA_ENV = 'sandbox'            # 'sandbox' while testing, 'production' when live
MPESA_CONSUMER_KEY = 'your-consumer-key'
MPESA_CONSUMER_SECRET = 'your-consumer-secret'
MPESA_SHORTCODE = '600000'
MPESA_INITIATOR_NAME = 'testapi'
MPESA_SECURITY_CREDENTIAL = 'your-encrypted-security-credential'
MPESA_RESULT_URL = 'https://your-tunnel.example/mpesa/b2c/result/'
MPESA_TIMEOUT_URL = 'https://your-tunnel.example/mpesa/b2c/timeout/'
