# -----------------------------------------------------------------------------
# REAL EMAIL SETUP (Gmail) — copy this file to "email_config.py" and fill it in.
# -----------------------------------------------------------------------------
# 1. Use a Gmail account.
# 2. Turn on 2-Step Verification:  https://myaccount.google.com/security
# 3. Create an "App Password":      https://myaccount.google.com/apppasswords
#       - App: "Mail",  Device: "Other (CodeForge)"
#       - Google gives you a 16-character password like: abcd efgh ijkl mnop
# 4. Copy this file to "email_config.py" in the same folder and paste below
#    (remove the spaces from the app password).
# 5. Restart the server. Emails will now send for real.
#
# NOTE: email_config.py holds a secret — don't share it or commit it.

EMAIL_HOST_USER = "youraddress@gmail.com"
EMAIL_HOST_PASSWORD = "abcdefghijklmnop"  # 16-char Gmail App Password (no spaces)
