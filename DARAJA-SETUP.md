# M-Pesa Daraja setup (real payouts)

The system pays workers through Safaricom's **Daraja B2C API**. With no
credentials configured, payouts are **simulated** (a mock transaction code is
recorded — perfect for demos). Follow these steps to run it against
Safaricom's **sandbox** (their free test environment) or production.

## 1. Get sandbox credentials (free)

1. Create an account at <https://developer.safaricom.co.ke> and log in.
2. **My Apps → Add a new app** → tick **Lipa na M-Pesa Sandbox** → Create.
3. Open the app: copy the **Consumer Key** and **Consumer Secret**.
4. Sandbox test values (from **APIs → M-Pesa B2C → sandbox docs**):
   - Shortcode (PartyA): usually `600XXX` (shown in the B2C test credentials)
   - InitiatorName: `testapi`
   - Initiator password: `Safaricom999!*!` (sandbox default)
5. **Security credential**: on the Daraja portal go to the B2C API page →
   *Security credential* tool → paste the initiator password → it returns a
   long encrypted string. That string is `MPESA_SECURITY_CREDENTIAL`.

## 2. Expose your callbacks (needed for final confirmation)

Daraja reports the payout result asynchronously to a **public https URL**.
For a local machine, use a tunnel:

```
ngrok http 8000
```

Note the https URL it gives you (e.g. `https://ab12cd.ngrok-free.app`).

## 3. Configure the project

Copy the example config and fill it in:

```
copy TaskManagement\mpesa_config.example.py TaskManagement\mpesa_config.py
```

Edit `TaskManagement/mpesa_config.py`:

```python
MPESA_ENV = 'sandbox'
MPESA_CONSUMER_KEY = '…from step 1.3…'
MPESA_CONSUMER_SECRET = '…from step 1.3…'
MPESA_SHORTCODE = '600XXX'
MPESA_INITIATOR_NAME = 'testapi'
MPESA_SECURITY_CREDENTIAL = '…from step 1.5…'
MPESA_RESULT_URL = 'https://ab12cd.ngrok-free.app/mpesa/b2c/result/'
MPESA_TIMEOUT_URL = 'https://ab12cd.ngrok-free.app/mpesa/b2c/timeout/'
```

(This file is git-ignored — your keys never reach the repository.)

## 4. Verify the connection

```
python manage.py mpesa_check
```

- *"Payouts are SIMULATED"* → settings still missing (it lists which).
- *"OAuth OK"* → the system successfully authenticated with Safaricom;
  payouts now run live against the chosen environment.

The **Payments** page also shows a badge: *M-Pesa: simulated* or
*M-Pesa Daraja: CONNECTED (sandbox)*.

## 5. Pay someone

Pay a worker as usual (Engineer page → Pay). With Daraja configured:

- the payslip is created with status **Pending confirmation**;
- Safaricom processes the B2C payment and POSTs the result to your
  `MPESA_RESULT_URL`;
- the payslip flips to **Confirmed ✓** with the **real M-Pesa transaction
  code** (or **Failed**, with the payment amount preserved for review).

## Notes & honest limits

- The **sandbox** proves the full API round-trip (OAuth, B2C request,
  result callback) but does not send real money or SMSes.
- **Production** requires a Safaricom go-live review, a real B2C shortcode
  and a funded working account.
- Bank payouts have no public API and remain simulated by design.
